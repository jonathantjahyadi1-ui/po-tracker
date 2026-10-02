from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Max, Sum
from django.utils import timezone

from .models import (
    Alokasi,
    Hasil,
    Invoice,
    InvoiceAttachment,
    InvoicePo,
    KirimGudang,
    Log,
    Master,
    Po,
    Roll,
    normalize_po,
)

ZERO = Decimal('0.00')
TRANSITIONS = {
    'tersedia': {'menunggu'},
    'menunggu': {'siap_kirim', 'tersedia'},
    'siap_kirim': {'dikirim', 'tersedia'},
    'dikirim': {'diterima', 'siap_kirim'},
    'diterima': {'terpakai', 'rusak', 'tersedia'},
    'terpakai': {'diterima'},
    'rusak': {'diterima'},
}


def log(user, aksi, objek, detail=''):
    return Log.objects.create(user=user, aksi=aksi, objek=objek, detail=detail)


def valid_date(value, label):
    if value is None or value > timezone.localdate() + timedelta(days=1):
        raise ValidationError(f'{label} wajib diisi dan tidak boleh lebih dari besok.')


def master(kind, name):
    if kind not in Master.Kind.values:
        raise ValidationError('Jenis master tidak dikenal.')
    name = ' '.join(name.split())
    if not name:
        raise ValidationError('Nama wajib diisi.')
    found = Master.objects.filter(kind=kind, name__iexact=name).first()
    if found:
        if not found.active:
            raise ValidationError(f'{found.name} sudah nonaktif.')
        return found
    return Master.objects.create(kind=kind, name=name)


def roll_count(queryset):
    return queryset.aggregate(value=Count('id'))['value']


def yard_total(queryset):
    return queryset.aggregate(value=Sum('yard'))['value'] or ZERO


def yard_po(po, warna):
    return yard_total(
        Roll.objects.filter(
            alokasi__po=po, color=warna, status__in=['dikirim', 'diterima', 'terpakai']
        )
    )


def done(po, warna):
    hasil = Hasil.objects.filter(po=po, color=warna).values_list('pcs', flat=True).first()
    kirim = KirimGudang.objects.filter(po=po, color=warna).aggregate(total=Sum('pcs'))
    return hitung_done(hasil, kirim['total'])


def hitung_done(hasil, terkirim):
    return (hasil or 0) - (terkirim or 0)


def pemakaian(po, warna):
    hasil = Hasil.objects.filter(po=po, color=warna).values_list('pcs', flat=True).first()
    return hitung_pemakaian(yard_po(po, warna), hasil)


def hitung_pemakaian(yard, hasil):
    if not hasil:
        return None
    return (yard / Decimal(hasil)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def po_balance(po):
    rows = list(Hasil.objects.filter(po=po).values_list('color_id', 'pcs'))
    if not rows:
        return False
    colors = set(Roll.objects.filter(alokasi__po=po).values_list('color_id', flat=True).distinct())
    if colors != {color_id for color_id, _ in rows}:
        return False
    shipped = dict(
        KirimGudang.objects.filter(po=po)
        .values('color_id')
        .annotate(total=Sum('pcs'))
        .values_list('color_id', 'total')
    )
    return all(pcs == shipped.get(color_id, 0) for color_id, pcs in rows)


@transaction.atomic
def pindah_status(rolls, ke, user, **kolom):
    ids = [item.pk if isinstance(item, Roll) else int(item) for item in rolls]
    # PostgreSQL cannot lock the nullable tables joined by select_related.
    locked = list(
        Roll.objects.select_for_update(of=('self',))
        .select_related('invoice', 'material', 'color', 'alokasi__po')
        .filter(pk__in=ids)
        .order_by('pk')
    )
    if not ids or len(locked) != len(set(ids)):
        raise ValidationError('Pilihan roll tidak ditemukan.')
    reason = kolom.get('alasan', '').strip()
    for roll in locked:
        if roll.alokasi_id and roll.alokasi.po.selesai:
            raise ValidationError('PO sudah selesai.')
        if ke not in TRANSITIONS.get(roll.status, set()):
            po = roll.alokasi.po.nomor if roll.alokasi_id else '-'
            raise ValidationError(
                f'Roll #{roll.urut} baru saja berubah status ke {roll.get_status_display()} '
                f'untuk {po}. Muat ulang halaman.'
            )
        if ke == 'menunggu' and roll.alokasi_id:
            raise ValidationError(f'Roll #{roll.urut} sudah dialokasikan.')
        if (roll.status, ke) in {
            ('siap_kirim', 'tersedia'),
            ('dikirim', 'siap_kirim'),
            ('terpakai', 'diterima'),
            ('rusak', 'diterima'),
        } and not reason:
            raise ValidationError('Alasan wajib diisi.')
        if ke == 'rusak' and not kolom.get('catatan', '').strip():
            raise ValidationError('Catatan kerusakan wajib diisi.')
        if ke == 'dikirim':
            valid_date(kolom.get('tgl_kirim'), 'Tanggal kirim')
            if kolom['tgl_kirim'] < roll.invoice.tanggal:
                raise ValidationError('Tanggal kirim tidak boleh sebelum tanggal invoice.')
        if ke == 'diterima' and roll.status == 'dikirim':
            valid_date(kolom.get('tgl_terima'), 'Tanggal terima')
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
        if before == 'dikirim' and ke == 'siap_kirim':
            roll.tgl_kirim = None
            roll.sj_kirim = ''
        if ke == 'diterima' and before == 'dikirim':
            roll.tgl_terima = kolom['tgl_terima']
        if ke == 'rusak':
            roll.catatan = kolom['catatan']
        roll.save()
        log(
            user,
            f'{before} → {ke}',
            f'Roll {roll.color.name} #{roll.urut} {roll.invoice.nomor}',
            reason,
        )
    return locked


@transaction.atomic
def ajukan_alokasi(roll_ids, nomor_po, cmt, user):
    if cmt.kind != Master.Kind.CMT or not cmt.active:
        raise ValidationError('Pilih CMT aktif dari daftar.')
    nomor = normalize_po(nomor_po)
    if not nomor:
        raise ValidationError('Nomor PO wajib diisi.')
    ids = list(dict.fromkeys(int(value) for value in roll_ids))
    if not ids:
        raise ValidationError('Pilih setidaknya satu roll.')
    locked = list(
        Roll.objects.select_for_update(of=('self',))
        .select_related('alokasi__po', 'invoice_po__po')
        .filter(pk__in=ids)
        .order_by('pk')
    )
    if len(locked) != len(ids):
        raise ValidationError('Ada roll yang tidak ditemukan.')
    for roll in locked:
        if roll.status != 'tersedia' or roll.alokasi_id:
            po = roll.alokasi.po.nomor if roll.alokasi_id else 'PO lain'
            raise ValidationError(f'Roll #{roll.urut} baru saja dialokasikan ke {po}.')
    groups = {roll.invoice_po_id: roll.invoice_po for roll in locked if roll.invoice_po_id}
    if len(groups) > 1 or (groups and any(not roll.invoice_po_id for roll in locked)):
        raise ValidationError('Pilih roll dari satu grup PO untuk setiap pengajuan.')
    po, _ = Po.objects.get_or_create(nomor=nomor)
    if po.selesai:
        raise ValidationError('PO sudah selesai.')
    if groups:
        group = next(iter(groups.values()))
        if group.po_id and group.po_id != po.pk:
            raise ValidationError(f'Roll pada PO {group.urut} sudah terkait ke {group.po.nomor}.')
        if InvoicePo.objects.filter(invoice=group.invoice, po=po).exclude(pk=group.pk).exists():
            raise ValidationError(f'{po.nomor} sudah dipakai grup PO lain di invoice ini.')
        if not group.po_id:
            group.po = po
            group.save(update_fields=['po'])
    alokasi = Alokasi.objects.create(po=po, cmt=cmt, dibuat_oleh=user)
    pindah_status(ids, 'menunggu', user, alokasi=alokasi)
    log(user, 'ajukan', f'Alokasi #{alokasi.pk}', f'{po.nomor} · {cmt.name}')
    return alokasi


@transaction.atomic
def putuskan_alokasi(alokasi, aksi, user, alasan=''):
    alokasi = Alokasi.objects.select_for_update().get(pk=alokasi.pk)
    ids = list(Roll.objects.filter(alokasi=alokasi).values_list('id', flat=True))
    if aksi == 'acc' and alokasi.status == 'menunggu':
        pindah_status(ids, 'siap_kirim', user)
        alokasi.status = 'disetujui'
        alokasi.acc_oleh = user
        alokasi.acc_pada = timezone.now()
    elif aksi in ('tolak', 'batalkan'):
        if aksi == 'tolak' and alokasi.status != 'menunggu':
            raise ValidationError('Hanya alokasi menunggu yang dapat ditolak.')
        if not alasan.strip():
            raise ValidationError('Alasan wajib diisi.')
        available = list(
            Roll.objects.filter(alokasi=alokasi, status__in=['menunggu', 'siap_kirim']).values_list(
                'id', flat=True
            )
        )
        if len(available) != len(ids):
            raise ValidationError('Roll sudah dikirim. Batalkan pengiriman lebih dahulu.')
        pindah_status(ids, 'tersedia', user, alasan=alasan)
        alokasi.status = 'ditolak' if aksi == 'tolak' else 'dibatalkan'
        alokasi.alasan = alasan.strip()
    else:
        raise ValidationError('Aksi alokasi tidak sesuai status.')
    alokasi.save()
    log(user, aksi, f'Alokasi #{alokasi.pk}', alasan)


@transaction.atomic
def simpan_invoice(data, groups, user, invoice=None):
    vendor = master('vendor', data['vendor'])
    nomor = ' '.join(data['nomor'].split())
    valid_date(data['tanggal'], 'Tanggal invoice')
    if not nomor:
        raise ValidationError('Nomor invoice wajib diisi.')
    if (
        Invoice.objects.filter(vendor=vendor, nomor__iexact=nomor)
        .exclude(pk=getattr(invoice, 'pk', None))
        .exists()
    ):
        raise ValidationError('Nomor invoice sudah dipakai vendor ini.')
    if invoice and invoice.dibatalkan:
        raise ValidationError('Invoice yang dibatalkan tidak dapat diubah.')
    if invoice:
        allocated = invoice.roll_set.exclude(status='tersedia').exists()
        if allocated and (
            invoice.vendor_id != vendor.pk
            or invoice.nomor != nomor
            or invoice.tanggal != data['tanggal']
        ):
            raise ValidationError('Vendor, nomor, dan tanggal terkunci setelah alokasi.')
    else:
        invoice = Invoice(dibuat_oleh=user)
    for key, value in data.items():
        if key not in ('vendor', 'invoice_file'):
            setattr(invoice, key, value)
    invoice.vendor = vendor
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
    pending = []
    next_urut = {}
    resolved = {}

    def resolve(kind, name):
        key = (kind, name.strip().casefold())
        if key not in resolved:
            resolved[key] = master(kind, name)
        return resolved[key]

    next_group = (
        invoice.po_groups.aggregate(value=Max('urut'))['value'] or 0
    )
    for group in groups:
        po_number = group.get('po_number', '')
        po = None
        if po_number:
            po, _ = Po.objects.get_or_create(nomor=normalize_po(po_number))
            if po.selesai:
                raise ValidationError(f'{po.nomor} sudah selesai dan tidak dapat ditambah roll.')
        invoice_po = (
            invoice.po_groups.filter(po=po).first() if po else None
        )
        if not invoice_po:
            next_group += 1
            if next_group > 10:
                raise ValidationError('Satu invoice maksimal berisi 10 grup PO.')
            invoice_po = InvoicePo.objects.create(invoice=invoice, urut=next_group, po=po)
        roll_data = group.get('rolls') or [
            {
                'yard': yard,
                'material': group['material'],
                'color': group['color'],
                'lokasi': group.get('lokasi', ''),
            }
            for yard in group['yards']
        ]
        for item in roll_data:
            material = resolve('material', item['material'])
            color = resolve('color', item['color'])
            lokasi = resolve('warehouse', item['lokasi']) if item.get('lokasi') else None
            key = (material.pk, color.pk)
            if key not in next_urut:
                next_urut[key] = (
                    invoice.roll_set.filter(material=material, color=color)
                    .aggregate(value=Max('urut'))['value']
                    or 0
                )
            next_urut[key] += 1
            pending.append(
                Roll(
                    invoice=invoice,
                    invoice_po=invoice_po,
                    material=material,
                    color=color,
                    lokasi=lokasi,
                    urut=next_urut[key],
                    yard=item['yard'],
                    catatan=group.get('catatan', ''),
                )
            )
    Roll.objects.bulk_create(pending)
    log(user, 'simpan', f'Invoice {invoice.nomor}', f'{len(pending)} roll baru')
    return invoice


@transaction.atomic
def batalkan_invoice(invoice, user):
    if invoice.roll_set.exclude(status='tersedia').exists():
        raise ValidationError('Invoice dengan roll teralokasi tidak dapat dibatalkan.')
    invoice.dibatalkan = True
    invoice.save(update_fields=['dibatalkan'])
    log(user, 'batalkan', f'Invoice {invoice.nomor}')


@transaction.atomic
def ubah_roll_invoice(invoice, submitted, user):
    from decimal import InvalidOperation

    from .parsers import parse_number

    rolls = (
        Roll.objects.select_for_update(of=('self',))
        .select_related('material', 'color', 'lokasi')
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
        except InvalidOperation:
            raise ValidationError(f'Yard roll {roll.urut} tidak valid.')
        material_name = submitted.get(prefix + '-material', roll.material.name)
        color_name = submitted.get(prefix + '-color', roll.color.name)
        location_name = submitted.get(prefix + '-lokasi', roll.lokasi.name if roll.lokasi else '')
        differs = (
            yard != roll.yard
            or material_name.strip().casefold() != roll.material.name.casefold()
            or color_name.strip().casefold() != roll.color.name.casefold()
            or location_name.strip().casefold()
            != (roll.lokasi.name.casefold() if roll.lokasi else '')
        )
        if not differs:
            continue
        if roll.status != 'tersedia':
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
        roll.material, roll.color, roll.lokasi, roll.yard = material, color, lokasi, yard
        roll.save()
        changed += 1
    if changed:
        log(user, 'ubah roll', f'Invoice {invoice.nomor}', f'{changed} roll')


@transaction.atomic
def ubah_po_invoice(invoice, submitted, user):
    changed = 0
    for group in invoice.po_groups.select_for_update(of=('self',)).select_related('po'):
        key = f'existing-po-{group.pk}'
        if key not in submitted:
            continue
        number = normalize_po(submitted[key])
        if len(number) > 80:
            raise ValidationError(f'PO {group.urut}: nomor maksimal 80 karakter.')
        if number == (group.po.nomor if group.po_id else ''):
            continue
        if group.rolls.exclude(status='tersedia').exists():
            raise ValidationError(f'PO {group.urut} sudah memiliki roll teralokasi dan terkunci.')
        po = None
        if number:
            po, _ = Po.objects.get_or_create(nomor=number)
            if po.selesai:
                raise ValidationError(f'{po.nomor} sudah selesai.')
            if invoice.po_groups.filter(po=po).exclude(pk=group.pk).exists():
                raise ValidationError(f'{po.nomor} sudah dipakai grup PO lain di invoice ini.')
        group.po = po
        group.save(update_fields=['po'])
        changed += 1
    if changed:
        log(user, 'ubah PO invoice', f'Invoice {invoice.nomor}', f'{changed} grup PO')


@transaction.atomic
def simpan_hasil(po, color, pcs, user):
    if po.selesai:
        raise ValidationError('PO sudah selesai.')
    if not Roll.objects.filter(alokasi__po=po, color=color).exists():
        raise ValidationError('Warna belum memiliki roll di PO ini.')
    if pcs < 0:
        raise ValidationError('Hasil tidak boleh negatif.')
    Hasil.objects.update_or_create(po=po, color=color, defaults={'pcs': pcs})
    log(user, 'hasil', po.nomor, f'{color.name}: {pcs} pcs')


@transaction.atomic
def kirim_gudang(po, color, data, user):
    if po.selesai:
        raise ValidationError('PO sudah selesai.')
    if not Roll.objects.filter(alokasi__po=po, color=color).exists():
        raise ValidationError('Warna belum memiliki roll di PO ini.')
    valid_date(data['tanggal'], 'Tanggal kirim gudang')
    if data['pcs'] <= 0:
        raise ValidationError('Jumlah pcs harus lebih dari 0.')
    KirimGudang.objects.create(po=po, color=color, **data)
    log(user, 'kirim gudang', po.nomor, f'{color.name}: {data["pcs"]} pcs')


@transaction.atomic
def selesaikan_po(po, user):
    if not po_balance(po):
        raise ValidationError('PO belum balance.')
    if Roll.objects.filter(alokasi__po=po, status__in=['dikirim', 'diterima']).exists():
        raise ValidationError('Masih ada roll dikirim atau diterima.')
    po.selesai = True
    po.save(update_fields=['selesai'])
    log(user, 'selesai', po.nomor)
