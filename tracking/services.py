import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from functools import wraps
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum, Q
from .middleware import request_id
from .models import *

P = User.Role.PURCHASING

def require(actor, roles):
    current = User.objects.get(pk=actor.pk)
    if not current.is_active or current.role not in roles:
        raise PermissionDenied('Akun ini tidak memiliki hak untuk tindakan tersebut.')
    return current

def audit(actor, action, obj, before=None, after=None):
    Audit.objects.create(actor=actor, role=actor.role, action=action, entity=type(obj).__name__, object_id=str(obj.pk), before=before or {}, after=after or {}, request_id=request_id.get())

def command(roles, model):
    def decorate(fn):
        @wraps(fn)
        def wrapped(actor, *args, key=None, **kwargs):
            actor = require(actor, roles)
            try:
                token = uuid.UUID(str(key)) if key else uuid.uuid4()
            except (ValueError, TypeError):
                raise ValidationError('Token permintaan tidak valid. Muat ulang form.')
            with transaction.atomic():
                op, _ = Operation.objects.get_or_create(key=token, defaults={'actor': actor, 'action': fn.__name__})
                op = Operation.objects.select_for_update().get(pk=op.pk)
                if op.actor_id != actor.pk or op.action != fn.__name__:
                    raise ValidationError('Token sudah dipakai untuk permintaan lain.')
                if op.result_id:
                    return model.objects.get(pk=op.result_id)
                result = fn(actor, *args, **kwargs)
                op.result_id = result.pk
                op.save(update_fields=['result_id'])
                return result
        return wrapped
    return decorate

def qty(rolls, yards, allow_zero=False):
    if isinstance(rolls, (float, bool)) or isinstance(yards, (float, bool)):
        raise ValidationError('Gunakan bilangan bulat untuk roll dan maksimal dua desimal untuk yard.')
    try:
        r, y = Decimal(str(rolls)), Decimal(str(yards))
        if not r.is_finite() or not y.is_finite() or r != r.to_integral_value() or r < 0 or r > 2147483647 or y < 0 or y >= Decimal('1000000000000') or y != y.quantize(Decimal('.01')):
            raise ValueError()
    except (ValueError, InvalidOperation):
        raise ValidationError('Roll harus bulat, yard maksimal 2 desimal, dan kuantitas tidak boleh negatif.')
    if not allow_zero and r == 0 and y == 0:
        raise ValidationError('Isi roll atau yard lebih dari nol.')
    return int(r), y.quantize(Decimal('.01'))

def active(master, kind):
    if not master or master.kind != kind or not master.active:
        raise ValidationError(f'Data {kind} tidak aktif atau tidak sesuai.')

def validate_evidence(evidence):
    if evidence is None:
        raise ValidationError('Lampiran bukti wajib diunggah.')

def report(data, required_evidence=False):
    if not data.get('pic', '').strip() or data.get('medium') not in dict(Report._meta.get_field('medium').choices) or not data.get('report_date'):
        raise ValidationError('Tanggal laporan, PIC, dan media laporan wajib diisi.')
    if required_evidence:
        validate_evidence(data.get('evidence'))

def lock_orders(*ids):
    objects = {o.pk: o for o in Order.objects.select_for_update().filter(pk__in=[i for i in ids if i]).order_by('pk')}
    for obj in objects.values():
        if obj.status in ['closed', 'cancelled']:
            raise ValidationError(f'{obj.number} sudah ditutup atau dibatalkan.')
    return objects

def lock_lots(ids):
    return list(Lot.objects.select_for_update().filter(pk__in=ids).order_by('pk'))

def balance(lot=None, bucket=None, location=None, order=None, **filters):
    qs = Movement.objects.all()
    for field, value in [('lot', lot), ('bucket', bucket), ('location', location), ('order', order)]:
        if value is not None:
            qs = qs.filter(**{field: value})
    data = qs.filter(**filters).aggregate(r=Sum('rolls'), y=Sum('yards'))
    return data['r'] or 0, (data['y'] or ZERO).quantize(Decimal('.01'))

def minus(a, b):
    return a[0] - b[0], a[1] - b[1]

def available(lot, location, source_order=None):
    bucket = 'cmt' if source_order else 'physical'
    return minus(balance(lot, bucket, location, source_order), balance(lot, 'reserved', location, source_order))

def enough(have, need, label):
    if have[0] < need[0] or have[1] < need[1]:
        raise ValidationError(f'{label}: tersedia {have[0]} roll / {have[1]:,.2f} yard; diminta {need[0]} roll / {need[1]:,.2f} yard.')

def move(actor, lot, bucket, location, quantity, kind, order=None, sign=1, **source):
    Movement.objects.create(actor=actor, lot=lot, bucket=bucket, location=location, rolls=quantity[0] * sign, yards=quantity[1] * sign, order=order, kind=kind, **source)

def transition(actor, obj, status, action, notes=''):
    previous = obj.status
    obj.status = status
    obj.version += 1
    obj.save(update_fields=['status', 'version', 'updated_at'])
    audit(actor, action, obj, {'status': previous}, {'status': status, 'notes': notes})

def totals(order):
    data = WarehouseReceipt.objects.filter(shipment__order=order, reversed=False).aggregate(good=Sum('good'), reject=Sum('reject'))
    return result_totals(order,data)

def result_totals(order,data):
    good, reject = data['good'] or 0, data['reject'] or 0
    return {'good': good, 'reject': reject, 'remaining': max(order.target - good, 0), 'over': max(good - order.target, 0), 'percent': min(good * 100 // order.target, 100)}

def totals_many(orders):
    orders=list(orders)
    grouped=WarehouseReceipt.objects.filter(shipment__order_id__in=[o.pk for o in orders],reversed=False).values('shipment__order_id').annotate(good=Sum('good'),reject=Sum('reject'))
    values={item['shipment__order_id']:item for item in grouped}
    return {o.pk:result_totals(o,values.get(o.pk,{'good':0,'reject':0})) for o in orders}

def unresolved(order):
    if order.transfer_allocations.filter(status='draft').exists():
        return True
    if Movement.objects.filter(order=order).exists():
        for bucket in ['reserved', 'transit', 'cmt']:
            if balance(bucket=bucket, order=order) != (0, ZERO):
                return True
    return Discrepancy.objects.filter(Q(shipment_line__shipment__allocation__order=order) | Q(finished_shipment__order=order), resolved=False).exists() or FinishedShipment.objects.filter(order=order, status__in=['dispatched', 'partially_received', 'discrepancy']).exists() or Allocation.objects.filter(order=order, status='draft').exists()

def refresh_order(actor, order):
    if order.status in ['closed', 'cancelled', 'draft']:
        return
    t = totals(order)
    if t['remaining'] == 0:
        status = 'balanced' if not unresolved(order) and (not t['over'] or order.over_resolved) else 'balance_candidate'
    elif t['good']:
        status = 'partial_delivery'
    elif balance(bucket='cmt', order=order) != (0, ZERO) or order.progress_updates.exists() or order.finished_shipments.exists():
        status = 'in_production'
    elif order.allocations.filter(status__in=['allocated', 'partially_shipped', 'fully_shipped']).exists():
        status = 'material_allocated'
    else:
        status = 'waiting_material'
    if order.status != status:
        transition(actor, order, status, 'recalculate')

@command([P], Master)
def save_master(actor, data, pk=None, version=None):
    obj = Master.objects.select_for_update().get(pk=pk) if pk else Master(created_by=actor)
    if pk and obj.version != version:
        raise ValidationError('Data sudah berubah. Muat ulang sebelum menyimpan.')
    before = {k: str(getattr(obj, k)) for k in data} if pk else {}
    if pk and data.get('kind', obj.kind) != obj.kind:
        raise ValidationError('Jenis data master tidak dapat diubah.')
    for field, value in data.items():
        setattr(obj, field, value)
    if obj.unit:
        active(obj.unit, 'unit')
    obj.code = normalized(obj.code)
    obj.version += 1 if pk else 0
    obj.full_clean()
    obj.save()
    audit(actor, 'update_master' if pk else 'create_master', obj, before, {k: str(v) for k, v in data.items()})
    return obj

@command([P], Receipt)
def save_receipt(actor, data, lines, pk=None, version=None):
    obj = Receipt.objects.select_for_update().get(pk=pk) if pk else Receipt(created_by=actor)
    if pk and (obj.status != 'draft' or obj.version != version):
        raise ValidationError('Hanya draft dengan versi terbaru yang dapat diubah.')
    active(data['vendor'], 'vendor')
    active(data['warehouse'], 'warehouse')
    data['invoice'] = normalized(data['invoice'])
    if data.get('revision_of') and (data['revision_of'].vendor != data['vendor'] or data['revision_of'].status != 'reversed'):
        raise ValidationError('Invoice revisi harus merujuk invoice vendor yang sudah direversal. Gunakan nomor revisi unik.')
    before = {'invoice': obj.invoice, 'version': obj.version} if pk else {}
    for k, v in data.items():
        setattr(obj, k, v)
    obj.version += 1 if pk else 0
    obj.full_clean()
    obj.save()
    if pk:
        obj.lines.all().delete()  # Draft belum memiliki lot atau movement.
    for line in lines:
        if not line:
            continue
        for k, kind in [('material','material'), ('color','color'), ('unit','unit'), ('warehouse','warehouse')]:
            active(line[k], kind)
        r, y = qty(line['rolls'], line['yards'])
        ReceiptLine.objects.create(receipt=obj, **{**line, 'rolls': r, 'yards': y})
    audit(actor, 'save_draft', obj, before, {'invoice': obj.invoice, 'lines': obj.lines.count(), 'version': obj.version})
    return obj

@command([P], Receipt)
def post_receipt(actor, pk):
    obj = Receipt.objects.select_for_update().get(pk=pk)
    if obj.status != 'draft':
        raise ValidationError('Penerimaan harus berstatus draft.')
    active(obj.vendor, 'vendor')
    if not obj.lines.exists():
        raise ValidationError('Tambahkan minimal satu baris bahan.')
    for line in obj.lines.select_related('material', 'color', 'warehouse', 'unit'):
        for master, kind in [(line.material,'material'), (line.color,'color'), (line.warehouse,'warehouse'), (line.unit,'unit')]:
            active(master, kind)
        q = qty(line.rolls, line.yards)
        lot = Lot.objects.create(receipt_line=line, code=f'LOT-{obj.pk:05d}-{line.pk:05d}', created_by=actor)
        move(actor, lot, 'physical', line.warehouse, q, 'receipt', receipt=obj)
    transition(actor, obj, 'posted', 'post')
    return obj

@command([P], Receipt)
def cancel_receipt(actor, pk, reason):
    obj = Receipt.objects.select_for_update().get(pk=pk)
    if obj.status != 'draft' or not reason.strip():
        raise ValidationError('Hanya draft dapat dibatalkan; alasan wajib diisi.')
    transition(actor, obj, 'cancelled', 'cancel', reason)
    return obj

@command([P], Order)
def save_order(actor, data, pk=None, version=None):
    obj = Order.objects.select_for_update().get(pk=pk) if pk else Order(created_by=actor)
    if pk and (obj.status != 'draft' or obj.version != version):
        raise ValidationError('PO hanya dapat diedit pada draft dengan versi terbaru.')
    active(data['product'], 'product')
    cmts = data.pop('cmts', [])
    for cmt in cmts:
        active(cmt, 'cmt')
    for k,v in data.items():
        setattr(obj,k,v)
    obj.number = normalize_po(obj.number)
    if obj.due_date and obj.due_date < obj.order_date:
        raise ValidationError('Target selesai tidak boleh mendahului tanggal order.')
    obj.version += 1 if pk else 0
    obj.full_clean()
    obj.save()
    obj.cmts.set(cmts)
    audit(actor, 'save_draft', obj, after={'number': obj.number, 'target': obj.target, 'version': obj.version})
    return obj

@command([P], Order)
def order_action(actor, pk, action, reason='', evidence=None):
    obj = Order.objects.select_for_update().get(pk=pk)
    if action == 'activate':
        if obj.status != 'draft':
            raise ValidationError('Hanya PO draft dapat diaktifkan.')
        active(obj.product, 'product')
        transition(actor, obj, 'waiting_material', action)
    elif action == 'close':
        if obj.status in ['closed','cancelled','draft']:
            raise ValidationError('Status PO tidak mengizinkan penutupan.')
        if not settings.BUSINESS_RULES_CONFIRMED:
            raise ValidationError('Aturan penutupan PO belum dikonfirmasi pada konfigurasi.')
        t = totals(obj)
        if t['remaining'] or unresolved(obj) or (t['over'] and not obj.over_resolved):
            raise ValidationError('Target, penerimaan, reservasi, selisih, atau rekonsiliasi bahan belum selesai.')
        transition(actor, obj, 'closed', action)
    elif action == 'reopen':
        if obj.status != 'closed' or not reason.strip():
            raise ValidationError('PO harus sudah ditutup dan alasan wajib diisi.')
        obj.archived = False
        obj.save(update_fields=['archived'])
        transition(actor, obj, 'waiting_material', action, reason)
        refresh_order(actor, obj)
    elif action == 'resolve_over':
        if obj.status in ['closed','cancelled','draft'] or not totals(obj)['over'] or not reason.strip():
            raise ValidationError('Resolusi memerlukan kelebihan hasil pada PO aktif dan alasan.')
        obj.over_resolved = True
        obj.save(update_fields=['over_resolved'])
        audit(actor, action, obj, after={'reason':reason, 'over':totals(obj)['over']})
        refresh_order(actor,obj)
    elif action == 'archive':
        if obj.status not in ['closed','cancelled']:
            raise ValidationError('Hanya PO ditutup atau dibatalkan yang dapat diarsipkan.')
        obj.archived = True
        obj.save(update_fields=['archived'])
        audit(actor, action, obj)
    elif action == 'cancel':
        if obj.status in ['closed','cancelled'] or not reason.strip():
            raise ValidationError('Status tidak dapat dibatalkan atau alasan belum diisi.')
        if obj.finished_shipments.exists() or obj.progress_updates.exists() or Movement.objects.filter(order=obj).exclude(bucket='reserved').exists():
            raise ValidationError('PO sudah memiliki aktivitas produksi. Selesaikan rekonsiliasi dan penutupan.')
        if obj.transfer_allocations.exclude(status__in=['cancelled','rejected','fully_shipped']).exists():
            raise ValidationError('Batalkan permintaan transfer keluar terlebih dahulu.')
        allocations = list(obj.allocations.select_for_update().exclude(status__in=['cancelled','rejected']))
        lock_lots(AllocationLine.objects.filter(allocation__in=allocations).values_list('lot_id',flat=True))
        for allocation in allocations:
            release(actor, allocation)
            transition(actor, allocation, 'cancelled', 'cancel', reason)
        transition(actor,obj,'cancelled',action,reason)
    else:
        raise ValidationError('Aksi tidak dikenal.')
    return obj

def allocation_remaining(line):
    return balance(line.lot, 'reserved', line.warehouse, allocation=line.allocation)

@command([P], Allocation)
def save_allocation(actor, data, lines, pk=None, version=None):
    lock_orders(data['order'].pk, data.get('source_order').pk if data.get('source_order') else None)
    obj = Allocation.objects.select_for_update().get(pk=pk) if pk else Allocation(created_by=actor)
    if pk and (obj.status != 'draft' or obj.version != version):
        raise ValidationError('Alokasi harus draft dan menggunakan versi terbaru.')
    if Order.objects.get(pk=data['order'].pk).status == 'draft':
        raise ValidationError('Aktifkan PO sebelum membuat alokasi.')
    active(data['cmt'], 'cmt')
    if data['order'].cmts.exists() and not data['order'].cmts.filter(pk=data['cmt'].pk).exists():
        raise ValidationError('CMT tidak terdaftar pada PO tujuan.')
    if data.get('source_order') == data['order']:
        raise ValidationError('PO sumber dan tujuan transfer harus berbeda.')
    for k,v in data.items():
        setattr(obj,k,v)
    obj.version += 1 if pk else 0
    obj.full_clean()
    obj.save()
    if pk:
        obj.lines.all().delete()
    for line in lines:
        q = qty(line['rolls'],line['yards'])
        if obj.source_order:
            if line['warehouse'] != obj.cmt:
                raise ValidationError('Sumber transfer harus lokasi CMT yang sama.')
        else:
            active(line['warehouse'], 'warehouse')
        AllocationLine.objects.create(allocation=obj, **{**line, 'rolls':q[0], 'yards':q[1]})
    audit(actor,'save_draft',obj,after={'version':obj.version,'lines':obj.lines.count()})
    return obj

@command([P], Allocation)
def post_allocation(actor, pk):
    ref = Allocation.objects.get(pk=pk)
    orders = lock_orders(ref.order_id, ref.source_order_id)
    obj = Allocation.objects.select_for_update().get(pk=pk)
    if obj.status != 'draft' or not obj.lines.exists():
        raise ValidationError('Alokasi harus draft dengan minimal satu lot.')
    if any(order.status == 'draft' for order in orders.values()):
        raise ValidationError('Aktifkan PO sebelum mengalokasikan bahan.')
    active(obj.cmt, 'cmt')
    if obj.order.cmts.exists() and not obj.order.cmts.filter(pk=obj.cmt_id).exists():
        raise ValidationError('CMT tidak terdaftar pada PO tujuan.')
    if obj.source_order_id == obj.order_id:
        raise ValidationError('PO sumber dan tujuan transfer harus berbeda.')
    lock_lots(obj.lines.values_list('lot_id',flat=True))
    for line in obj.lines.select_related('lot','warehouse'):
        if obj.source_order_id:
            if line.warehouse_id != obj.cmt_id:
                raise ValidationError('Sumber transfer harus lokasi CMT yang sama.')
        else:
            active(line.warehouse, 'warehouse')
        quantity = qty(line.rolls, line.yards)
        enough(available(line.lot,line.warehouse,obj.source_order), quantity, line.lot.code)
        move(actor,line.lot,'reserved',line.warehouse,quantity,'reserve',order=obj.source_order or obj.order,allocation=obj)
    transition(actor,obj,'allocated','allocate')
    for order in orders.values():
        refresh_order(actor,order)
    return obj

def release(actor, obj):
    for line in obj.lines.select_related('lot','warehouse'):
        q = allocation_remaining(line)
        if q != (0,ZERO):
            move(actor,line.lot,'reserved',line.warehouse,q,'release',order=obj.source_order or obj.order,allocation=obj,sign=-1)

@command([P], Allocation)
def cancel_allocation(actor, pk, reason):
    ref = Allocation.objects.get(pk=pk)
    orders = lock_orders(ref.order_id,ref.source_order_id)
    obj = Allocation.objects.select_for_update().get(pk=pk)
    if not reason.strip() or obj.status in ['cancelled','rejected','fully_shipped']:
        raise ValidationError('Alasan wajib diisi dan alokasi harus masih terbuka.')
    lock_lots(obj.lines.values_list('lot_id',flat=True))
    release(actor,obj)
    transition(actor,obj,'cancelled','cancel',reason)
    for order in orders.values():
        refresh_order(actor,order)
    return obj

def refresh_allocation(actor, obj):
    if obj.status == 'cancelled':
        return
    remaining = [allocation_remaining(line) for line in obj.lines.all()]
    if all(q == (0,ZERO) for q in remaining):
        status = 'fully_shipped'
    elif all(q == (line.rolls,line.yards) for q,line in zip(remaining,obj.lines.all())):
        status = 'allocated'
    else:
        status = 'partially_shipped'
    if obj.status != status:
        transition(actor,obj,status,'shipment_status')

@command([P], Shipment)
def save_shipment(actor, allocation, data, lines, pk=None, version=None):
    lock_orders(allocation.order_id)
    allocation = Allocation.objects.select_for_update().get(pk=allocation.pk)
    if allocation.status not in ['allocated','partially_shipped'] or allocation.source_order_id:
        raise ValidationError('Gunakan alokasi gudang aktif yang masih memiliki sisa.')
    obj = Shipment.objects.select_for_update().get(pk=pk) if pk else Shipment(created_by=actor,allocation=allocation)
    if pk and (obj.status != 'draft' or obj.version != version or obj.allocation_id != allocation.pk):
        raise ValidationError('Hanya draft versi terbaru dapat diubah.')
    for k,v in data.items():
        setattr(obj,k,v)
    obj.version += 1 if pk else 0
    obj.full_clean()
    obj.save()
    if pk:
        obj.lines.all().delete()
    for line in lines:
        al = line['allocation_line']
        if al.allocation_id != allocation.pk:
            raise ValidationError('Lot bukan bagian alokasi ini.')
        q = qty(line['rolls'],line['yards'])
        enough(allocation_remaining(al),q,'Sisa alokasi')
        ShipmentLine.objects.create(shipment=obj,allocation_line=al,rolls=q[0],yards=q[1])
    audit(actor,'save_draft',obj,after={'delivery_note':obj.delivery_note,'lines':obj.lines.count()})
    return obj

@command([P], Shipment)
def dispatch(actor, pk):
    ref = Shipment.objects.select_related('allocation').get(pk=pk)
    order = lock_orders(ref.allocation.order_id)[ref.allocation.order_id]
    allocation = Allocation.objects.select_for_update().get(pk=ref.allocation_id)
    obj = Shipment.objects.select_for_update().get(pk=pk)
    if obj.status != 'draft' or allocation.status not in ['allocated','partially_shipped'] or not obj.lines.exists():
        raise ValidationError('Pengiriman harus draft dengan alokasi dan rincian yang valid.')
    lock_lots(obj.lines.values_list('allocation_line__lot_id',flat=True))
    for line in obj.lines.select_related('allocation_line__lot','allocation_line__warehouse'):
        al = line.allocation_line
        q = (line.rolls,line.yards)
        enough(allocation_remaining(al),q,'Sisa reservasi')
        enough(balance(al.lot,'physical',al.warehouse),q,'Stok fisik')
        move(actor,al.lot,'physical',al.warehouse,q,'dispatch',order=order,shipment=obj,shipment_line=line,sign=-1)
        move(actor,al.lot,'reserved',al.warehouse,q,'dispatch',order=order,shipment=obj,shipment_line=line,allocation=allocation,sign=-1)
        move(actor,al.lot,'transit',allocation.cmt,q,'dispatch',order=order,shipment=obj,shipment_line=line)
    transition(actor,obj,'dispatched','dispatch')
    refresh_allocation(actor,allocation)
    refresh_order(actor,order)
    return obj

def shipment_balance(line):
    return balance(line.allocation_line.lot,'transit',shipment_line=line)

def refresh_shipment(actor, obj):
    if obj.status in ['draft','reversed']:
        return
    if Discrepancy.objects.filter(shipment_line__shipment=obj,resolved=False).exists():
        status = 'discrepancy'
    elif all(shipment_balance(line) == (0,ZERO) for line in obj.lines.all()):
        status = 'received'
    elif CMTReceipt.objects.filter(shipment_line__shipment=obj,reversed=False).exists():
        status = 'partially_received'
    else:
        status = 'dispatched'
    if obj.status != status:
        transition(actor,obj,status,'receipt_status')

@command([P], CMTReceipt)
def receive_cmt(actor, line, data):
    allocation = line.shipment.allocation
    order = lock_orders(allocation.order_id)[allocation.order_id]
    shipment = Shipment.objects.select_for_update().get(pk=line.shipment_id)
    if shipment.status not in ['dispatched','partially_received','discrepancy']:
        raise ValidationError('Pengiriman belum diberangkatkan atau sudah selesai.')
    report(data, data['condition'] != 'good')
    if data['report_date'] < shipment.sent_date:
        raise ValidationError('Tanggal penerimaan mendahului pengiriman.')
    lock_lots([line.allocation_line.lot_id])
    q = qty(data['rolls'],data['yards'],allow_zero=data['condition'] != 'good')
    enough(shipment_balance(line),q,'Bahan dalam perjalanan')
    obj = CMTReceipt.objects.create(created_by=actor,shipment_line=line,**data)
    if q != (0,ZERO):
        move(actor,line.allocation_line.lot,'transit',allocation.cmt,q,'cmt_receive',order=order,shipment=shipment,shipment_line=line,cmt_receipt=obj,sign=-1)
        move(actor,line.allocation_line.lot,'cmt',allocation.cmt,q,'cmt_receive',order=order,shipment=shipment,shipment_line=line,cmt_receipt=obj)
    if data['condition'] != 'good':
        if not data.get('notes','').strip():
            raise ValidationError('Jelaskan selisih atau kerusakan pada catatan.')
        Discrepancy.objects.create(created_by=actor,shipment_line=line,description=data['notes'],evidence=data['evidence'])
    audit(actor,'post',obj,after={'rolls':q[0],'yards':str(q[1]),'condition':obj.condition})
    refresh_shipment(actor,shipment)
    refresh_order(actor,order)
    return obj

@command([P], Progress)
def save_progress(actor, data):
    order = lock_orders(data['order'].pk)[data['order'].pk]
    if order.status == 'draft':
        raise ValidationError('Aktifkan PO terlebih dahulu.')
    active(data['cmt'],'cmt')
    if not order.allocations.filter(cmt=data['cmt'],status__in=['allocated','partially_shipped','fully_shipped','cancelled']).exists():
        raise ValidationError('CMT belum memiliki alokasi aktif untuk PO ini.')
    report(data)
    if data['reject'] > data['quantity']:
        raise ValidationError('Reject tidak boleh melebihi jumlah progres.')
    obj = Progress(created_by=actor,**data)
    obj.full_clean()
    obj.save()
    audit(actor,'post',obj,after={'stage':obj.stage,'quantity':obj.quantity})
    refresh_order(actor,order)
    return obj

@command([P], FinishedShipment)
def send_finished(actor, data):
    order = lock_orders(data['order'].pk)[data['order'].pk]
    if order.status == 'draft':
        raise ValidationError('Aktifkan PO terlebih dahulu.')
    active(data['cmt'],'cmt')
    active(data['warehouse'],'warehouse')
    if not order.allocations.filter(cmt=data['cmt'],status__in=['allocated','partially_shipped','fully_shipped','cancelled']).exists():
        raise ValidationError('CMT belum memiliki alokasi aktif untuk PO ini.')
    report(data)
    obj = FinishedShipment(created_by=actor,**data)
    obj.full_clean()
    obj.save()
    audit(actor,'post',obj,after={'quantity':obj.quantity,'delivery_note':obj.delivery_note})
    refresh_order(actor,order)
    return obj

def refresh_finished(actor, obj):
    received = obj.receipts.filter(reversed=False).aggregate(q=Sum('received'))['q'] or 0
    if obj.discrepancies.filter(resolved=False).exists():
        status = 'discrepancy'
    elif received >= obj.quantity:
        status = 'received'
    else:
        loss = Correction.objects.filter(kind='finished_loss',warehouse_receipt__shipment=obj).aggregate(q=Sum('rolls'))['q'] or 0
        status = 'received' if received+loss == obj.quantity else ('partially_received' if received else 'dispatched')
    if obj.status != status:
        transition(actor,obj,status,'receipt_status')

@command([P], WarehouseReceipt)
def receive_warehouse(actor, data):
    ref = data['shipment']
    order = lock_orders(ref.order_id)[ref.order_id]
    shipment = FinishedShipment.objects.select_for_update().get(pk=ref.pk)
    if shipment.status == 'received':
        raise ValidationError('Penerimaan pengiriman hasil sudah selesai.')
    report(data,data['discrepancy'])
    if data['report_date'] < shipment.report_date:
        raise ValidationError('Tanggal penerimaan mendahului pengiriman.')
    received = shipment.receipts.filter(reversed=False).aggregate(q=Sum('received'))['q'] or 0
    loss = Correction.objects.filter(kind='finished_loss',warehouse_receipt__shipment=shipment).aggregate(q=Sum('rolls'))['q'] or 0
    if data['received'] + received + loss > shipment.quantity:
        raise ValidationError('Penerimaan melebihi sisa jumlah yang dikirim.')
    obj = WarehouseReceipt(created_by=actor,**data)
    obj.full_clean()
    obj.save()
    if obj.discrepancy:
        if not obj.notes.strip():
            raise ValidationError('Catatan selisih wajib diisi.')
        Discrepancy.objects.create(created_by=actor,finished_shipment=shipment,description=obj.notes,evidence=obj.evidence)
    order.over_resolved = False
    order.save(update_fields=['over_resolved'])
    audit(actor,'post',obj,after={'received':obj.received,'good':obj.good,'reject':obj.reject})
    refresh_finished(actor,shipment)
    refresh_order(actor,order)
    return obj

@command([P], Reconciliation)
def reconcile(actor, data):
    target = data.get('target_allocation')
    orders = lock_orders(data['order'].pk,target.allocation.order_id if target else None)
    if target:
        locked_allocation = Allocation.objects.select_for_update().get(pk=target.allocation_id)
        target = AllocationLine.objects.get(pk=target.pk)
        target.allocation = locked_allocation
        data = {**data, 'target_allocation': target}
    order = orders[data['order'].pk]
    q = qty(data['rolls'],data['yards'])
    action = data['action']
    if action not in dict(Reconciliation._meta.get_field('action').choices):
        raise ValidationError('Tindakan rekonsiliasi tidak valid.')
    report(data,action in ['waste','damaged'])
    if action in ['waste','damaged','transfer','returned'] and not data.get('notes','').strip():
        raise ValidationError('Alasan tindakan wajib dicatat.')
    if action == 'transfer':
        if not target or target.allocation.source_order_id != order.pk or target.lot_id != data['lot'].pk or target.allocation.cmt_id != data['cmt'].pk or target.allocation.status not in ['allocated','partially_shipped']:
            raise ValidationError('Transfer harus memakai alokasi transfer aktif untuk lot, PO sumber, dan CMT yang sama.')
        Allocation.objects.select_for_update().get(pk=target.allocation_id)
    lock_lots([data['lot'].pk])
    enough(balance(data['lot'],'cmt',data['cmt'],order),q,'Bahan di CMT')
    if action == 'transfer':
        enough(allocation_remaining(target),q,'Sisa alokasi transfer')
    else:
        enough(available(data['lot'],data['cmt'],order),q,'Bahan CMT bebas reservasi')
    if action == 'returned':
        active(data.get('warehouse'),'warehouse')
    obj = Reconciliation(created_by=actor,**data)
    obj.full_clean()
    obj.save()
    move(actor,obj.lot,'cmt',obj.cmt,q,action,order=order,reconciliation=obj,sign=-1)
    if action == 'returned':
        move(actor,obj.lot,'physical',obj.warehouse,q,action,reconciliation=obj)
    elif action == 'transfer':
        move(actor,obj.lot,'reserved',obj.cmt,q,action,order=order,allocation=target.allocation,reconciliation=obj,sign=-1)
        move(actor,obj.lot,'cmt',obj.cmt,q,action,order=target.allocation.order,reconciliation=obj)
        refresh_allocation(actor,target.allocation)
    audit(actor,'post',obj,after={'action':action,'rolls':q[0],'yards':str(q[1])})
    for item in orders.values():
        refresh_order(actor,item)
    return obj

@command([P], Correction)
def reverse(actor, kind, pk, reason, evidence):
    if not reason.strip():
        raise ValidationError('Alasan reversal wajib diisi.')
    validate_evidence(evidence)
    models_map = {'receipt':Receipt,'shipment':Shipment,'cmt_receipt':CMTReceipt,'warehouse_receipt':WarehouseReceipt,'reconciliation':Reconciliation}
    if kind not in models_map:
        raise ValidationError('Jenis reversal tidak valid.')
    ref = models_map[kind].objects.get(pk=pk)
    ids = []
    if kind == 'shipment':
        ids = [ref.allocation.order_id]
    elif kind == 'cmt_receipt':
        ids = [ref.shipment_line.shipment.allocation.order_id]
    elif kind == 'warehouse_receipt':
        ids = [ref.shipment.order_id]
    elif kind == 'reconciliation':
        ids = [ref.order_id]
        if ref.target_allocation:
            raise ValidationError('Reversal transfer memerlukan transfer balik melalui alokasi baru.')
    orders = lock_orders(*ids)
    obj = models_map[kind].objects.select_for_update().get(pk=pk)
    if hasattr(obj,'reversed'):
        if obj.reversed:
            raise ValidationError('Transaksi sudah direversal.')
    elif obj.status not in ['posted','dispatched','partially_received','received','discrepancy']:
        raise ValidationError('Status transaksi tidak dapat direversal.')
    if kind == 'shipment' and (obj.allocation.status == 'cancelled' or CMTReceipt.objects.filter(shipment_line__shipment=obj,reversed=False).exists() or Correction.objects.filter(shipment=obj).exists()):
        raise ValidationError('Pengiriman sudah diterima atau dikoreksi. Reversal penerimaan CMT terlebih dahulu.')
    if kind == 'cmt_receipt' and obj.shipment_line.discrepancies.exists():
        raise ValidationError('Penerimaan terkait selisih harus diselesaikan melalui resolusi.')
    if kind == 'warehouse_receipt' and obj.shipment.discrepancies.exists():
        raise ValidationError('Penerimaan terkait selisih harus diselesaikan melalui resolusi.')
    movements_qs = Movement.objects.none() if kind == 'warehouse_receipt' else Movement.objects.filter(**{kind:obj},correction__isnull=True)
    if kind == 'shipment':
        movements_qs = movements_qs.filter(kind='dispatch')
    movements = list(movements_qs)
    lock_lots(m.lot_id for m in movements)
    deltas = defaultdict(lambda: [0,ZERO])
    for m in movements:
        group = (m.lot_id,m.bucket,m.location_id,m.order_id if m.bucket in ['cmt','transit','reserved'] else None)
        deltas[group][0] -= m.rolls
        deltas[group][1] -= m.yards
    for (lot,bucket,location,order_id), delta in deltas.items():
        have = balance(lot,bucket,location,order_id)
        if have[0]+delta[0] < 0 or have[1]+delta[1] < 0:
            raise ValidationError('Reversal ditolak karena bahan sudah digunakan oleh transaksi berikutnya.')
        if bucket in ['physical','cmt']:
            reserved = balance(lot,'reserved',location,order_id if bucket=='cmt' else None)
            if have[0]+delta[0] < reserved[0] or have[1]+delta[1] < reserved[1]:
                raise ValidationError('Reversal akan mengurangi stok yang sudah direservasi.')
    correction = Correction.objects.create(created_by=actor,kind='reverse_'+kind,reason=reason,evidence=evidence,**{kind:obj})
    for m in movements:
        Movement.objects.create(actor=actor,lot=m.lot,bucket=m.bucket,location=m.location,order=m.order,rolls=-m.rolls,yards=-m.yards,kind='reversal',correction=correction,allocation=m.allocation,shipment=m.shipment,shipment_line=m.shipment_line)
    if hasattr(obj,'reversed'):
        obj.reversed = True
        obj.save(update_fields=['reversed','updated_at'])
        audit(actor,'reverse',obj,after={'reason':reason,'correction':correction.pk})
    else:
        transition(actor,obj,'reversed','reverse',reason)
    if kind == 'shipment':
        refresh_allocation(actor,obj.allocation)
    elif kind == 'cmt_receipt':
        refresh_shipment(actor,obj.shipment_line.shipment)
    elif kind == 'warehouse_receipt':
        obj.shipment.order.over_resolved = False
        obj.shipment.order.save(update_fields=['over_resolved'])
        refresh_finished(actor,obj.shipment)
    for order in orders.values():
        order.refresh_from_db()
        refresh_order(actor,order)
    return correction

@command([P], Correction)
def adjust(actor, lot, warehouse, direction, rolls, yards, reason, evidence):
    if direction not in ['in','out'] or not reason.strip():
        raise ValidationError('Arah adjustment dan alasan wajib diisi.')
    active(warehouse,'warehouse')
    validate_evidence(evidence)
    lock_lots([lot.pk])
    q = qty(rolls,yards)
    before = balance(lot,'physical',warehouse)
    if direction == 'out':
        enough(available(lot,warehouse),q,'Stok bebas')
    obj = Correction.objects.create(created_by=actor,kind='adjustment_'+direction,reason=reason,evidence=evidence,lot=lot,warehouse=warehouse,rolls=q[0],yards=q[1])
    move(actor,lot,'physical',warehouse,q,obj.kind,correction=obj,sign=1 if direction=='in' else -1)
    audit(actor,'adjustment',obj,before={'rolls':before[0],'yards':str(before[1])},after={'rolls':balance(lot,'physical',warehouse)[0],'yards':str(balance(lot,'physical',warehouse)[1]),'reason':reason})
    return obj

@command([P], Discrepancy)
def resolve_discrepancy(actor, pk, resolution, reason, evidence, rolls=0, yards=ZERO):
    ref = Discrepancy.objects.get(pk=pk)
    order_id = ref.shipment_line.shipment.allocation.order_id if ref.shipment_line_id else ref.finished_shipment.order_id
    order = lock_orders(order_id)[order_id]
    obj = Discrepancy.objects.select_for_update().get(pk=pk)
    if obj.resolved or not reason.strip() or resolution not in ['confirmed','loss']:
        raise ValidationError('Pilih resolusi dan isi alasan untuk selisih yang masih terbuka.')
    validate_evidence(evidence)
    if obj.shipment_line:
        line = obj.shipment_line
        shipment = Shipment.objects.select_for_update().get(pk=line.shipment_id)
        lock_lots([line.allocation_line.lot_id])
        if resolution == 'loss':
            q = qty(rolls,yards)
            enough(shipment_balance(line),q,'Sisa dalam perjalanan')
            correction = Correction.objects.create(created_by=actor,kind='transit_loss',shipment=shipment,lot=line.allocation_line.lot,reason=reason,evidence=evidence,rolls=q[0],yards=q[1])
            move(actor,line.allocation_line.lot,'transit',shipment.allocation.cmt,q,'transit_loss',order=order,shipment=shipment,shipment_line=line,correction=correction,sign=-1)
        else:
            if shipment_balance(line) != (0,ZERO):
                raise ValidationError('Masih ada bahan dalam perjalanan. Catat penerimaan atau resolusi kehilangan.')
            correction = Correction.objects.create(created_by=actor,kind='discrepancy_confirmed',shipment=shipment,reason=reason,evidence=evidence)
    else:
        shipment = FinishedShipment.objects.select_for_update().get(pk=obj.finished_shipment_id)
        received = shipment.receipts.filter(reversed=False).aggregate(q=Sum('received'))['q'] or 0
        losses = Correction.objects.filter(kind='finished_loss',warehouse_receipt__shipment=shipment).aggregate(q=Sum('rolls'))['q'] or 0
        if resolution == 'loss':
            count, _ = qty(rolls,ZERO)
            if count > shipment.quantity-received-losses:
                raise ValidationError('Kehilangan melebihi sisa hasil dikirim.')
            receipt = shipment.receipts.filter(reversed=False).last()
            correction = Correction.objects.create(created_by=actor,kind='finished_loss',warehouse_receipt=receipt,reason=reason,evidence=evidence,rolls=count)
        else:
            if received+losses != shipment.quantity:
                raise ValidationError('Sisa pengiriman hasil belum selesai diterima atau diselesaikan.')
            correction = Correction.objects.create(created_by=actor,kind='discrepancy_confirmed',order=order,reason=reason,evidence=evidence)
    obj.resolved = True
    obj.resolution = reason
    obj.correction = correction
    obj.save(update_fields=['resolved','resolution','correction','updated_at'])
    audit(actor,'resolve',obj,after={'resolution':resolution,'reason':reason,'correction':correction.pk})
    if obj.shipment_line_id:
        refresh_shipment(actor,shipment)
    else:
        refresh_finished(actor,shipment)
    refresh_order(actor,order)
    return obj