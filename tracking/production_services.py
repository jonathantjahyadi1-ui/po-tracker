"""Manual material standards, immutable snapshots, and explicit shipment closure."""

import json
import re
from decimal import Decimal, InvalidOperation, localcontext

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from .models import KirimGudang, Master, MaterialUsage, Po, ProductionUnit, Roll
from .services import limited_text, log, po_rolls, require_mapped_production, require_purchasing

MISSING_USAGE = 'Pemakaian paten untuk produk dan bahan ini belum diisi di Master.'


def parse_usage(value):
    text = (format(value, 'f') if isinstance(value, Decimal) else str(value)).strip()
    if not re.fullmatch(r'[0-9]+(?:[.,][0-9]{1,8})?', text):
        raise ValidationError('Isi pemakaian paten dengan angka desimal, maksimal 8 angka pecahan.')
    try:
        amount = Decimal(text.replace(',', '.'))
    except InvalidOperation:
        raise ValidationError('Pemakaian paten harus berupa angka.')
    if not amount.is_finite() or amount <= 0 or amount >= Decimal('1000000000000'):
        raise ValidationError(
            'Pemakaian paten harus lebih dari 0 dan kurang dari 1 triliun yard/pcs.'
        )
    return amount


def estimate_pcs(yard, usage):
    """Compare the exact remainder, with no prematurely rounded division."""
    yard, usage = Decimal(str(yard)), Decimal(str(usage))
    if not yard.is_finite() or not usage.is_finite() or yard < 0 or usage <= 0:
        raise ValidationError('Yard harus tidak negatif dan pemakaian paten harus lebih dari 0.')
    with localcontext() as context:
        context.prec = 64
        whole = yard // usage
        remainder = yard - whole * usage
        return int(whole) + int(2 * remainder >= usage)


def lock_po(pk):
    # All production writers acquire the same lock before reading shipment totals.
    if connection.vendor == 'sqlite':
        Po.objects.filter(pk=pk).update(selesai=F('selesai'))
    return Po.objects.select_for_update().get(pk=pk)


def selected_product(value):
    try:
        pk = value.pk if isinstance(value, Master) else int(value)
    except (ValueError, TypeError):
        raise ValidationError('Pilih produk aktif dari Master Produk.')
    product = Master.objects.filter(pk=pk, kind='product', active=True).first()
    if product is None:
        raise ValidationError('Pilih produk aktif dari Master Produk.')
    return product


@transaction.atomic
def save_material_usages(product, rows, user):
    require_purchasing(user)
    if connection.vendor == 'sqlite':
        try:
            product_id = product.pk if isinstance(product, Master) else int(product)
        except (TypeError, ValueError):
            raise ValidationError('Pilih produk aktif dari Master Produk.')
        Master.objects.filter(pk=product_id, kind='product').update(active=F('active'))
    product = selected_product(product)
    # Serialize inserts as well as edits for this product, including duplicate submissions.
    product = Master.objects.select_for_update().get(pk=product.pk)
    if not product.active:
        raise ValidationError('Pilih produk aktif dari Master Produk.')
    existing = {u.pk: u for u in MaterialUsage.objects.select_for_update().filter(product=product)}
    seen, parsed = set(), []
    if not isinstance(rows, list) or not rows or len(rows) > 500:
        raise ValidationError('Isi minimal satu bahan, maksimal 500 baris.')
    for row in rows:
        try:
            identity = int(row['id']) if row.get('id') else None
            material_id = (
                row['material'].pk if isinstance(row['material'], Master) else int(row['material'])
            )
        except (ValueError, TypeError, KeyError):
            raise ValidationError('Pilih bahan yang valid.')
        if material_id in seen:
            raise ValidationError('Kombinasi produk dan bahan duplikat; edit entri yang sudah ada.')
        seen.add(material_id)
        old = existing.get(identity)
        if identity and (old is None or old.material_id != material_id):
            raise ValidationError('Identitas patokan tidak sesuai produk dan bahan.')
        material = Master.objects.filter(pk=material_id, kind='material').first()
        if material is None or (not material.active and old is None):
            raise ValidationError('Pilih bahan aktif dari Master Bahan.')
        if old is None and any(u.material_id == material_id for u in existing.values()):
            raise ValidationError('Kombinasi produk dan bahan sudah ada; edit entri tersebut.')
        active = row.get('active', True)
        if not isinstance(active, bool):
            raise ValidationError('Status aktif tidak valid.')
        parsed.append((old, material, parse_usage(row.get('usage')), active))
    for old, material, amount, active in parsed:
        before = (
            None if old is None else {'usage': str(old.usage_yard_per_pcs), 'active': old.active}
        )
        if old and old.usage_yard_per_pcs == amount and old.active == active:
            continue
        if old is None:
            try:
                with transaction.atomic():
                    old = MaterialUsage.objects.create(
                        product=product,
                        material=material,
                        usage_yard_per_pcs=amount,
                        active=active,
                        created_by=user,
                        updated_by=user,
                    )
            except IntegrityError:
                raise ValidationError('Kombinasi produk dan bahan sudah ada; muat ulang Master.')
        else:
            old.usage_yard_per_pcs, old.active = amount, active
            old.updated_by, old.updated_at = user, timezone.now()
            old.save(update_fields=['usage_yard_per_pcs', 'active', 'updated_by', 'updated_at'])
        log(
            user,
            'ubah pemakaian paten',
            f'Patokan #{old.pk}',
            json.dumps(
                {
                    'product_id': product.pk,
                    'material_id': material.pk,
                    'before': before,
                    'after': {'usage': str(amount), 'active': active},
                }
            ),
        )


def require_open_unit(po, material, color, *, allow_legacy_metadata=False):
    if po.legacy_completed or po.selesai:
        if not allow_legacy_metadata:
            raise ValidationError('PO historis sudah selesai; perubahan memerlukan alur koreksi.')
    unit = ProductionUnit.objects.filter(po=po, material=material, color=color).first()
    if unit and unit.continuation == 'final':
        raise ValidationError('Pengiriman sudah final; perubahan memerlukan alur koreksi.')
    return unit


def take_snapshots(po, product, pairs, user, *, allow_existing_shipments=False):
    """Caller holds the PO lock. Existing units never acquire a new standard silently."""
    product = selected_product(product)
    if po.legacy_completed or po.selesai:
        raise ValidationError('PO historis sudah selesai; patokan tidak diubah.')
    pairs = set(pairs)
    if not pairs:
        raise ValidationError('Belum ada yard yang dialokasikan pada PO ini.')
    if po.product_id and po.product_id != product.pk:
        raise ValidationError('Produk berbeda dari produk PO; ubah produk draft secara eksplisit.')
    for material_id, color_id in sorted(pairs):
        require_mapped_production(po, Master.objects.get(pk=color_id))
        old = require_open_unit(po, material_id, color_id)
        if (
            not allow_existing_shipments
            and KirimGudang.objects.filter(
                po=po, material_id=material_id, color_id=color_id
            ).exists()
        ):
            raise ValidationError(
                'Sudah ada pengiriman; dasar yard tidak boleh ditambah melalui alokasi biasa.'
            )
        source = (
            MaterialUsage.objects.select_for_update(of=('self',))
            .filter(
                product=product,
                material_id=material_id,
                active=True,
                product__active=True,
                material__active=True,
            )
            .first()
        )
        if source is None:
            raise ValidationError(MISSING_USAGE)
        if old:
            if old.product_id != product.pk or old.usage_snapshot != source.usage_yard_per_pcs:
                raise ValidationError(
                    'Patokan Master berbeda dari snapshot unit PO ini. '
                    'Gunakan PO baru agar patokan lama dan baru tidak tercampur.'
                )
            continue
        unit = ProductionUnit.objects.create(
            po=po,
            product=product,
            material_id=material_id,
            color_id=color_id,
            usage_source=source,
            usage_snapshot=source.usage_yard_per_pcs,
        )
        log(
            user,
            'snapshot pemakaian paten',
            po.nomor,
            json.dumps(
                {
                    'unit_id': unit.pk,
                    'product_id': product.pk,
                    'material_id': material_id,
                    'color_id': color_id,
                    'usage_source_id': source.pk,
                    'usage_snapshot': str(unit.usage_snapshot),
                    'snapshot_at': unit.snapshot_at.isoformat(),
                }
            ),
        )
    if po.product_id is None:
        po.product, po.produk = product, product.name
        po.save(update_fields=['product', 'produk'])


@transaction.atomic
def set_legacy_snapshots(po, product, user):
    require_purchasing(user)
    po = lock_po(po.pk)
    pairs = set(po_rolls(po).values_list('material_id', 'color_id'))
    # Explicit historical setup only fills missing snapshots, never refreshes an existing one.
    existing = set(po.production_units.values_list('material_id', 'color_id'))
    missing = pairs - existing
    if not missing:
        raise ValidationError('Seluruh unit sudah memiliki snapshot patokan.')
    for material_id, color_id in missing:
        yard = (
            po_rolls(po)
            .filter(material_id=material_id, color_id=color_id)
            .aggregate(total=Sum('yard'))['total']
        )
        if yard is None or yard <= 0:
            raise ValidationError('Total yard alokasi harus lebih dari 0.')
    take_snapshots(po, product, missing, user, allow_existing_shipments=True)
    return po


@transaction.atomic
def change_draft_product(po, product, user, cmt=None):
    require_purchasing(user)
    po = lock_po(po.pk)
    if cmt is not None and po.cmt_id != cmt.pk:
        raise ValidationError('PO tidak dimiliki CMT ini.')
    product = selected_product(product)
    if po.product_id == product.pk:
        return po
    if (
        po.legacy_completed
        or po.selesai
        or KirimGudang.objects.filter(po=po).exists()
        or po.production_units.filter(continuation='final').exists()
    ):
        raise ValidationError(
            'Produk sudah memiliki pengiriman atau sudah final; gunakan alur koreksi.'
        )
    before = {
        'product_id': po.product_id,
        'name': po.produk,
        'snapshots': list(po.production_units.values('id', 'usage_snapshot', 'usage_source_id')),
    }
    pairs = set(po_rolls(po).values_list('material_id', 'color_id'))
    sources = {}
    for material_id, color_id in sorted(pairs):
        source = (
            MaterialUsage.objects.select_for_update(of=('self',))
            .filter(product=product, material_id=material_id, active=True, material__active=True)
            .first()
        )
        if source is None:
            raise ValidationError(MISSING_USAGE)
        sources[(material_id, color_id)] = source
    po.product, po.produk = product, product.name
    po.save(update_fields=['product', 'produk'])
    for (material_id, color_id), source in sources.items():
        ProductionUnit.objects.update_or_create(
            po=po,
            material_id=material_id,
            color_id=color_id,
            defaults={
                'product': product,
                'usage_source': source,
                'usage_snapshot': source.usage_yard_per_pcs,
                'snapshot_at': timezone.now(),
            },
        )
    log(
        user,
        'ubah produk draft',
        po.nomor,
        json.dumps(
            {
                'before': before,
                'after': {
                    'product_id': product.pk,
                    'name': product.name,
                    'snapshots': list(
                        po.production_units.values('id', 'usage_snapshot', 'usage_source_id')
                    ),
                },
            },
            default=str,
        ),
    )
    return po


@transaction.atomic
def save_delivery_decision(po, unit_id, continuation, note, user):
    require_purchasing(user)
    po = lock_po(po.pk)
    try:
        unit = ProductionUnit.objects.get(pk=int(unit_id), po=po)
    except (ValueError, TypeError, ProductionUnit.DoesNotExist):
        raise ValidationError('Pilih unit produksi yang terkait PO ini.')
    if continuation not in ProductionUnit.Continuation.values:
        raise ValidationError('Pilih kelanjutan pengiriman yang valid.')
    note = limited_text(note, 'Keterangan Selisih', 4000)
    if unit.continuation == 'final':
        if continuation == 'final' and note == unit.discrepancy_note:
            return unit
        raise ValidationError('Pengiriman sudah final; perubahan memerlukan alur koreksi.')
    require_open_unit(po, unit.material_id, unit.color_id)
    yard = (
        po_rolls(po)
        .filter(material_id=unit.material_id, color_id=unit.color_id)
        .aggregate(total=Sum('yard'))['total']
    )
    if yard is None or yard <= 0:
        raise ValidationError('Total yard alokasi harus lebih dari 0.')
    estimate = estimate_pcs(yard, unit.usage_snapshot)
    shipped = (
        KirimGudang.objects.filter(
            po=po, material_id=unit.material_id, color_id=unit.color_id
        ).aggregate(total=Sum('pcs'))['total']
        or 0
    )
    if continuation == 'final' and shipped < estimate and not note:
        raise ValidationError(
            {
                'discrepancy_note': (
                    'Keterangan Selisih wajib diisi untuk finalisasi dengan kekurangan.'
                )
            }
        )
    before = {'continuation': unit.continuation, 'note': unit.discrepancy_note}
    unit.continuation, unit.discrepancy_note = continuation, note
    if continuation == 'final':
        unit.finalized_by, unit.finalized_at = user, timezone.now()
    unit.save(update_fields=['continuation', 'discrepancy_note', 'finalized_by', 'finalized_at'])
    log(
        user,
        'kelanjutan pengiriman',
        po.nomor,
        json.dumps(
            {
                'unit_id': unit.pk,
                'before': before,
                'after': {'continuation': continuation, 'note': note},
                'yard': str(yard),
                'estimate': estimate,
                'shipped': shipped,
                'finalized_at': unit.finalized_at.isoformat() if unit.finalized_at else None,
            }
        ),
    )
    return unit


def apply_production_status(summaries, ids):
    """Same bounded resolver for detail, lists, dashboards, and exports."""
    pos = Po.objects.in_bulk(ids)
    units = {
        (u.po_id, u.material_id, u.color_id): u
        for u in ProductionUnit.objects.filter(po_id__in=ids).select_related(
            'product', 'finalized_by'
        )
    }
    yards = {}
    for item in (
        Roll.objects.filter(
            Q(alokasi__po_id__in=ids) | Q(alokasi__isnull=True, invoice_po__po_id__in=ids)
        )
        .values('alokasi__po_id', 'invoice_po__po_id', 'material_id', 'color_id')
        .annotate(total=Sum('yard'))
    ):
        key = (
            item['alokasi__po_id'] or item['invoice_po__po_id'],
            item['material_id'],
            item['color_id'],
        )
        yards[key] = yards.get(key, Decimal(0)) + item['total']
    for po_id, summary in summaries.items():
        historical = pos[po_id].legacy_completed or pos[po_id].selesai
        summary['actual_over'] = summary['over']
        for row in summary['rows']:
            row['actual_over'] = row['over']
            key = (po_id, row['material_id'], row['color_id'])
            unit = units.get(key)
            yard = yards.get(key, Decimal(0))
            estimate = estimate_pcs(yard, unit.usage_snapshot) if unit else None
            shipped = row['shipped']
            delta = shipped - estimate if estimate is not None else None
            row.update(
                unit=unit,
                usage_snapshot=unit.usage_snapshot if unit else None,
                estimate=estimate,
                difference=delta,
                allocated_yard=yard,
                patent_missing=unit is None,
                legacy_completed=historical,
            )
            if historical:
                continue
            row['remaining'] = max(-delta, 0) if delta is not None else None
            row['over'] = max(delta, 0) if delta is not None else 0
            if unit is None:
                code, label = 'patokan_belum', 'Patokan belum ditetapkan'
            elif unit.continuation == 'final':
                code, label = (
                    ('selesai_selisih', 'Selesai dengan Selisih')
                    if delta < 0
                    else ('done', 'Selesai')
                )
            elif delta < 0 and unit.continuation == 'follow_up':
                code, label = 'kurang_kirim', 'Kurang Kirim'
            elif delta < 0 and shipped > 0:
                code, label = 'menunggu_keterangan', 'Menunggu Keterangan'
            elif shipped > 0:
                code, label = 'dalam_pengiriman', 'Dalam pengiriman'
            else:
                code, label = 'dalam_produksi', 'Dalam produksi'
            row.update(status_key=code, status_label=label)
        rows = summary['rows']
        codes = {r['status_key'] for r in rows}
        if historical:
            code, label = 'done', 'Selesai (historis)'
        elif 'menunggu_keterangan' in codes:
            code, label = 'menunggu_keterangan', 'Menunggu Keterangan'
        elif 'kurang_kirim' in codes:
            code, label = 'kurang_kirim', 'Kurang Kirim'
        elif 'patokan_belum' in codes:
            code, label = 'patokan_belum', 'Patokan belum ditetapkan'
        elif rows and codes <= {'done', 'selesai_selisih'}:
            code, label = (
                ('selesai_selisih', 'Selesai dengan Selisih')
                if 'selesai_selisih' in codes
                else ('done', 'Selesai')
            )
        elif 'dalam_pengiriman' in codes:
            code, label = 'dalam_pengiriman', 'Dalam pengiriman'
        else:
            code, label = 'dalam_produksi', 'Dalam produksi'
        summary.update(
            code=code,
            label=label,
            status_key=code,
            status_label=label,
            is_done=code in ('done', 'selesai_selisih'),
            legacy_completed=historical,
            estimate=sum(r['estimate'] for r in rows)
            if rows and all(r['estimate'] is not None for r in rows)
            else None,
            missing_patents=any(r['patent_missing'] for r in rows),
        )
        if not historical:
            summary.update(
                remaining=sum(r['remaining'] or 0 for r in rows), over=sum(r['over'] for r in rows)
            )
    return summaries
