import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from .models import (
    Alokasi,
    AlokasiRoll,
    Hasil,
    HasilUkuran,
    Invoice,
    InvoiceAttachment,
    InvoiceColor,
    InvoiceMaterial,
    InvoiceWrite,
    KirimGudang,
    KirimUkuran,
    Log,
    Master,
    Po,
    Roll,
    normalize_po,
)

ZERO = Decimal('0.00')
# Siap kirim and the production roll states remain readable for legacy data only.
TRANSITIONS = {
    'tersedia': {'menunggu'},
    'menunggu': {'dikirim', 'tersedia'},
    'siap_kirim': {'dikirim', 'tersedia'},
    'dikirim': {'diterima'},
    'diterima': set(),
    'terpakai': set(),
    'rusak': set(),
}


def require_purchasing(user):
    if not user.is_authenticated or not user.is_active or user.role != 'purchasing':
        raise PermissionDenied('Hanya Purchasing yang dapat mengubah data operasional.')


def log(user, aksi, objek, detail=''):
    return Log.objects.create(user=user, aksi=aksi, objek=objek, detail=detail)


def valid_date(value, label):
    if (
        not isinstance(value, date)
        or isinstance(value, datetime)
        or value > timezone.localdate() + timedelta(days=1)
    ):
        raise ValidationError(f'{label} wajib diisi dan tidak boleh lebih dari besok.')


def limited_text(value, label, max_length):
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > max_length:
        raise ValidationError(f'{label} maksimal {max_length} karakter.')
    return value.strip()


def master(kind, name):
    if kind not in Master.Kind.values:
        raise ValidationError('Jenis master tidak dikenal.')
    name = ' '.join(name.split())
    if not name:
        raise ValidationError('Nama wajib diisi.')
    if len(name) > 160:
        raise ValidationError('Nama maksimal 160 karakter.')
    found = Master.objects.filter(kind=kind, name__iexact=name).first()
    if not found:
        try:
            with transaction.atomic():
                found = Master.objects.create(kind=kind, name=name)
        except IntegrityError:
            found = Master.objects.get(kind=kind, name__iexact=name)
    if not found.active:
        raise ValidationError(f'{found.name} sudah nonaktif.')
    return found


def roll_count(queryset):
    return queryset.aggregate(value=Count('id'))['value']


def yard_total(queryset):
    return queryset.aggregate(value=Sum('yard'))['value'] or ZERO


def po_rolls(po):
    """Include unallocated invoice-only historical PO links without duplicating rolls."""
    return Roll.objects.filter(Q(alokasi__po=po) | Q(alokasi__isnull=True, invoice_po__po=po))


def production_material(po, color, material=None):
    source_ids = list(
        po_rolls(po).filter(color=color).values_list('material_id', flat=True).distinct()
    )
    if not source_ids:
        raise ValidationError('Warna belum memiliki roll di PO ini.')
    if material is None:
        if len(source_ids) != 1:
            raise ValidationError(
                f'Pilih bahan untuk warna {color.name}; warna ini dipakai beberapa bahan di PO.'
            )
        material_id = source_ids[0]
    else:
        try:
            material_id = material.pk if isinstance(material, Master) else int(material)
        except (TypeError, ValueError):
            raise ValidationError('Pilih bahan yang terkait PO dan warna ini.')
        if material_id not in source_ids:
            raise ValidationError('Bahan belum memiliki roll untuk warna ini di PO.')
    found = Master.objects.filter(pk=material_id, kind=Master.Kind.MATERIAL).first()
    if not found:
        raise ValidationError('Pilih bahan yang terkait PO dan warna ini.')
    return found


def require_mapped_production(po, color):
    if Hasil.objects.filter(po=po, color=color, material=None).exists() or (
        KirimGudang.objects.filter(po=po, color=color, material=None).exists()
    ):
        raise ValidationError(
            f'Petakan bahan pada data hasil/kiriman lama warna {color.name} '
            'sebelum mencatat hasil atau kiriman baru.'
        )


def yard_po(po, warna, material=None):
    material = production_material(po, warna, material)
    return yard_total(
        po_rolls(po).filter(
            material=material, color=warna, status__in=['dikirim', 'diterima', 'terpakai']
        )
    )


def done(po, warna, material=None):
    material = production_material(po, warna, material)
    hasil = (
        Hasil.objects.filter(po=po, material=material, color=warna)
        .values_list('pcs', flat=True)
        .first()
    )
    kirim = KirimGudang.objects.filter(po=po, material=material, color=warna).aggregate(
        total=Sum('pcs')
    )
    return hitung_done(hasil, kirim['total'])


def hitung_done(hasil, terkirim):
    return None if hasil is None else hasil - (terkirim or 0)


def pemakaian(po, warna, material=None):
    material = production_material(po, warna, material)
    hasil = (
        Hasil.objects.filter(po=po, material=material, color=warna)
        .values_list('pcs', flat=True)
        .first()
    )
    return hitung_pemakaian(yard_po(po, warna, material), hasil)


def hitung_pemakaian(yard, hasil):
    if not hasil:
        return None
    return (yard / Decimal(hasil)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _resolve_po(pairs, results, shipments, last_shipment):
    rows = []
    legacy_colors = {color_id for material_id, color_id in pairs if material_id is None}
    for pair, details in sorted(
        pairs.items(),
        key=lambda item: (
            item[0][0] is None,
            item[1]['material'].name.casefold() if item[1]['material'] else '',
            item[1]['color'].name.casefold(),
            item[0][0] or 0,
            item[0][1],
        ),
    ):
        material_id, color_id = pair
        material, color = details['material'], details['color']
        hasil = results.get(pair)
        shipped = shipments.get(pair, 0)
        delta = hitung_done(hasil, shipped)
        remaining = None if delta is None else max(delta, 0)
        over = 0 if delta is None else max(-delta, 0)
        if hasil is None:
            key, label = 'belum_ada_hasil', 'Belum ada hasil'
        elif over:
            key, label = 'lebih_kirim', f'Lebih kirim {over} pcs'
        elif remaining:
            key, label = 'kurang_kirim', f'Kurang kirim {remaining} pcs'
        elif hasil > 0:
            key, label = 'done', 'Done'
        else:
            key, label = 'belum_ada_produksi', 'Belum ada hasil produksi'
        if material_id is None:
            if key == 'done':
                key, label = 'belum_lengkap', 'Bahan belum dipetakan (data lama)'
            else:
                label += ' · Bahan belum dipetakan (data lama)'
        rows.append(
            {
                'key': pair,
                'material': material,
                'material_id': material_id,
                'color': color,
                'color_id': color_id,
                'hasil': hasil,
                'has_result': hasil is not None,
                'shipped': shipped,
                'terkirim': shipped,
                'remaining': remaining,
                'over': over,
                'status_key': key,
                'status_label': label,
                'unmapped_material': material_id is None,
                'has_unmapped_legacy': color_id in legacy_colors,
            }
        )
    remaining = sum(row['remaining'] or 0 for row in rows)
    over = sum(row['over'] for row in rows)
    missing = [row['color'] for row in rows if not row['has_result']]
    total_hasil = sum(results.values())
    if over:
        code, label = 'lebih_kirim', 'Lebih kirim'
    elif remaining:
        code, label = 'kurang_kirim', 'Kurang kirim'
    elif legacy_colors:
        code, label = 'belum_lengkap', 'Belum lengkap'
    elif not results:
        code, label = 'belum_ada_hasil', 'Belum ada hasil'
    elif missing:
        code, label = 'belum_lengkap', 'Belum lengkap'
    elif total_hasil == 0:
        code, label = 'belum_ada_produksi', 'Belum ada hasil produksi'
    else:
        code, label = 'done', 'Done'
    return {
        'code': code,
        'label': label,
        'status_key': code,
        'status_label': label,
        'remaining': remaining,
        'over': over,
        'hasil': total_hasil,
        'terkirim': sum(shipments.values()),
        'missing_results': missing,
        'is_done': code == 'done',
        'unmapped_material': bool(legacy_colors),
        'last_shipment': last_shipment,
        'rows': rows,
    }


def po_status_many(pos):
    """One shared resolver, with a bounded number of queries for lists and exports."""
    ids = [item.pk if isinstance(item, Po) else int(item) for item in pos]
    if not ids:
        return {}
    pair_ids = {po_id: set() for po_id in ids}
    rolls = Roll.objects.filter(
        Q(alokasi__po_id__in=ids) | Q(alokasi__isnull=True, invoice_po__po_id__in=ids)
    ).values_list('alokasi__po_id', 'invoice_po__po_id', 'material_id', 'color_id')
    for allocation_po, legacy_po, material_id, color_id in rolls:
        pair_ids[allocation_po or legacy_po].add((material_id, color_id))
    results = {po_id: {} for po_id in ids}
    sized_items = {}
    for result_id, po_id, material_id, color_id, pcs, complete in Hasil.objects.filter(
        po_id__in=ids
    ).values_list('id', 'po_id', 'material_id', 'color_id', 'pcs', 'sizes_complete'):
        pair = (material_id, color_id)
        results[po_id][pair] = pcs
        if complete:
            sized_items[result_id] = (po_id, pair)
        pair_ids[po_id].add(pair)
    shipments = {po_id: {} for po_id in ids}
    last = {po_id: None for po_id in ids}
    totals = (
        KirimGudang.objects.filter(po_id__in=ids)
        .values('po_id', 'material_id', 'color_id')
        .annotate(total=Sum('pcs'), latest=Max('tanggal'))
    )
    for item in totals:
        po_id = item['po_id']
        pair = (item['material_id'], item['color_id'])
        shipments[po_id][pair] = item['total']
        pair_ids[po_id].add(pair)
        if last[po_id] is None or item['latest'] > last[po_id]:
            last[po_id] = item['latest']
    # Sized item totals are derived; the parent pcs remains an atomic compatibility cache.
    size_over = {}
    if sized_items:
        size_sent = dict(
            KirimUkuran.objects.filter(ukuran__hasil_id__in=sized_items)
            .values('ukuran_id')
            .annotate(total=Sum('pcs'))
            .values_list('ukuran_id', 'total')
        )
        for result_id, (po_id, pair) in sized_items.items():
            results[po_id][pair] = 0
        for size_id, result_id, pcs in HasilUkuran.objects.filter(
            hasil_id__in=sized_items
        ).values_list('id', 'hasil_id', 'pcs'):
            po_id, pair = sized_items[result_id]
            results[po_id][pair] += pcs
            extra = max(size_sent.get(size_id, 0) - pcs, 0)
            size_over[(po_id, pair)] = size_over.get((po_id, pair), 0) + extra
    master_ids = {
        master_id
        for pairs in pair_ids.values()
        for pair in pairs
        for master_id in pair
        if master_id is not None
    }
    all_masters = Master.objects.in_bulk(master_ids)
    summaries = {
        po_id: _resolve_po(
            {
                pair: {
                    'material': all_masters[pair[0]] if pair[0] is not None else None,
                    'color': all_masters[pair[1]],
                }
                for pair in pair_ids[po_id]
            },
            results[po_id],
            shipments[po_id],
            last[po_id],
        )
        for po_id in ids
    }
    for po_id, summary in summaries.items():
        for row in summary['rows']:
            extra = size_over.get((po_id, row['key']), 0)
            if extra > row['over']:
                summary['over'] += extra - row['over']
                row['over'] = extra
                row['status_key'], row['status_label'] = 'lebih_kirim', f'Lebih kirim {extra} pcs'
        if summary['over']:
            summary.update(
                code='lebih_kirim',
                label='Lebih kirim',
                status_key='lebih_kirim',
                status_label='Lebih kirim',
                is_done=False,
            )
    return summaries


def po_status(po):
    return po_status_many([po])[po.pk]


def po_balance(po):
    return po_status(po)['is_done']


def _ids(values):
    try:
        ids = list(
            dict.fromkeys(item.pk if isinstance(item, Roll) else int(item) for item in values)
        )
    except (TypeError, ValueError):
        raise ValidationError('Pilihan roll tidak valid.')
    if not ids:
        raise ValidationError('Pilih setidaknya satu roll.')
    return ids


@transaction.atomic
def pindah_status(rolls, ke, user, **kolom):
    require_purchasing(user)
    ids = _ids(rolls)
    locked = list(
        Roll.objects.select_for_update(of=('self',))
        .select_related('invoice', 'material', 'color', 'alokasi__po')
        .filter(pk__in=ids)
        .order_by('pk')
    )
    if len(locked) != len(ids):
        raise ValidationError('Pilihan roll tidak ditemukan.')
    reason = kolom.get('alasan', '').strip()
    for roll in locked:
        if ke not in TRANSITIONS.get(roll.status, set()):
            raise ValidationError(
                f'Roll #{roll.urut} baru saja berubah status ke {roll.get_status_display()}. '
                'Muat ulang halaman.'
            )
        if ke == 'menunggu' and (roll.alokasi_id or roll.invoice.dibatalkan):
            raise ValidationError(f'Roll #{roll.urut} sudah dialokasikan atau invoice dibatalkan.')
        if ke == 'tersedia' and roll.alokasi_id and roll.alokasi.po_id:
            raise ValidationError(
                'Roll yang sudah terhubung PO tidak dapat dilepas melalui aksi biasa.'
            )
        if (roll.status, ke) == ('siap_kirim', 'tersedia') and not reason:
            raise ValidationError('Alasan wajib diisi.')
        if ke == 'dikirim':
            kolom['sj_kirim'] = limited_text(
                kolom.get('sj_kirim', ''), 'Surat jalan pengiriman kain', 80
            )
            valid_date(kolom.get('tgl_kirim'), 'Tanggal kirim kain')
            if kolom['tgl_kirim'] < roll.invoice.tanggal:
                raise ValidationError('Tanggal kirim tidak boleh sebelum tanggal invoice.')
        if ke == 'diterima':
            valid_date(kolom.get('tgl_terima'), 'Tanggal terima bahan')
            if roll.tgl_kirim is None:
                raise ValidationError(f'Roll #{roll.urut} belum memiliki tanggal kirim kain.')
            if kolom['tgl_terima'] < roll.tgl_kirim:
                raise ValidationError('Tanggal terima tidak boleh sebelum tanggal kirim.')
    for roll in locked:
        before = roll.status
        roll.status = ke
        if ke == 'menunggu':
            roll.alokasi = kolom['alokasi']
        if ke == 'tersedia':
            roll.alokasi = None
            roll.tgl_kirim = None
            roll.sj_kirim = ''
            roll.tgl_terima = None
        if ke == 'dikirim':
            roll.tgl_kirim = kolom['tgl_kirim']
            roll.sj_kirim = kolom.get('sj_kirim', '')
        if ke == 'diterima':
            roll.tgl_terima = kolom['tgl_terima']
        roll.save()
        log(
            user,
            f'{before} → {ke}',
            f'Roll {roll.color.name} #{roll.urut} {roll.invoice.nomor}',
            reason,
        )
    return locked


@transaction.atomic
def ajukan_alokasi(roll_ids, cmt, user):
    require_purchasing(user)
    # Serialize CMT activation checks while permitting FK references from a concurrent PO.
    cmt = Master.objects.select_for_update(no_key=True).get(pk=cmt.pk)
    if cmt.kind != Master.Kind.CMT or not cmt.active:
        raise ValidationError('Pilih CMT aktif dari daftar.')
    ids = _ids(roll_ids)
    invoice_ids = list(
        Roll.objects.filter(pk__in=ids).values_list('invoice_id', flat=True).distinct()
    )
    if len(invoice_ids) != 1:
        raise ValidationError('Pilih roll dari satu invoice untuk setiap pengajuan.')
    invoice = Invoice.objects.select_for_update().get(pk=invoice_ids[0])
    if invoice.dibatalkan:
        raise ValidationError('Invoice yang dibatalkan tidak dapat dialokasikan.')
    locked = list(
        Roll.objects.select_for_update(of=('self',))
        .select_related('invoice')
        .filter(pk__in=ids)
        .order_by('pk')
    )
    if len(locked) != len(ids):
        raise ValidationError('Ada roll yang tidak ditemukan.')
    if len({roll.invoice_id for roll in locked}) != 1:
        raise ValidationError('Pilih roll dari satu invoice untuk setiap pengajuan.')
    for roll in locked:
        if roll.status != 'tersedia' or roll.alokasi_id:
            raise ValidationError(f'Roll #{roll.urut} baru saja dialokasikan. Muat ulang halaman.')
        if roll.invoice.dibatalkan:
            raise ValidationError('Invoice yang dibatalkan tidak dapat dialokasikan.')
    alokasi = Alokasi.objects.create(cmt=cmt, dibuat_oleh=user)
    AlokasiRoll.objects.bulk_create([AlokasiRoll(alokasi=alokasi, roll=roll) for roll in locked])
    pindah_status(ids, 'menunggu', user, alokasi=alokasi)
    log(user, 'ajukan', f'Alokasi #{alokasi.pk}', f'{cmt.name} · {len(ids)} roll; PO belum diisi')
    return alokasi


@transaction.atomic
def putuskan_alokasi(alokasi, aksi, user, alasan='', tgl_kirim=None, sj_kirim=''):
    require_purchasing(user)
    alokasi = (
        Alokasi.objects.select_for_update(of=('self',)).select_related('cmt').get(pk=alokasi.pk)
    )
    if aksi == 'acc' and alokasi.status == 'disetujui':
        return alokasi
    ids = list(Roll.objects.filter(alokasi=alokasi).order_by('pk').values_list('pk', flat=True))
    if aksi == 'acc' and alokasi.status == 'menunggu':
        if not alokasi.cmt.active or alokasi.cmt.kind != 'cmt':
            raise ValidationError('CMT tujuan sudah nonaktif atau tidak valid.')
        if tgl_kirim is None:
            tgl_kirim = timezone.localdate()
        sj_kirim = limited_text(sj_kirim, 'Surat jalan pengiriman kain', 80)
        pindah_status(ids, 'dikirim', user, tgl_kirim=tgl_kirim, sj_kirim=sj_kirim)
        alokasi.status = 'disetujui'
        alokasi.acc_oleh = user
        alokasi.acc_pada = timezone.now()
        alokasi.tgl_kirim, alokasi.sj_kirim = tgl_kirim, sj_kirim
    elif aksi in ('tolak', 'batalkan'):
        if aksi == 'tolak' and alokasi.status != 'menunggu':
            raise ValidationError('Hanya alokasi menunggu yang dapat ditolak.')
        if alokasi.status not in ('menunggu', 'disetujui'):
            raise ValidationError('Aksi alokasi tidak sesuai status.')
        if not alasan.strip():
            raise ValidationError('Alasan wajib diisi.')
        if alokasi.po_id:
            raise ValidationError(
                'Alokasi sudah terhubung PO dan tidak dapat dibatalkan melalui aksi biasa.'
            )
        if (
            Roll.objects.filter(alokasi=alokasi)
            .exclude(status__in=['menunggu', 'siap_kirim'])
            .exists()
        ):
            raise ValidationError(
                'Roll sudah dikirim atau diterima dan tidak dapat dibatalkan melalui aksi biasa.'
            )
        pindah_status(ids, 'tersedia', user, alasan=alasan)
        alokasi.status = 'ditolak' if aksi == 'tolak' else 'dibatalkan'
        alokasi.alasan = alasan.strip()
    else:
        raise ValidationError('Aksi alokasi tidak sesuai status.')
    alokasi.save()
    detail = (
        f'Tanggal kirim kain {tgl_kirim}; surat jalan {sj_kirim or "—"}'
        if aksi == 'acc'
        else alasan
    )
    log(user, aksi, f'Alokasi #{alokasi.pk}', detail)
    return alokasi


@transaction.atomic
def kirim_alokasi_legacy(alokasi, roll_ids, tgl_kirim, user, sj_kirim=''):
    require_purchasing(user)
    alokasi = Alokasi.objects.select_for_update().get(pk=alokasi.pk)
    ids = _ids(roll_ids)
    sj_kirim = limited_text(sj_kirim, 'Surat jalan pengiriman kain', 80)
    if alokasi.status != 'disetujui':
        raise ValidationError('Hanya alokasi legacy yang disetujui dapat dicatat kirim.')
    if Roll.objects.filter(alokasi=alokasi, pk__in=ids, status='siap_kirim').count() != len(ids):
        raise ValidationError('Pilih hanya roll Siap kirim pada pengiriman legacy ini.')
    pindah_status(ids, 'dikirim', user, tgl_kirim=tgl_kirim, sj_kirim=sj_kirim)
    log(user, 'kirim legacy', f'Alokasi #{alokasi.pk}', f'{len(ids)} roll; {tgl_kirim}')
    return alokasi


@transaction.atomic
def terima_alokasi(alokasi, roll_ids, tgl_terima, user):
    require_purchasing(user)
    alokasi = Alokasi.objects.select_for_update().get(pk=alokasi.pk)
    ids = _ids(roll_ids)
    if alokasi.status != 'disetujui':
        raise ValidationError('Pengiriman belum di-ACC.')
    if Roll.objects.filter(alokasi=alokasi, pk__in=ids).count() != len(ids):
        raise ValidationError('Pilihan roll bukan bagian dari pengiriman ini.')
    selected = list(Roll.objects.select_for_update(of=('self',)).filter(pk__in=ids).order_by('pk'))
    # A browser retry with exactly the saved receipt date is harmless.
    if all(roll.status == 'diterima' and roll.tgl_terima == tgl_terima for roll in selected):
        return selected
    if any(roll.status != 'dikirim' for roll in selected):
        raise ValidationError('Hanya roll Dikirim yang dapat dicatat sebagai Diterima.')
    received = pindah_status(ids, 'diterima', user, tgl_terima=tgl_terima)
    log(user, 'terima CMT', f'Alokasi #{alokasi.pk}', f'{len(ids)} roll; {tgl_terima}')
    return received


@transaction.atomic
def tautkan_po(alokasi, nomor_po, user, produk=''):
    require_purchasing(user)
    alokasi = (
        Alokasi.objects.select_for_update(of=('self',))
        .select_related('po', 'cmt')
        .get(pk=alokasi.pk)
    )
    nomor = normalize_po(nomor_po)
    produk = ' '.join(produk.split())
    if not nomor or len(nomor) > 80:
        raise ValidationError('Nomor PO wajib diisi, maksimal 80 karakter.')
    if len(produk) > 160:
        raise ValidationError('Nama produk maksimal 160 karakter.')
    if alokasi.po_id:
        if alokasi.po.nomor == nomor:
            return alokasi.po
        raise ValidationError(f'Pengiriman ini sudah terhubung ke {alokasi.po.nomor}.')
    rolls = list(
        Roll.objects.select_for_update(of=('self',)).filter(alokasi=alokasi).order_by('pk')
    )
    if (
        alokasi.status != 'disetujui'
        or not rolls
        or any(roll.status != 'diterima' for roll in rolls)
    ):
        raise ValidationError('Nomor PO baru dapat diisi setelah seluruh roll pengiriman Diterima.')
    if not alokasi.cmt.active or alokasi.cmt.kind != 'cmt':
        raise ValidationError('CMT tujuan sudah nonaktif atau tidak valid.')
    po, created = Po.objects.get_or_create(
        nomor=nomor, defaults={'cmt': alokasi.cmt, 'produk': produk}
    )
    po = Po.objects.select_for_update().get(pk=po.pk)
    if po.cmt_id != alokasi.cmt_id:
        if po.cmt_id is None:
            raise ValidationError(
                f'{po.nomor} adalah PO historis dengan CMT yang belum dipetakan. '
                'Periksa riwayatnya.'
            )
        raise ValidationError(f'{po.nomor} sudah dimiliki CMT lain.')
    alokasi.po = po
    alokasi.save(update_fields=['po'])
    log(
        user,
        'isi PO' if created else 'tautkan PO',
        f'Alokasi #{alokasi.pk}',
        f'{po.nomor} · {alokasi.cmt.name}',
    )
    return po


@transaction.atomic
def ubah_produk_po(po, produk, user, cmt=None):
    require_purchasing(user)
    po = Po.objects.select_for_update().get(pk=po.pk)
    if cmt is not None and po.cmt_id != cmt.pk:
        raise ValidationError('PO tidak dimiliki CMT ini.')
    produk = ' '.join(produk.split())
    if len(produk) > 160:
        raise ValidationError('Nama produk maksimal 160 karakter.')
    if po.produk != produk:
        before = po.produk
        po.produk = produk
        po.save(update_fields=['produk'])
        log(user, 'ubah produk', po.nomor, f'{before or "—"} → {produk or "—"}')
    return po


def _invoice_panels(groups):
    """Accept new nested input and legacy yard imports, never create a PO."""
    panels = []
    for group in groups:
        if group.get('po_number'):
            raise ValidationError('Nomor PO diisi melalui CMT setelah bahan diterima.')
        if 'rows' in group:
            panels.append({'material': group['material'], 'rows': group['rows']})
        elif group.get('rolls'):
            # Historic import overrides are grouped by their actual material/color/location.
            by_material = {}
            for item in group['rolls']:
                name = item['material']
                key = ' '.join(name.split()).casefold()
                panel = by_material.setdefault(key, {'material': name, 'rows': {}})
                row_key = (
                    item['color'].strip().casefold(),
                    item.get('lokasi', '').strip().casefold(),
                )
                row = panel['rows'].setdefault(
                    row_key, {'color': item['color'], 'lokasi': item.get('lokasi', ''), 'yards': []}
                )
                row['yards'].append(item['yard'])
            panels.extend(
                {'material': panel['material'], 'rows': list(panel['rows'].values())}
                for panel in by_material.values()
            )
        else:
            panels.append(
                {
                    'material': group['material'],
                    'rows': [
                        {
                            'color': group['color'],
                            'lokasi': group.get('lokasi', ''),
                            'yards': group['yards'],
                        }
                    ],
                }
            )
    return panels


def _invoice_fingerprint(data, panels, user, invoice):
    metadata = {
        key: data[key]
        for key in ('vendor', 'nomor', 'surat_jalan', 'tanggal', 'total_rp', 'existing_roll_input')
        if key in data
    }
    upload = data.get('invoice_file')
    if upload:
        position = upload.tell()
        upload.seek(0)
        digest = hashlib.sha256(upload.read()).hexdigest()
        upload.seek(position)
        metadata['attachment'] = {
            'filename': upload.name,
            'size': upload.size,
            'content_type': upload.verified_content_type,
            'sha256': digest,
        }
    payload = {
        'invoice_id': getattr(invoice, 'pk', None),
        'user_id': user.pk,
        'metadata': metadata,
        'panels': panels,
    }
    content = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def _invoice_retry(request_id, fingerprint):
    if not request_id:
        return None
    previous = InvoiceWrite.objects.select_related('invoice').filter(request_id=request_id).first()
    if previous:
        if previous.fingerprint != fingerprint:
            raise ValidationError(
                'Identitas permintaan invoice sudah digunakan untuk data berbeda.'
            )
        return previous.invoice
    return None


@transaction.atomic
def simpan_invoice(data, groups, user, invoice=None):
    require_purchasing(user)
    panels = _invoice_panels(groups)
    if invoice is None and not panels:
        raise ValidationError('Isi minimal satu bahan, satu warna, dan satu roll.')
    if invoice:
        # Editing and cancellation both lock the invoice before reserving roll rows.
        # This also keeps the outer edit transaction (ubah_roll -> simpan) in one order.
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.dibatalkan:
            raise ValidationError('Invoice yang dibatalkan tidak dapat diubah.')
    request_id = data.get('request_id')
    if request_id == '':
        request_id = None
    if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 64):
        raise ValidationError('Identitas permintaan invoice tidak valid.')
    fingerprint = _invoice_fingerprint(data, panels, user, invoice) if request_id else None
    repeated = _invoice_retry(request_id, fingerprint)
    if repeated:
        return repeated
    vendor = master('vendor', data['vendor'])
    # Serializes duplicate invoice numbers for the same vendor, including case variants.
    vendor = Master.objects.select_for_update(no_key=True).get(pk=vendor.pk)
    # A concurrent creation may finish while this request waits for the vendor lock.
    repeated = _invoice_retry(request_id, fingerprint)
    if repeated:
        return repeated
    nomor = ' '.join(data['nomor'].split())
    valid_date(data['tanggal'], 'Tanggal invoice')
    if not nomor or len(nomor) > 80:
        raise ValidationError('Nomor invoice wajib diisi, maksimal 80 karakter.')
    if data.get('total_rp', ZERO) < 0:
        raise ValidationError('Total rupiah tidak boleh negatif.')
    if 'surat_jalan' in data:
        invoice_delivery_note = limited_text(data['surat_jalan'], 'Surat jalan invoice', 80)
    if (
        Invoice.objects.filter(vendor=vendor, nomor__iexact=nomor)
        .exclude(pk=getattr(invoice, 'pk', None))
        .exists()
    ):
        raise ValidationError('Nomor invoice sudah dipakai vendor ini.')
    if invoice:
        allocated = (
            invoice.roll_set.exclude(status='tersedia').exists()
            or invoice.roll_set.filter(alokasi__isnull=False).exists()
        )
        if allocated and (
            invoice.vendor_id != vendor.pk
            or invoice.nomor != nomor
            or invoice.tanggal != data['tanggal']
        ):
            raise ValidationError('Vendor, nomor, dan tanggal terkunci setelah alokasi.')
        if data.get('existing_roll_input'):
            ubah_roll_invoice(invoice, data['existing_roll_input'], user)
    else:
        invoice = Invoice(dibuat_oleh=user)
    for key in ('surat_jalan', 'tanggal', 'total_rp'):
        if key in data:
            setattr(invoice, key, invoice_delivery_note if key == 'surat_jalan' else data[key])
    invoice.nomor, invoice.vendor = nomor, vendor
    invoice.save()
    upload = data.get('invoice_file')
    if upload:
        filename = upload.name.replace('\\', '/').rsplit('/', 1)[-1][:180]
        InvoiceAttachment.objects.update_or_create(
            invoice=invoice,
            defaults={
                'filename': filename,
                'content_type': upload.verified_content_type,
                'size': upload.size,
                'content': upload.read(),
            },
        )
    next_group = invoice.material_groups.aggregate(value=Max('urut'))['value'] or 0
    if panels and invoice.material_groups.count() + len(panels) > 10:
        raise ValidationError('Satu invoice maksimal berisi 10 panel bahan.')
    pending, next_urut, resolved = [], {}, {}

    def resolve(kind, name):
        key = (kind, ' '.join(name.split()).casefold())
        if key not in resolved:
            resolved[key] = master(kind, name)
        return resolved[key]

    for panel in panels:
        material = resolve('material', panel['material'])
        if not panel['rows']:
            raise ValidationError('Setiap bahan wajib memiliki minimal satu baris warna.')
        next_group += 1
        material_group = InvoiceMaterial.objects.create(
            invoice=invoice, material=material, urut=next_group
        )
        for row_no, row in enumerate(panel['rows'], 1):
            color = resolve('color', row['color'])
            lokasi = resolve('warehouse', row['lokasi']) if row.get('lokasi', '').strip() else None
            if not row.get('yards'):
                raise ValidationError(
                    f'{material.name} / {color.name}: isi minimal satu yard roll.'
                )
            color_row = InvoiceColor.objects.create(
                group=material_group, color=color, lokasi=lokasi, urut=row_no
            )
            key = (material.pk, color.pk)
            if key not in next_urut:
                next_urut[key] = (
                    invoice.roll_set.filter(material=material, color=color).aggregate(
                        value=Max('urut')
                    )['value']
                    or 0
                )
            for raw in row['yards']:
                try:
                    yard = Decimal(raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                except (InvalidOperation, TypeError, ValueError):
                    raise ValidationError(f'{material.name} / {color.name}: yard tidak valid.')
                if not yard.is_finite() or yard <= 0 or yard >= Decimal('100000000'):
                    raise ValidationError(
                        f'{material.name} / {color.name}: '
                        'yard harus positif dan kurang dari 100.000.000.'
                    )
                next_urut[key] += 1
                pending.append(
                    Roll(
                        invoice=invoice,
                        invoice_color=color_row,
                        material=material,
                        color=color,
                        lokasi=lokasi,
                        urut=next_urut[key],
                        yard=yard,
                    )
                )
    Roll.objects.bulk_create(pending)
    if request_id:
        try:
            with transaction.atomic():
                InvoiceWrite.objects.create(
                    invoice=invoice, request_id=request_id, fingerprint=fingerprint
                )
        except IntegrityError:
            if InvoiceWrite.objects.filter(request_id=request_id).exists():
                raise ValidationError(
                    'Identitas permintaan invoice sudah digunakan untuk data berbeda.'
                )
            raise
    log(user, 'simpan', f'Invoice {invoice.nomor}', f'{len(pending)} roll baru')
    return invoice


@transaction.atomic
def batalkan_invoice(invoice, user):
    require_purchasing(user)
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    rolls = list(
        Roll.objects.select_for_update(of=('self',)).filter(invoice=invoice).order_by('pk')
    )
    if any(roll.status != 'tersedia' or roll.alokasi_id for roll in rolls):
        raise ValidationError('Invoice dengan roll teralokasi tidak dapat dibatalkan.')
    invoice.dibatalkan = True
    invoice.save(update_fields=['dibatalkan'])
    log(user, 'batalkan', f'Invoice {invoice.nomor}')


@transaction.atomic
def ubah_roll_invoice(invoice, submitted, user):
    from .parsers import parse_number

    require_purchasing(user)
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.dibatalkan:
        raise ValidationError('Invoice yang dibatalkan tidak dapat diubah.')
    rolls = (
        Roll.objects.select_for_update(of=('self',))
        .select_related('material', 'color', 'lokasi', 'invoice_color__group')
        .filter(invoice=invoice)
        .order_by('pk')
    )
    changed = 0
    for roll in rolls:
        prefix = f'existing-{roll.pk}'
        if prefix not in submitted:
            continue
        try:
            yard = parse_number(submitted[prefix])
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError(f'Yard roll {roll.urut} tidak valid.')
        if not yard.is_finite() or yard <= 0 or yard >= Decimal('100000000'):
            raise ValidationError(
                f'Yard roll {roll.urut} harus positif dan kurang dari 100.000.000.'
            )
        material_name = submitted.get(prefix + '-material', roll.material.name)
        color_name = submitted.get(prefix + '-color', roll.color.name)
        location_name = submitted.get(prefix + '-lokasi', roll.lokasi.name if roll.lokasi else '')
        differs = yard != roll.yard or any(
            (
                material_name.strip().casefold() != roll.material.name.casefold(),
                color_name.strip().casefold() != roll.color.name.casefold(),
                location_name.strip().casefold()
                != (roll.lokasi.name.casefold() if roll.lokasi else ''),
            )
        )
        if not differs:
            continue
        if roll.status != 'tersedia' or roll.alokasi_id:
            raise ValidationError(f'Roll {roll.urut} sudah dialokasikan dan terkunci.')
        material = master('material', material_name)
        color = master('color', color_name)
        lokasi = master('warehouse', location_name) if location_name.strip() else None
        if (material.pk, color.pk) != (roll.material_id, roll.color_id):
            roll.urut = (
                Roll.objects.filter(invoice=invoice, material=material, color=color).aggregate(
                    value=Max('urut')
                )['value']
                or 0
            ) + 1
        # Reattach to a matching material/color row; preserve historical InvoicePo links.
        material_group = invoice.material_groups.filter(material=material).first()
        if not material_group:
            if invoice.material_groups.count() >= 10:
                raise ValidationError('Satu invoice maksimal berisi 10 panel bahan.')
            urut = (invoice.material_groups.aggregate(value=Max('urut'))['value'] or 0) + 1
            material_group = InvoiceMaterial.objects.create(
                invoice=invoice, material=material, urut=urut
            )
        color_row = material_group.color_rows.filter(color=color, lokasi=lokasi).first()
        if not color_row:
            urut = (material_group.color_rows.aggregate(value=Max('urut'))['value'] or 0) + 1
            color_row = InvoiceColor.objects.create(
                group=material_group, color=color, lokasi=lokasi, urut=urut
            )
        roll.material, roll.color, roll.lokasi, roll.yard = material, color, lokasi, yard
        roll.invoice_color = color_row
        roll.save()
        changed += 1
    if changed:
        log(user, 'ubah roll', f'Invoice {invoice.nomor}', f'{changed} roll')


def ubah_po_invoice(invoice, submitted, user):
    require_purchasing(user)
    if any(key.startswith('existing-po-') for key in submitted):
        raise ValidationError('Nomor PO diisi melalui CMT; hubungan PO historis tetap disimpan.')


@transaction.atomic
def simpan_hasil(po, color, pcs, user, material=None):
    require_purchasing(user)
    po = Po.objects.select_for_update().get(pk=po.pk)
    material = production_material(po, color, material)
    require_mapped_production(po, color)
    if not isinstance(pcs, int) or isinstance(pcs, bool) or pcs < 0:
        raise ValidationError('Hasil harus bilangan bulat tidak negatif.')
    shipped = (
        KirimGudang.objects.filter(po=po, material=material, color=color).aggregate(
            total=Sum('pcs')
        )['total']
        or 0
    )
    if pcs < shipped:
        raise ValidationError(
            f'Hasil {material.name} / {color.name} '
            f'tidak boleh lebih kecil dari total kiriman {shipped} pcs.'
        )
    before = (
        Hasil.objects.filter(po=po, material=material, color=color)
        .values_list('pcs', flat=True)
        .first()
    )
    if before == pcs:
        return Hasil.objects.get(po=po, material=material, color=color)
    item = Hasil.objects.filter(po=po, material=material, color=color).first()
    if item and item.sizes_complete:
        raise ValidationError(
            'Ubah hasil melalui rincian ukuran; total merupakan jumlah semua ukuran.'
        )
    if shipped:
        raise ValidationError(
            'Kiriman lama belum dirinci per ukuran; perubahan total hasil diblokir.'
        )
    hasil, _ = Hasil.objects.update_or_create(
        po=po, material=material, color=color, defaults={'pcs': pcs}
    )
    log(
        user,
        'hasil',
        po.nomor,
        f'{material.name} / {color.name}: '
        f'{before if before is not None else "belum diisi"} → {pcs} pcs',
    )
    return hasil


def kirim_gudang(po, color, data, user, material=None):
    from .size_services import kirim_ukuran

    payload = dict(data)
    rows = payload.pop('sizes', None)
    material = material if material is not None else payload.pop('material', None)
    return kirim_ukuran(po, color, material, payload, rows, user, payload.pop('hasil_id', None))


@transaction.atomic
def map_legacy_production(po, color, material, user):
    require_purchasing(user)
    po = Po.objects.select_for_update().get(pk=po.pk)
    if material is None:
        raise ValidationError('Pilih bahan tujuan untuk data hasil/kiriman lama.')
    material = production_material(po, color, material)
    legacy_results = Hasil.objects.filter(po=po, color=color, material=None)
    legacy_shipments = KirimGudang.objects.filter(po=po, color=color, material=None)
    result_count, shipment_count = legacy_results.count(), legacy_shipments.count()
    existing_result = Hasil.objects.filter(po=po, color=color, material=material).exists()
    existing_shipments = KirimGudang.objects.filter(po=po, color=color, material=material).exists()
    if not result_count and not shipment_count:
        if existing_result or existing_shipments:
            return {'hasil': 0, 'kiriman': 0, 'already_mapped': True}
        raise ValidationError('Tidak ada data hasil/kiriman lama untuk warna ini.')
    if existing_result or existing_shipments:
        raise ValidationError(
            f'{material.name} / {color.name} sudah memiliki hasil atau kiriman. '
            'Data lama tidak dapat digabung atau menimpa transaksi tersebut.'
        )
    result_pcs = legacy_results.aggregate(total=Sum('pcs'))['total'] or 0
    shipment_pcs = legacy_shipments.aggregate(total=Sum('pcs'))['total'] or 0
    legacy_results.update(material=material)
    legacy_shipments.update(material=material)
    log(
        user,
        'petakan bahan produksi',
        po.nomor,
        f'{color.name}: bahan belum dipetakan → {material.name}; '
        f'{result_count} hasil ({result_pcs} pcs), {shipment_count} kiriman ({shipment_pcs} pcs)',
    )
    return {'hasil': result_count, 'kiriman': shipment_count, 'already_mapped': False}


def selesaikan_po(po, user):
    require_purchasing(user)
    # Retained as an import-compatible check; manual completion no longer changes data.
    if not po_balance(po):
        raise ValidationError(
            'PO belum Done; status dihitung otomatis dari hasil dan kiriman per bahan dan warna.'
        )
    return po
