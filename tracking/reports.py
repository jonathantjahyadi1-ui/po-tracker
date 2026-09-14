from collections import defaultdict
from decimal import Decimal
from django.db.models import Q, Sum
from .models import *
from .presentation import cell, row, url, date, number, STATUS, ACTION_LABELS, ENTITY_LABELS, ROLE_LABELS
from .services import totals

TITLES={'receipts':'Penerimaan bahan','stock':'Stok bahan','movements':'Kartu stok','orders':'PO produksi','allocations':'Alokasi bahan','shipments':'Pengiriman bahan','cmt':'Penerimaan CMT','progress':'Progres produksi','finished':'Pengiriman hasil','warehouse':'Penerimaan gudang','reconciliation':'Sisa bahan','exceptions':'Selisih & resolusi','corrections':'Koreksi & reversal','audit':'Audit log','masters':'Data master','accounts':'Akun pengguna'}
MODEL_MAP={'receipts':Receipt,'orders':Order,'allocations':Allocation,'shipments':Shipment,'cmt':CMTReceipt,'progress':Progress,'finished':FinishedShipment,'warehouse':WarehouseReceipt,'reconciliation':Reconciliation,'exceptions':Discrepancy,'corrections':Correction,'audit':Audit,'masters':Master,'accounts':User,'stock':Lot}
FILTER_PATHS={
 'receipts':{'vendor':'vendor','material':'lines__material','color':'lines__color','warehouse':'lines__warehouse','date':'received_date','search':['invoice','vendor__name','delivery_note'],'status':'status'},
 'orders':{'product':'product','cmt':'cmts','order':'pk','date':'order_date','search':['number','product__name'],'status':'status'},
 'allocations':{'order':'order','cmt':'cmt','vendor':'lines__lot__receipt_line__receipt__vendor','material':'lines__lot__receipt_line__material','color':'lines__lot__receipt_line__color','warehouse':'lines__warehouse','product':'order__product','date':'created_at__date','search':['order__number','cmt__name','lines__lot__code'],'status':'status'},
 'shipments':{'order':'allocation__order','cmt':'allocation__cmt','vendor':'lines__allocation_line__lot__receipt_line__receipt__vendor','material':'lines__allocation_line__lot__receipt_line__material','color':'lines__allocation_line__lot__receipt_line__color','warehouse':'lines__allocation_line__warehouse','product':'allocation__order__product','date':'sent_date','search':['delivery_note','allocation__order__number','allocation__cmt__name'],'status':'status'},
 'cmt':{'order':'shipment_line__shipment__allocation__order','cmt':'shipment_line__shipment__allocation__cmt','material':'shipment_line__allocation_line__lot__receipt_line__material','color':'shipment_line__allocation_line__lot__receipt_line__color','date':'report_date','search':['pic','shipment_line__shipment__delivery_note','shipment_line__shipment__allocation__order__number']},
 'progress':{'order':'order','cmt':'cmt','product':'order__product','date':'report_date','search':['order__number','pic','notes']},
 'finished':{'order':'order','cmt':'cmt','warehouse':'warehouse','product':'order__product','date':'report_date','search':['delivery_note','order__number','cmt__name'],'status':'status'},
 'warehouse':{'order':'shipment__order','cmt':'shipment__cmt','warehouse':'shipment__warehouse','product':'shipment__order__product','date':'report_date','search':['shipment__order__number','pic','shipment__delivery_note']},
 'reconciliation':{'order':'order','cmt':'cmt','warehouse':'warehouse','material':'lot__receipt_line__material','color':'lot__receipt_line__color','vendor':'lot__receipt_line__receipt__vendor','date':'report_date','search':['order__number','lot__code','notes']},
 'masters':{'date':'created_at__date','search':['code','name','pic','contact']},
 'accounts':{'date':'date_joined__date','search':['username','first_name','email']},
 'audit':{'date':'created_at__date','search':['action','entity','object_id','actor__username']},
 'exceptions':{'date':'created_at__date','search':['description','resolution','shipment_line__shipment__allocation__order__number','finished_shipment__order__number']},
 'corrections':{'date':'created_at__date','search':['reason','kind']},
 'stock':{'vendor':'lot__receipt_line__receipt__vendor','material':'lot__receipt_line__material','color':'lot__receipt_line__color','order':'order','product':'order__product','warehouse':'location','cmt':'location','date':'created_at__date','search':['lot__code','lot__receipt_line__receipt__invoice','lot__receipt_line__material__name','lot__receipt_line__color__name'],'status':'bucket'},
}
FILTER_PATHS['movements']=FILTER_PATHS['stock']
FILTER_PATHS['stock']['scope']='location__kind'

def filter_query(qs,kind,filters,snapshot=False):
    paths=FILTER_PATHS[kind]
    for k,v in filters.items():
        if not v or k not in paths or k in ['date','search']:
            continue
        if kind=='shipments' and k=='status' and v=='awaiting_receipt':
            qs=qs.filter(status__in=['dispatched','partially_received'])
            continue
        qs=qs.filter(**{paths[k]:v.pk if hasattr(v,'pk') else v})
    if filters.get('q'):
        query=Q()
        for path in paths['search']:
            query |= Q(**{path+'__icontains':filters['q']})
        qs=qs.filter(query)
    if filters.get('start') and not snapshot:
        qs=qs.filter(**{paths['date']+'__gte':filters['start']})
    if filters.get('end'):
        qs=qs.filter(**{paths['date']+'__lte':filters['end']})
    return qs.distinct()

def stock_rows(filters):
    selected_bucket=filters.get('status')
    qs=Movement.objects.all()
    # PO filters choose source lots; every balance for those lots stays complete.
    if filters.get('order'):
        qs=qs.filter(lot_id__in=Lot.objects.filter(allocation_lines__allocation__order=filters['order']).values('pk'))
    if filters.get('product'):
        qs=qs.filter(lot_id__in=Lot.objects.filter(allocation_lines__allocation__order__product=filters['product']).values('pk'))
    qs=filter_query(qs,'stock',{k:v for k,v in filters.items() if k not in ['status','order','product']},True)
    grouped=qs.values('lot_id','location_id','bucket').annotate(r=Sum('rolls'),y=Sum('yards')).order_by('lot_id','location_id')
    values=defaultdict(lambda:defaultdict(lambda:[0,ZERO]))
    for item in grouped:
        values[(item['lot_id'],item['location_id'])][item['bucket']]=[item['r'],item['y'].quantize(Decimal('.01'))]
    lots={x.pk:x for x in Lot.objects.select_related('receipt_line__receipt__vendor','receipt_line__material','receipt_line__color').filter(pk__in={k[0] for k in values})}
    locations=Master.objects.in_bulk({k[1] for k in values})
    result=[]
    for (lot_id,loc_id),b in values.items():
        if all(v==[0,ZERO] for v in b.values()):
            continue
        if selected_bucket and b[selected_bucket]==[0,ZERO]:
            continue
        lot=lots[lot_id]; line=lot.receipt_line; loc=locations[loc_id]
        base=b['cmt'] if loc.kind=='cmt' else b['physical']
        free=[base[0]-b['reserved'][0],base[1]-b['reserved'][1]]
        def amount(pair):
            return cell(number(pair[1],2),sub=f'{number(pair[0])} roll',numeric=True)
        result.append(row(lot,[cell(lot.code,url(lot),sub=f'{line.material.name} / {line.color.name}'),cell(line.receipt.invoice,url(line.receipt),sub=line.receipt.vendor.name),cell(loc.name),amount(b['physical']),amount(b['reserved']),amount(free),amount(b['transit']),amount(b['cmt'])]))
    return ['Lot / bahan','Invoice / vendor','Lokasi','Fisik · yard','Reservasi · yard','Bebas · yard','Perjalanan · yard','Di CMT · yard'],result

def dataset(kind,filters,user,extra=None):
    extra=extra or {}
    if kind=='stock':
        return stock_rows(filters)
    if kind=='movements':
        qs=filter_query(Movement.objects.select_related('lot','location','order','actor','receipt','allocation','shipment','correction'),'movements',filters).order_by('created_at','id')
        if extra.get('lot'):
            qs=qs.filter(lot_id=extra['lot'])
        # Running location balance includes earlier movements hidden by report filters.
        selected=list(qs)
        selected_ids={m.pk for m in selected}
        ledger=Movement.objects.filter(lot_id__in={m.lot_id for m in selected}).select_related('lot','location','order','actor','receipt','allocation','shipment','correction','cmt_receipt','reconciliation').order_by('created_at','id')
        if filters.get('end'):
            ledger=ledger.filter(created_at__date__lte=filters['end'])
        running=defaultdict(lambda:[0,ZERO])
        rows=[]
        for m in ledger:
            owned=m.bucket in ['cmt','transit'] or (m.bucket=='reserved' and m.location.kind=='cmt')
            b=running[(m.lot_id,m.location_id,m.bucket,m.order_id if owned else None)]
            b[0]+=m.rolls; b[1]+=m.yards
            if m.pk not in selected_ids:
                continue
            rows.append(row(m,[cell(date(timezone.localtime(m.created_at)),sub=timezone.localtime(m.created_at).strftime('%H:%M')),cell(m.lot.code,url(m.lot)),cell(ACTION_LABELS.get(m.kind,m.kind),url(m.reference),sub=str(m.reference)),cell(m.location.name,sub=STATUS.get(m.bucket,m.bucket)),cell(str(m.order or '—'),url(m.order) if m.order else ''),cell(number(m.rolls),numeric=True),cell(number(m.yards,2),numeric=True),cell(number(b[1],2),sub=f'{number(b[0])} roll',numeric=True),cell(m.actor.get_full_name() or m.actor.username)]))
        return ['Waktu','Lot','Pergerakan / sumber','Lokasi / status','PO','Roll +/−','Yard +/−','Saldo yard','Pengguna'],rows
    qs=MODEL_MAP[kind].objects.all()
    if kind=='audit':
        if user.role!='admin':
            qs=qs.filter(actor=user)
    if kind=='orders':
        qs=qs.filter(archived=extra.get('archive')=='1')
        if extra.get('overdue')=='1':
            qs=qs.exclude(status__in=['closed','cancelled','balanced']).filter(due_date__lt=timezone.localdate())
    if kind=='masters' and extra.get('kind'):
        qs=qs.filter(kind=extra['kind'])
    if kind=='exceptions' and extra.get('resolved')!='1':
        qs=qs.filter(resolved=False)
    qs=filter_query(qs,kind,filters)
    relations={'receipts':['vendor','warehouse'],'orders':['product'],'allocations':['order','cmt'],'shipments':['allocation__order','allocation__cmt'],'cmt':['shipment_line__shipment__allocation__order','shipment_line__shipment__allocation__cmt','shipment_line__allocation_line__lot'],'progress':['order','cmt'],'finished':['order','cmt','warehouse'],'warehouse':['shipment__order','shipment__warehouse'],'reconciliation':['order','cmt','lot'],'audit':['actor'],'exceptions':['shipment_line__shipment__allocation__order','finished_shipment__order'],'corrections':['created_by']}
    if kind in relations:
        qs=qs.select_related(*relations[kind])
    qs=qs.order_by('id' if extra.get('sort')=='oldest' else '-id')
    if '_page' in extra:
        from django.core.paginator import Paginator
        paginator=Paginator(qs,25)
        page=paginator.get_page(extra['_page'])
        extra['_total']=paginator.count
        extra['_page_number']=page.number
        qs=page.object_list
    result=[]; headers=[]
    for o in qs:
        if kind=='receipts':
            q=o.lines.aggregate(r=Sum('rolls'),y=Sum('yards'))
            headers=['Invoice','Vendor','Diterima','Roll','Yard','Total invoice','Status']
            cells=[cell(o.invoice,url(o),sub=o.delivery_note or '—'),cell(o.vendor.name),cell(date(o.received_date)),cell(number(q['r'] or 0),numeric=True),cell(number(q['y'] or ZERO,2),numeric=True),cell(number(o.invoice_total,2),numeric=True),cell(o.status,status=True)]
        elif kind=='orders':
            t=totals(o)
            headers=['PO / produk','Target','Good diterima','Reject','Sisa target','Kelebihan','Target selesai','Status']
            cells=[cell(o.number,url(o),sub=o.product.name),cell(number(o.target),numeric=True),cell(number(t['good']),numeric=True),cell(number(t['reject']),numeric=True),cell(number(t['remaining']),numeric=True),cell(number(t['over']),numeric=True),cell(date(o.due_date)),cell(o.status,status=True)]
        elif kind=='allocations':
            q=o.lines.aggregate(r=Sum('rolls'),y=Sum('yards'))
            headers=['Alokasi','PO','CMT','Roll','Yard','Rencana kirim','Status']
            cells=[cell(str(o),url(o),sub='Transfer' if o.source_order_id else 'Gudang → CMT'),cell(o.order.number,url(o.order)),cell(o.cmt.name),cell(number(q['r'] or 0),numeric=True),cell(number(q['y'] or ZERO,2),numeric=True),cell(date(o.planned_date)),cell(o.status,status=True)]
        elif kind=='shipments':
            q=o.lines.aggregate(r=Sum('rolls'),y=Sum('yards'))
            headers=['Pengiriman','PO','CMT','Tanggal kirim','Roll','Yard','Status']
            cells=[cell(o.delivery_note,url(o),sub=str(o)),cell(o.allocation.order.number,url(o.allocation.order)),cell(o.allocation.cmt.name),cell(date(o.sent_date)),cell(number(q['r'] or 0),numeric=True),cell(number(q['y'] or ZERO,2),numeric=True),cell(o.status,status=True)]
        elif kind=='cmt':
            headers=['Laporan','PO','CMT','Lot','Roll','Yard','PIC / media','Kondisi']
            cells=[cell(date(o.report_date),url(o)),cell(str(o.shipment_line.shipment.allocation.order),url(o.shipment_line.shipment.allocation.order)),cell(o.shipment_line.shipment.allocation.cmt.name),cell(o.shipment_line.allocation_line.lot.code,url(o.shipment_line.allocation_line.lot)),cell(number(o.rolls),numeric=True),cell(number(o.yards,2),numeric=True),cell(o.pic,sub=o.get_medium_display()),cell('reversed' if o.reversed else o.get_condition_display(),status=True)]
        elif kind=='progress':
            headers=['Tanggal','PO','CMT','Tahap','Jumlah','Reject','PIC','Estimasi']
            cells=[cell(date(o.report_date),url(o)),cell(o.order.number,url(o.order)),cell(o.cmt.name),cell(o.get_stage_display()),cell(number(o.quantity),numeric=True),cell(number(o.reject),numeric=True),cell(o.pic,sub=o.get_medium_display()),cell(date(o.eta))]
        elif kind=='finished':
            headers=['Surat jalan','PO','CMT','Gudang','Tanggal kirim','Dikirim (pcs)','Status']
            cells=[cell(o.delivery_note,url(o)),cell(o.order.number,url(o.order)),cell(o.cmt.name),cell(o.warehouse.name),cell(date(o.report_date)),cell(number(o.quantity),numeric=True),cell(o.status,status=True)]
        elif kind=='warehouse':
            headers=['Tanggal','PO','Gudang','Diterima','Good','Reject','PIC / media','Status']
            cells=[cell(date(o.report_date),url(o)),cell(o.shipment.order.number,url(o.shipment.order)),cell(o.shipment.warehouse.name),cell(number(o.received),numeric=True),cell(number(o.good),numeric=True),cell(number(o.reject),numeric=True),cell(o.pic,sub=o.get_medium_display()),cell('reversed' if o.reversed else ('discrepancy' if o.discrepancy else 'posted'),status=True)]
        elif kind=='reconciliation':
            headers=['Tanggal','PO','CMT','Lot','Tindakan','Roll','Yard','PIC']
            cells=[cell(date(o.report_date),url(o)),cell(o.order.number,url(o.order)),cell(o.cmt.name),cell(o.lot.code,url(o.lot)),cell('reversed' if o.reversed else o.action,status=True),cell(number(o.rolls),numeric=True),cell(number(o.yards,2),numeric=True),cell(o.pic)]
        elif kind=='masters':
            headers=['Kode','Nama','Jenis','PIC','Kontak','Status']
            cells=[cell(o.code,url(o)),cell(o.name),cell(o.get_kind_display()),cell(o.pic or '—'),cell(o.contact or '—'),cell('Aktif' if o.active else 'Nonaktif')]
        elif kind=='accounts':
            headers=['Pengguna','Nama','Role','Email','Terakhir masuk','Status']
            cells=[cell(o.username,url(o)),cell(o.get_full_name() or '—'),cell(o.get_role_display()),cell(o.email or '—'),cell(date(timezone.localtime(o.last_login)) if o.last_login else '—'),cell('Aktif' if o.is_active else 'Nonaktif')]
        elif kind=='audit':
            headers=['Waktu','Pengguna','Role','Aksi','Entitas','ID','Perubahan','Request ID']
            cells=[cell(date(timezone.localtime(o.created_at)),sub=timezone.localtime(o.created_at).strftime('%H:%M:%S')),cell(o.actor.username if o.actor else 'Sistem'),cell(ROLE_LABELS.get(o.role,o.role)),cell(ACTION_LABELS.get(o.action,o.action)),cell(ENTITY_LABELS.get(o.entity,o.entity)),cell(o.object_id),cell(str(o.after)),cell(o.request_id)]
        elif kind=='exceptions':
            order=o.shipment_line.shipment.allocation.order if o.shipment_line else o.finished_shipment.order
            headers=['Referensi','PO','Catatan selisih','Dicatat','Status']
            cells=[cell(f'SLS-{o.pk:05d}',url(o)),cell(str(order),url(order)),cell(o.description),cell(date(timezone.localtime(o.created_at))),cell('Selesai' if o.resolved else 'Terbuka')]
        else:
            headers=['Referensi','Jenis','Alasan','Pengguna','Waktu']
            cells=[cell(f'KOR-{o.pk:05d}',url(o)),cell(o.kind),cell(o.reason),cell(o.created_by.username),cell(date(timezone.localtime(o.created_at)))]
        result.append(row(o,cells))
    return headers,result
