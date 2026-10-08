"""Size totals and historical shipment allocation, serialized by the existing PO lock."""

import json
import re
from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum

from .models import Hasil, HasilUkuran, KirimGudang, KirimUkuran, Po, normalize_name
from .services import (
    limited_text,
    log,
    production_material,
    require_mapped_production,
    require_purchasing,
    valid_date,
)


def whole_pcs(value, *, blank_zero=False):
    if blank_zero and value in ('', None):
        return 0
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)):
        raise ValidationError('Isi jumlah hasil/kiriman berupa bilangan bulat, minimal 0.')
    result = int(value)
    if result > 2147483647:
        raise ValidationError('Jumlah pcs maksimal 2147483647.')
    return result


def row_id(value):
    if value in (None, ''):
        return None
    result = whole_pcs(value)
    if not result:
        raise ValidationError('Identitas rincian tidak valid.')
    return result


def sequence(value):
    if not isinstance(value, list) or len(value) > 500:
        raise ValidationError('Rincian harus berupa daftar, maksimal 500 baris per permintaan.')
    if any(not isinstance(row, dict) for row in value):
        raise ValidationError('Format rincian tidak valid.')
    return value


def item_shipments(hasil):
    return KirimGudang.objects.filter(
        po_id=hasil.po_id, material_id=hasil.material_id, color_id=hasil.color_id
    )


def pending_shipments(hasil):
    return (
        item_shipments(hasil)
        .annotate(detailed=Sum('sizes__pcs'))
        .filter(Q(detailed__isnull=True) | ~Q(detailed=F('pcs')))
    )


def shipped_sizes(hasil):
    return dict(
        KirimUkuran.objects.filter(ukuran__hasil=hasil)
        .values('ukuran_id')
        .annotate(total=Sum('pcs'))
        .values_list('ukuran_id', 'total')
    )


def get_item(po, material, color, hasil_id=None):
    material = production_material(po, color, material)
    require_mapped_production(po, color)
    hasil = Hasil.objects.filter(po=po, material=material, color=color).first()
    if hasil_id is not None and (hasil is None or hasil.pk != row_id(hasil_id)):
        raise ValidationError('Item produksi tidak terkait PO, bahan, dan warna ini.')
    return material, hasil


@transaction.atomic
def simpan_ukuran(po, color, material, rows, user, hasil_id=None):
    require_purchasing(user)
    po = Po.objects.select_for_update().get(pk=po.pk)
    material, hasil = get_item(po, material, color, hasil_id)
    rows = sequence(rows)
    if not rows:
        raise ValidationError(
            'Isi minimal satu ukuran. Gunakan 0 untuk hasil ukuran yang belum ada.'
        )
    existing = {size.pk: size for size in hasil.sizes.all()} if hasil else {}
    shipped = shipped_sizes(hasil) if hasil else {}
    parsed, keys, identities = [], set(), set()
    for at, row in enumerate(rows):
        label = row.get('label')
        if not isinstance(label, str) or not normalize_name(label):
            raise ValidationError(f'Baris {at + 1}: label ukuran wajib diisi.')
        label = normalize_name(label)
        key = label.upper()
        if len(key) > 80:
            raise ValidationError('Label ukuran maksimal 80 karakter.')
        if key in keys:
            raise ValidationError(f'Ukuran {label} sudah digunakan.')
        keys.add(key)
        identity = row_id(row.get('id'))
        if identity is not None and (identity not in existing or identity in identities):
            raise ValidationError(
                'Identitas ukuran tidak terkait item ini atau digunakan dua kali.'
            )
        identities.add(identity)
        pcs = whole_pcs(row.get('pcs'))
        old = existing.get(identity)
        if old and identity in shipped:
            if old.label != label:
                raise ValidationError(
                    f'Ukuran {old.label} memiliki histori kiriman; label tidak boleh diganti.'
                )
            if pcs < shipped[identity]:
                raise ValidationError(
                    f'Hasil ukuran {label} tidak boleh kurang dari '
                    f'{shipped[identity]} pcs yang sudah terkirim.'
                )
        parsed.append({'id': identity, 'label': label, 'key': key, 'pcs': pcs, 'urut': at})
    deleted = set(existing) - identities
    if deleted & set(shipped):
        raise ValidationError('Ukuran dengan histori kiriman tidak boleh dihapus.')
    total = sum(row['pcs'] for row in parsed)
    if total > 2147483647:
        raise ValidationError('Total hasil terlalu besar.')
    if hasil and not hasil.sizes_complete and total != hasil.pcs:
        raise ValidationError(
            f'Pembagian pertama harus sama dengan total hasil lama {hasil.pcs} pcs. '
            'Koreksi total dapat dilakukan setelah pembagian tersimpan.'
        )
    if hasil and pending_shipments(hasil).exists() and total != hasil.pcs:
        raise ValidationError(
            'Kiriman lama belum dirinci per ukuran; perubahan total hasil diblokir.'
        )
    before_total = hasil.pcs if hasil else None
    before_complete = hasil.sizes_complete if hasil else False
    before = [
        {'id': s.pk, 'label': s.label, 'pcs': s.pcs, 'urut': s.urut} for s in existing.values()
    ]
    if (
        hasil
        and hasil.sizes_complete
        and before == [{k: row[k] for k in ('id', 'label', 'pcs', 'urut')} for row in parsed]
    ):
        return hasil
    if hasil is None:
        legacy_sent = (
            KirimGudang.objects.filter(po=po, material=material, color=color).aggregate(
                total=Sum('pcs')
            )['total']
            or 0
        )
        if total < legacy_sent:
            raise ValidationError(
                f'Total hasil tidak boleh kurang dari kiriman lama {legacy_sent} pcs.'
            )
        hasil = Hasil.objects.create(po=po, material=material, color=color, pcs=total)
    # Free changed keys before replacing rows; unshipped labels may be swapped.
    for identity, old in existing.items():
        if identity not in deleted:
            HasilUkuran.objects.filter(pk=identity).update(key=f'__editing_{identity}')
    HasilUkuran.objects.filter(pk__in=deleted).delete()
    for row in parsed:
        identity = row.pop('id')
        if identity is None:
            HasilUkuran.objects.create(hasil=hasil, **row)
        else:
            HasilUkuran.objects.filter(pk=identity, hasil=hasil).update(**row)
    hasil.pcs, hasil.sizes_complete = total, True
    hasil.save(update_fields=['pcs', 'sizes_complete'])
    after = list(hasil.sizes.values('id', 'label', 'pcs', 'urut'))
    log(
        user,
        'hasil ukuran',
        po.nomor,
        json.dumps(
            {
                'hasil_id': hasil.pk,
                'bahan': material.name,
                'warna': color.name,
                'before': before,
                'before_total': before_total,
                'before_sizes_complete': before_complete,
                'after': after,
                'total': total,
            },
            ensure_ascii=False,
        ),
    )
    return hasil


def quantities(rows, sizes):
    result = {}
    for row in sequence(rows):
        identity = row_id(row.get('id'))
        if identity not in sizes or identity in result:
            raise ValidationError('Ukuran tidak terkait item ini atau digunakan dua kali.')
        pcs = whole_pcs(row.get('pcs'), blank_zero=True)
        result[identity] = pcs
    return {identity: pcs for identity, pcs in result.items() if pcs}


@transaction.atomic
def kirim_ukuran(po, color, material, data, rows, user, hasil_id=None):
    require_purchasing(user)
    po = Po.objects.select_for_update().get(pk=po.pk)
    material, hasil = get_item(po, material, color, hasil_id)
    if hasil is None or not hasil.sizes_complete:
        raise ValidationError('Isi rincian ukuran hasil produksi sebelum mencatat kiriman baru.')
    sizes = {s.pk: s for s in hasil.sizes.all()}
    amounts = quantities(rows, sizes)
    if not amounts:
        raise ValidationError('Isi minimal satu kiriman ukuran lebih dari 0 pcs.')
    identity = data.get('request_id')
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 64:
        raise ValidationError('Identitas permintaan kiriman wajib diisi, maksimal 64 karakter.')
    tanggal = data.get('tanggal')
    valid_date(tanggal, 'Tanggal kirim gudang')
    warehouse = data.get('gudang')
    if warehouse is not None and (warehouse.kind != 'warehouse' or not warehouse.active):
        raise ValidationError('Pilih gudang aktif dari daftar.')
    payload = {
        'tanggal': tanggal,
        'gudang_id': warehouse.pk if warehouse else None,
        'surat_jalan': limited_text(data.get('surat_jalan', ''), 'Surat jalan gudang', 80),
        'catatan': limited_text(data.get('catatan', ''), 'Catatan kiriman', 4000),
        'pcs': sum(amounts.values()),
    }
    previous = KirimGudang.objects.filter(request_id=identity).first()
    if previous:
        old_amounts = dict(previous.sizes.values_list('ukuran_id', 'pcs'))
        if (
            previous.po_id != po.pk
            or previous.material_id != material.pk
            or previous.color_id != color.pk
            or amounts != old_amounts
            or any(getattr(previous, k) != v for k, v in payload.items())
        ):
            raise ValidationError('Identitas permintaan sudah digunakan untuk kiriman berbeda.')
        return previous
    if pending_shipments(hasil).exists():
        raise ValidationError(
            'Kiriman lama belum dirinci per ukuran; lengkapi rekonsiliasi terlebih dahulu.'
        )
    shipped = shipped_sizes(hasil)
    for size_id, pcs in amounts.items():
        remaining = sizes[size_id].pcs - shipped.get(size_id, 0)
        if pcs > remaining:
            raise ValidationError(
                f'Kiriman ukuran {sizes[size_id].label} melebihi sisa {max(remaining, 0)} pcs.'
            )
    try:
        with transaction.atomic():
            shipment = KirimGudang.objects.create(
                po=po, material=material, color=color, request_id=identity, **payload
            )
            KirimUkuran.objects.bulk_create(
                [
                    KirimUkuran(kiriman=shipment, ukuran_id=size_id, pcs=pcs)
                    for size_id, pcs in amounts.items()
                ]
            )
    except IntegrityError:
        if KirimGudang.objects.filter(request_id=identity).exists():
            raise ValidationError('Identitas permintaan sudah digunakan untuk kiriman berbeda.')
        raise
    log(
        user,
        'kirim gudang',
        po.nomor,
        json.dumps(
            {
                'kiriman_id': shipment.pk,
                'hasil_id': hasil.pk,
                'sizes': {sizes[k].label: v for k, v in amounts.items()},
                'pcs': payload['pcs'],
                'tanggal': str(tanggal),
            },
            ensure_ascii=False,
        ),
    )
    return shipment


@transaction.atomic
def rekonsiliasi_ukuran(po, hasil_id, rows, user):
    require_purchasing(user)
    po = Po.objects.select_for_update().get(pk=po.pk)
    hasil = Hasil.objects.filter(pk=row_id(hasil_id), po=po).first()
    if hasil is None or not hasil.sizes_complete:
        raise ValidationError('Isi rincian ukuran pada item PO ini terlebih dahulu.')
    require_mapped_production(po, hasil.color)
    sizes = {s.pk: s for s in hasil.sizes.all()}
    shipments = {s.pk: s for s in item_shipments(hasil).prefetch_related('sizes')}
    rows = sequence(rows)
    pending = set(pending_shipments(hasil).values_list('pk', flat=True))
    parsed, seen, additions = {}, set(), defaultdict(int)
    for row in rows:
        identity = row_id(row.get('shipment_id'))
        if identity not in shipments or identity in seen:
            raise ValidationError('Kiriman tidak terkait item ini atau digunakan dua kali.')
        seen.add(identity)
        amounts = quantities(row.get('sizes'), sizes)
        if sum(amounts.values()) != shipments[identity].pcs:
            raise ValidationError(
                f'Total rincian kiriman #{identity} harus {shipments[identity].pcs} pcs.'
            )
        existing = {d.ukuran_id: d.pcs for d in shipments[identity].sizes.all()}
        if existing:
            if amounts != existing:
                raise ValidationError(
                    'Kiriman sudah dirinci; pembagian tersimpan tidak boleh ditimpa.'
                )
            continue
        parsed[identity] = amounts
        for size_id, pcs in amounts.items():
            additions[size_id] += pcs
    if not pending.issubset(seen):
        raise ValidationError('Lengkapi seluruh kiriman lama item ini dalam satu rekonsiliasi.')
    shipped = shipped_sizes(hasil)
    for size_id, pcs in additions.items():
        if pcs + shipped.get(size_id, 0) > sizes[size_id].pcs:
            raise ValidationError(
                f'Alokasi kiriman lama ukuran {sizes[size_id].label} melebihi hasil produksinya.'
            )
    if not parsed:
        return hasil
    KirimUkuran.objects.bulk_create(
        [
            KirimUkuran(kiriman_id=identity, ukuran_id=size_id, pcs=pcs)
            for identity, amounts in parsed.items()
            for size_id, pcs in amounts.items()
        ]
    )
    log(
        user,
        'rekonsiliasi kiriman ukuran',
        po.nomor,
        json.dumps(
            {
                'hasil_id': hasil.pk,
                'before': 'Kiriman belum dirinci',
                'after': parsed,
                'total_terkirim_tetap': sum(s.pcs for s in shipments.values()),
            },
            ensure_ascii=False,
        ),
    )
    return hasil


def production_size_data(po):
    """Bounded reads; parent transaction totals remain the legacy source of truth."""
    items = list(Hasil.objects.filter(po=po).prefetch_related('sizes'))
    details = defaultdict(list)
    sent = defaultdict(int)
    for row in KirimUkuran.objects.filter(kiriman__po=po).values(
        'kiriman_id', 'ukuran_id', 'ukuran__label', 'pcs'
    ):
        details[row['kiriman_id']].append(row)
        sent[row['ukuran_id']] += row['pcs']
    by_pair = defaultdict(list)
    for shipment in KirimGudang.objects.filter(po=po).order_by('tanggal', 'pk'):
        by_pair[(shipment.material_id, shipment.color_id)].append(shipment)
    result = {}
    for item in items:
        shipments = by_pair[(item.material_id, item.color_id)]
        pending = [s for s in shipments if sum(d['pcs'] for d in details[s.pk]) != s.pcs]
        complete = item.sizes_complete and not pending
        sizes = [
            {
                'id': s.pk,
                'label': s.label,
                'pcs': s.pcs,
                'shipped': sent.get(s.pk, 0) if complete else None,
                'remaining': s.pcs - sent.get(s.pk, 0) if complete else None,
                'history': s.pk in sent,
            }
            for s in item.sizes.all()
        ]
        result[f'{item.material_id or "legacy"}-{item.color_id}'] = {
            'id': item.pk,
            'pcs': item.pcs,
            'sizes_complete': item.sizes_complete,
            'shipments_complete': complete,
            'sizes': sizes,
            'pending': [
                {
                    'id': s.pk,
                    'pcs': s.pcs,
                    'date': s.tanggal.isoformat(),
                    'surat_jalan': s.surat_jalan,
                }
                for s in pending
            ],
        }
    return result
