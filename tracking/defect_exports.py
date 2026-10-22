"""Source-backed BS/refund sheets shared by invoice and Payment downloads."""

from collections import defaultdict

from django.utils import timezone

from .models import FabricDefectItem, VendorRefund
from .payment_services import payment_summary
from .services import ZERO


def load_adjustments(invoices):
    ids = [invoice.pk for invoice in invoices]
    items, refunds = defaultdict(list), defaultdict(list)
    for item in (
        FabricDefectItem.objects.filter(invoice_id__in=ids)
        .select_related(
            'defect__recorded_by',
            'defect__cancelled_by',
            'roll__material',
            'roll__color',
            'vendor',
            'po',
            'returned_by',
        )
        .order_by('defect__date', 'pk')
    ):
        items[item.invoice_id].append(item)
    for refund in VendorRefund.objects.filter(invoice_id__in=ids).select_related(
        'recorded_by', 'cancelled_by'
    ):
        refunds[refund.invoice_id].append(refund)
    for invoice in invoices:
        invoice.export_bs = items[invoice.pk]
        invoice.export_refunds = refunds[invoice.pk]
        invoice._bs_total = sum(
            (item.amount for item in invoice.export_bs if not item.defect.cancelled_at), ZERO
        )
        invoice._refund_received = sum(
            (refund.amount for refund in invoice.export_refunds if not refund.cancelled_at), ZERO
        )


def add_adjustment_sheets(workbook, invoices):
    from .payment_views import append_record

    if not any(invoice.export_bs or invoice.export_refunds for invoice in invoices):
        return
    position = workbook.create_sheet('Posisi BS dan Refund')
    bs = workbook.create_sheet('Rincian BS')
    refunds = workbook.create_sheet('Penerimaan Refund')
    append_record(
        position,
        [
            'Invoice',
            'Vendor',
            'Nilai awal (Rp)',
            'BS aktif (Rp)',
            'Setelah BS (Rp)',
            'Uang keluar (Rp)',
            'Sisa dibayar (Rp)',
            'Refund perlu diminta (Rp)',
            'Refund diterima (Rp)',
            'Sisa refund (Rp)',
            'Status pembayaran',
            'Status refund',
        ],
    )
    append_record(
        bs,
        [
            'Invoice asal',
            'Vendor',
            'Nomor BS',
            'Tanggal BS',
            'Pencatat BS',
            'Bahan',
            'Warna',
            'Roll',
            'ID roll',
            'PO',
            'Yard BS',
            'Snapshot harga (Rp/yard)',
            'Nilai BS (Rp)',
            'Keterangan BS',
            'Status',
            'Tanggal retur',
            'Pencatat retur',
            'Keterangan retur',
            'Alasan pembatalan',
        ],
    )
    append_record(
        refunds,
        [
            'ID penerimaan',
            'Invoice asal',
            'Vendor',
            'Tanggal diterima',
            'Nominal masuk (Rp)',
            'Pencatat',
            'Waktu dicatat (WIB)',
            'Referensi',
            'Status',
            'Alasan pembatalan',
        ],
    )
    for invoice in invoices:
        summary = getattr(invoice, 'payment', None) or payment_summary(invoice)
        append_record(
            position,
            [
                invoice.nomor,
                invoice.vendor.name,
                invoice.total_rp,
                summary.bs_total,
                summary.net_total,
                summary.paid,
                summary.remaining,
                summary.refund_due,
                summary.refund_received,
                summary.refund_remaining,
                summary.status,
                summary.refund_status,
            ],
        )
        for item in invoice.export_bs:
            append_record(
                bs,
                [
                    invoice.nomor,
                    item.vendor.name,
                    item.defect.number,
                    item.defect.date,
                    item.defect.recorded_by.username,
                    item.roll.material.name,
                    item.roll.color.name,
                    item.roll.urut,
                    item.roll_id,
                    item.po.nomor if item.po_id else '',
                    item.yard,
                    item.price,
                    item.amount,
                    item.defect.note,
                    'Dibatalkan'
                    if item.defect.cancelled_at
                    else 'Sudah diretur'
                    if item.returned_at
                    else 'Menunggu retur',
                    item.return_date,
                    item.returned_by.username if item.returned_by_id else '',
                    item.return_note,
                    item.defect.cancellation_reason,
                ],
            )
        for refund in invoice.export_refunds:
            append_record(
                refunds,
                [
                    refund.pk,
                    invoice.nomor,
                    invoice.vendor.name,
                    refund.date,
                    refund.amount,
                    refund.recorded_by.username,
                    timezone.localtime(refund.created_at).replace(tzinfo=None),
                    refund.note,
                    'Dibatalkan' if refund.cancelled_at else 'Diterima',
                    refund.cancellation_reason,
                ],
            )
    for sheet, money_columns, date_columns in (
        (position, range(3, 11), ()),
        (bs, (12, 13), (4, 16)),
        (refunds, (5,), (4,)),
    ):
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        for row in sheet.iter_rows(min_row=2):
            for col in money_columns:
                row[col - 1].number_format = '#,##0.00'
            for col in date_columns:
                row[col - 1].number_format = 'dd/mm/yyyy'
        for column in sheet.columns:
            sheet.column_dimensions[column[0].column_letter].width = min(
                50, max(16, max(len(str(cell.value or '')) for cell in column) + 2)
            )
    for row in bs.iter_rows(min_row=2):
        row[10].number_format = '#,##0.######'
    for row in refunds.iter_rows(min_row=2):
        row[6].number_format = 'dd/mm/yyyy hh:mm:ss'
