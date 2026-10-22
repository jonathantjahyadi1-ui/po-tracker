"""BS reserves yard once; the item itself is the source invoice adjustment."""

import hashlib
import json
import re
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import F, Sum
from django.utils import timezone

from .models import (
    Alokasi,
    FabricDefect,
    FabricDefectItem,
    Hasil,
    Invoice,
    KirimGudang,
    Master,
    ProductionUnit,
    Roll,
    VendorRefund,
)
from .money import CENT, MAX_MONEY, parse_money
from .payment_services import payment_summary
from .production_services import lock_po
from .services import ZERO, limited_text, lock_invoice, log, require_purchasing, valid_date


def parse_bs_yard(value):
    raw = format(value, 'f') if isinstance(value, Decimal) else str(value).strip()
    if not re.fullmatch(r'[0-9]+(?:[.,][0-9]{1,6})?', raw):
        raise ValidationError('Yard BS harus berupa angka, maksimal 6 desimal; contoh 12,5.')
    yard = Decimal(raw.replace(',', '.'))
    if yard <= 0 or yard >= Decimal('100000000'):
        raise ValidationError('Yard BS harus lebih dari nol dan kurang dari 100.000.000.')
    return yard.quantize(Decimal('0.000001'))


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def validate_request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,64}', value):
        raise ValidationError('Identitas permintaan tidak valid. Buka ulang form.')


def retry(model, request_id, digest):
    previous = model.objects.filter(request_id=request_id).first()
    if previous and previous.fingerprint != digest:
        raise ValidationError('Permintaan sudah digunakan untuk data berbeda. Buka ulang form.')
    return previous


def require_admin(user):
    if (
        not user.is_authenticated
        or not user.is_active
        or not (user.is_superuser or user.role == 'admin')
    ):
        raise PermissionDenied('Koreksi administratif hanya untuk Super Admin.')


def can_receive_refund(user):
    return (
        user.is_authenticated
        and user.is_active
        and (user.role == 'direktur' or user.is_superuser or user.role == 'admin')
    )


def set_bs_return_details(rolls):
    affected = [roll for roll in rolls if roll.bs_yard]
    returned = (
        dict(
            FabricDefectItem.objects.filter(
                roll_id__in={roll.pk for roll in affected},
                defect__cancelled_at__isnull=True,
                returned_at__isnull=False,
            )
            .values('roll_id')
            .annotate(total=Sum('yard'))
            .values_list('roll_id', 'total')
        )
        if affected
        else {}
    )
    for roll in affected:
        roll.returned_bs_yard = returned.get(roll.pk, ZERO)
        roll.pending_bs_yard = roll.bs_yard - roll.returned_bs_yard


def _lock_sources(roll_ids):
    """Allocation -> invoice -> roll -> PO, matching allocation/production writers."""
    sources = list(Roll.objects.filter(pk__in=roll_ids).values('pk', 'alokasi_id', 'invoice_id'))
    if len(sources) != len(roll_ids):
        raise ValidationError('Ada roll yang tidak ditemukan.')
    allocation_ids = sorted({r['alokasi_id'] for r in sources if r['alokasi_id']})
    if connection.vendor == 'sqlite':
        # Obtain SQLite's writer reservation before the first balance read.
        Invoice.objects.filter(pk=sources[0]['invoice_id']).update(total_rp=F('total_rp'))
    allocations = {
        a.pk: a
        for a in Alokasi.objects.filter(pk__in=allocation_ids).order_by('pk').select_for_update()
    }
    invoices = {
        invoice.pk: invoice
        for invoice in Invoice.objects.filter(pk__in={r['invoice_id'] for r in sources})
        .order_by('pk')
        .select_for_update()
    }
    rolls = list(
        Roll.objects.filter(pk__in=roll_ids)
        .order_by('pk')
        .select_for_update(of=('self',))
        .select_related('material', 'color', 'invoice_po')
    )
    expected = {r['pk']: (r['alokasi_id'], r['invoice_id']) for r in sources}
    for roll in rolls:
        if expected[roll.pk] != (roll.alokasi_id, roll.invoice_id):
            raise ValidationError('Alokasi roll baru saja berubah. Muat ulang form BS.')
    po_ids = {
        allocations[r.alokasi_id].po_id
        if r.alokasi_id
        else r.invoice_po.po_id
        if r.invoice_po_id
        else None
        for r in rolls
    } - {None}
    pos = {pk: lock_po(pk) for pk in sorted(po_ids)}
    return rolls, allocations, invoices, pos


def require_unused(roll, po):
    if po is None:
        return
    pair = {'po': po, 'material_id': roll.material_id, 'color_id': roll.color_id}
    if (
        po.selesai
        or po.legacy_completed
        or Hasil.objects.filter(**pair).exists()
        or Hasil.objects.filter(po=po, color_id=roll.color_id, material=None).exists()
        or KirimGudang.objects.filter(**pair).exists()
        or KirimGudang.objects.filter(po=po, color_id=roll.color_id, material=None).exists()
        or ProductionUnit.objects.filter(**pair, continuation='final').exists()
    ):
        raise ValidationError(
            f'Roll #{roll.urut}: bahan sudah memiliki hasil produksi/kiriman atau PO final. '
            'Yard belum terpakai tidak dapat dipastikan; selesaikan koreksi produksi dahulu.'
        )


@transaction.atomic
def record_defect(cmt, rows, date, note, request_id, user):
    require_purchasing(user)
    validate_request_id(request_id)
    valid_date(date, 'Tanggal BS')
    note = limited_text(note, 'Keterangan BS', 4000)
    if not note:
        raise ValidationError('Keterangan BS wajib diisi.')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
        raise ValidationError('Pilih 1 sampai 200 roll untuk setiap catatan BS.')
    amounts = {}
    for row in rows:
        try:
            pk = int(row['roll'])
            yard = parse_bs_yard(row['yard'])
        except (KeyError, TypeError, ValueError):
            raise ValidationError('Roll dan yard BS wajib diisi dengan benar.')
        if pk in amounts:
            raise ValidationError('Satu roll tidak boleh dipilih dua kali dalam catatan yang sama.')
        amounts[pk] = yard
    digest = fingerprint(
        {
            'cmt': cmt.pk,
            'rows': sorted(amounts.items()),
            'date': date,
            'note': note,
            'user': user.pk,
        }
    )
    previous = retry(FabricDefect, request_id, digest)
    if previous:
        return previous
    if not Master.objects.filter(pk=cmt.pk, kind='cmt').exists():
        raise ValidationError('CMT sumber tidak valid.')
    rolls, allocations, invoices, pos = _lock_sources(list(amounts))
    previous = retry(FabricDefect, request_id, digest)
    if previous:
        return previous
    pending, totals = [], {}
    summaries = {pk: payment_summary(invoice) for pk, invoice in invoices.items()}
    for roll in rolls:
        allocation = allocations.get(roll.alokasi_id)
        if (
            roll.status != 'diterima'
            or not allocation
            or allocation.cmt_id != cmt.pk
            or allocation.status != 'disetujui'
        ):
            raise ValidationError(f'Roll #{roll.urut} harus sudah diterima di CMT sumber.')
        if not roll.tgl_terima or date < roll.tgl_terima:
            raise ValidationError(f'Tanggal BS roll #{roll.urut} tidak boleh sebelum penerimaan.')
        invoice = invoices[roll.invoice_id]
        if invoice.dibatalkan or summaries[invoice.pk].status_key == 'reconciliation':
            raise ValidationError(f'Invoice {invoice.nomor} dibatalkan atau perlu rekonsiliasi.')
        po = pos.get(allocation.po_id)
        require_unused(roll, po)
        yard = amounts[roll.pk]
        if yard > roll.usable_yard:
            raise ValidationError(
                f'Roll #{roll.urut}: yard BS melebihi sisa layak pakai {roll.usable_yard} yard.'
            )
        price = roll.material.price_per_yard
        if price is None or price <= ZERO:
            raise ValidationError(
                f'Isi Harga per Yard di Master Bahan {roll.material.name} dahulu.'
            )
        amount = (yard * price).quantize(CENT, rounding=ROUND_HALF_UP)
        if amount <= ZERO or amount > MAX_MONEY:
            raise ValidationError('Nilai BS di luar batas nominal. Periksa yard dan harga bahan.')
        totals[invoice.pk] = totals.get(invoice.pk, ZERO) + amount
        pending.append(
            FabricDefectItem(
                roll=roll,
                invoice=invoice,
                vendor_id=invoice.vendor_id,
                po=po,
                yard=yard,
                price=price,
                amount=amount,
            )
        )
    for pk, amount in totals.items():
        if amount > summaries[pk].net_total:
            raise ValidationError(
                f'Nilai BS melampaui tagihan invoice {invoices[pk].nomor}. '
                'Periksa harga/sumber bahan.'
            )
    defect = FabricDefect.objects.create(
        number=f'BS-{date:%Y%m%d}-{uuid4().hex}',
        date=date,
        cmt=cmt,
        note=note,
        recorded_by=user,
        request_id=request_id,
        fingerprint=digest,
    )
    for item in pending:
        item.defect = defect
        item.roll.bs_yard += item.yard
        item.roll.save(update_fields=['bs_yard'])
    FabricDefectItem.objects.bulk_create(pending)
    log(
        user,
        'catat BS',
        defect.number,
        f'{len(pending)} roll; potongan invoice asal Rp {sum(totals.values(), ZERO)}',
    )
    return defect


@transaction.atomic
def return_defect_item(item, date, note, user):
    require_purchasing(user)
    valid_date(date, 'Tanggal retur')
    note = limited_text(note, 'Keterangan pengiriman', 4000)
    rolls, _, _, _ = _lock_sources([item.roll_id])
    item = FabricDefectItem.objects.select_for_update().select_related('defect').get(pk=item.pk)
    if item.defect.cancelled_at:
        raise ValidationError('BS sudah dibatalkan.')
    if item.returned_at:
        return item
    if date < item.defect.date:
        raise ValidationError('Tanggal retur tidak boleh sebelum tanggal BS.')
    item.return_date, item.return_note = date, note
    item.returned_at, item.returned_by = timezone.now(), user
    item.save(update_fields=['return_date', 'return_note', 'returned_at', 'returned_by'])
    roll = rolls[0]
    waiting = roll.defect_items.filter(
        defect__cancelled_at__isnull=True, returned_at__isnull=True
    ).exists()
    if roll.usable_yard == ZERO and not waiting:
        roll.status = Roll.Status.DIRETUR
        roll.save(update_fields=['status'])
    log(user, 'retur BS', item.defect.number, f'Item {item.pk}; {item.yard} yard; {note}')
    return item


@transaction.atomic
def cancel_defect(defect, reason, user):
    require_admin(user)
    reason = limited_text(reason, 'Alasan pembatalan', 4000)
    if not reason:
        raise ValidationError('Alasan pembatalan wajib diisi.')
    ids = list(defect.items.values_list('roll_id', flat=True))
    rolls, allocations, invoices, pos = _lock_sources(ids)
    defect = FabricDefect.objects.select_for_update().get(pk=defect.pk)
    if defect.cancelled_at:
        return defect
    items = list(defect.items.all())
    if any(item.returned_at for item in items):
        raise ValidationError(
            'BS sudah diretur. Koreksi lokasi stok belum tersedia; pembatalan dikunci.'
        )
    totals = {}
    for item in items:
        totals[item.invoice_id] = totals.get(item.invoice_id, ZERO) + item.amount
    for pk, amount in totals.items():
        summary = payment_summary(invoices[pk])
        new_refund = max((summary.paid or ZERO) - (summary.net_total + amount), ZERO)
        if summary.refund_received > new_refund:
            raise ValidationError(
                'Refund sudah diterima melebihi hak refund setelah pembatalan BS.'
            )
    by_roll = {item.roll_id: item for item in items}
    for roll in rolls:
        allocation = allocations.get(roll.alokasi_id)
        require_unused(roll, pos.get(allocation.po_id) if allocation else None)
        roll.bs_yard -= by_roll[roll.pk].yard
        roll.save(update_fields=['bs_yard'])
    defect.cancelled_at, defect.cancelled_by = timezone.now(), user
    defect.cancellation_reason = reason
    defect.save(update_fields=['cancelled_at', 'cancelled_by', 'cancellation_reason'])
    log(user, 'batalkan BS', defect.number, reason)
    return defect


@transaction.atomic
def record_refund(invoice, amount, date, note, request_id, user):
    if not can_receive_refund(user):
        raise PermissionDenied('Hanya Direktur/Super Admin yang dapat mencatat refund diterima.')
    validate_request_id(request_id)
    valid_date(date, 'Tanggal penerimaan refund')
    amount = parse_money(amount)
    note = limited_text(note, 'Referensi refund', 4000)
    digest = fingerprint(
        {'invoice': invoice.pk, 'amount': amount, 'date': date, 'note': note, 'user': user.pk}
    )
    invoice = lock_invoice(invoice.pk)
    previous = retry(VendorRefund, request_id, digest)
    if previous:
        return previous
    summary = payment_summary(invoice)
    if invoice.dibatalkan or summary.status_key == 'reconciliation':
        raise ValidationError('Invoice dibatalkan atau perlu rekonsiliasi.')
    if date < invoice.tanggal:
        raise ValidationError('Tanggal penerimaan refund tidak boleh sebelum invoice.')
    if amount <= ZERO or amount > summary.refund_remaining:
        raise ValidationError(
            'Nominal refund harus positif dan tidak melebihi sisa refund terbaru.'
        )
    refund = VendorRefund.objects.create(
        invoice=invoice,
        amount=amount,
        date=date,
        note=note,
        recorded_by=user,
        request_id=request_id,
        fingerprint=digest,
    )
    log(user, 'refund diterima', invoice.nomor, f'Penerimaan #{refund.pk}: Rp {amount}; {note}')
    return refund


@transaction.atomic
def cancel_refund(refund, reason, user):
    require_admin(user)
    reason = limited_text(reason, 'Alasan pembatalan', 4000)
    if not reason:
        raise ValidationError('Alasan pembatalan wajib diisi.')
    invoice = lock_invoice(refund.invoice_id)
    refund = VendorRefund.objects.select_for_update().get(pk=refund.pk)
    if refund.cancelled_at:
        return refund
    refund.cancelled_at, refund.cancelled_by = timezone.now(), user
    refund.cancellation_reason = reason
    refund.save(update_fields=['cancelled_at', 'cancelled_by', 'cancellation_reason'])
    log(user, 'batalkan penerimaan refund', invoice.nomor, f'#{refund.pk}: {reason}')
    return refund
