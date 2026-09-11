from decimal import Decimal
from django.urls import reverse
from .models import *

STATUS = {'draft':'Draft','posted':'Posted','cancelled':'Dibatalkan','reversed':'Direversal','pending':'Menunggu persetujuan','revision':'Perlu revisi','approved':'Disetujui','rejected':'Ditolak','partially_shipped':'Dikirim sebagian','fully_shipped':'Dikirim seluruhnya','dispatched':'Dalam perjalanan','partially_received':'Diterima sebagian','received':'Diterima','discrepancy':'Ada selisih','waiting_material':'Menunggu bahan','material_allocated':'Bahan dialokasikan','in_production':'Dalam produksi','partial_delivery':'Hasil diterima sebagian','balance_candidate':'Kandidat balance','balanced':'Balance','closed':'Ditutup','consumed':'Terpakai','returned':'Retur','waste':'Waste','damaged':'Rusak','transfer':'Transfer','physical':'Fisik','reserved':'Reservasi','transit':'Perjalanan','cmt':'Di CMT'}
RESOURCES = {Master:'masters',Receipt:'receipts',Order:'orders',Allocation:'allocations',Shipment:'shipments',CMTReceipt:'cmt',Progress:'progress',FinishedShipment:'finished',WarehouseReceipt:'warehouse',Reconciliation:'reconciliation',Correction:'corrections',Discrepancy:'exceptions',Lot:'stock',User:'accounts'}
ACTION_LABELS={'login':'Masuk','logout':'Keluar','save_draft':'Draft disimpan','create_master':'Master dibuat','update_master':'Master diperbarui','post':'Transaksi diposting','submit':'Alokasi diajukan','decision':'Keputusan alokasi','reserve':'Reservasi','dispatch':'Bahan dikirim','cancel':'Dibatalkan','reverse':'Direversal','recalculate':'Status PO diperbarui','shipment_status':'Status alokasi diperbarui','receipt_status':'Status penerimaan diperbarui','close':'PO ditutup','reopen':'PO dibuka kembali','archive':'PO diarsipkan','activate':'PO diaktifkan','resolve':'Selisih diselesaikan','resolve_over':'Kelebihan hasil diselesaikan','export':'Laporan diekspor','adjustment':'Adjustment dicatat','create_account':'Akun dibuat','update_account':'Akun diperbarui'}
ENTITY_LABELS={'Receipt':'Penerimaan bahan','Order':'PO produksi','Allocation':'Alokasi','Master':'Data master','CMTReceipt':'Penerimaan CMT','Shipment':'Pengiriman bahan','Progress':'Progres produksi','WarehouseReceipt':'Penerimaan gudang','FinishedShipment':'Pengiriman hasil','Correction':'Koreksi','Reconciliation':'Rekonsiliasi','Discrepancy':'Selisih','User':'Akun','Lot':'Lot'}
ACTION_LABELS.update({'receipt':'Bahan masuk','cmt_receive':'Diterima CMT','release':'Reservasi dilepas','reversal':'Reversal','consumed':'Terpakai','returned':'Retur','waste':'Waste','damaged':'Rusak','transfer':'Transfer','transit_loss':'Kehilangan dalam perjalanan','adjustment_in':'Adjustment masuk','adjustment_out':'Adjustment keluar'})

def url(obj):
    if isinstance(obj,ReceiptLine):
        return url(obj.receipt)
    if isinstance(obj,ShipmentLine):
        return url(obj.shipment)
    if isinstance(obj,AllocationLine):
        return url(obj.allocation)
    kind = RESOURCES.get(type(obj))
    return reverse('detail',args=[kind,obj.pk]) if kind else ''

def number(value, decimals=0):
    if value is None:
        return '—'
    return f'{value:,.{decimals}f}'.replace(',','~').replace('.',',').replace('~','.')

def cell(value,link='',status=False,sub='',numeric=False):
    return {'value':STATUS.get(value,value) if status else value,'link':link,'status':str(value) if status else '', 'sub':sub,'numeric':numeric}

def date(value):
    return value.strftime('%d %b %Y') if value else '—'

def row(obj, values):
    return {'id':obj.pk,'url':url(obj),'cells':values,'object':obj}
