import uuid
import re
from decimal import Decimal
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

ZERO = Decimal('0.00')

def normalized(value):
    return ' '.join(value.strip().upper().split())

def normalize_po(value):
    return re.sub(r'^PO(?:[\s_/-]+|(?=\d))', 'PO ', normalized(value))

class User(AbstractUser):
    class Role(models.TextChoices):
        PURCHASING = 'purchasing', 'Purchasing'
        DIRECTOR = 'director', 'Direktur'
        ADMIN = 'admin', 'Super Admin'
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.DIRECTOR)
    # Kept for existing backups; operational permissions now follow the Purchasing role.
    can_adjust = models.BooleanField(default=False)
    can_reopen = models.BooleanField(default=False)

    class Meta(AbstractUser.Meta):
        constraints = [models.CheckConstraint(
            condition=Q(role__in=['purchasing', 'director', 'admin']),
            name='valid_user_role',
        )]

class Record(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    version = models.PositiveIntegerField(default=1)
    class Meta:
        abstract = True
    def delete(self, *args, **kwargs):
        raise ValidationError('Data tidak dapat dihapus. Gunakan pembatalan atau nonaktifkan.')

class Master(Record):
    class Kind(models.TextChoices):
        VENDOR = 'vendor', 'Vendor'
        CMT = 'cmt', 'CMT'
        MATERIAL = 'material', 'Bahan'
        COLOR = 'color', 'Warna'
        UNIT = 'unit', 'Satuan'
        PRODUCT = 'product', 'Produk'
        TARGET = 'target', 'Target'
        WAREHOUSE = 'warehouse', 'Gudang'
    kind = models.CharField(max_length=20, choices=Kind.choices)
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=160)
    pic = models.CharField(max_length=100, blank=True)
    contact = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    unit = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    standard_usage = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    target_qty = models.PositiveIntegerField(null=True, blank=True)
    class Meta:
        ordering = ['kind', 'name']
        constraints = [models.UniqueConstraint(fields=['kind', 'code'], name='unique_master_kind_code'), models.CheckConstraint(condition=Q(target_qty__isnull=True) | Q(target_qty__gt=0), name='positive_master_target'), models.CheckConstraint(condition=Q(standard_usage__isnull=True) | Q(standard_usage__gte=0), name='positive_standard_usage')]
    def save(self, *args, **kwargs):
        self.code = normalized(self.code)
        self.name = self.name.strip()
        super().save(*args, **kwargs)
    def __str__(self):
        return f'{self.code} · {self.name}'

class Evidence(Record):
    name = models.CharField(max_length=240)
    mime_type = models.CharField(max_length=80)
    size = models.PositiveIntegerField()
    storage_key = models.CharField(max_length=300, unique=True)
    sha256 = models.CharField(max_length=64)
    backend = models.CharField(max_length=20, default='local')
    def __str__(self):
        return self.name

class Receipt(Record):
    vendor = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='receipts')
    invoice = models.CharField(max_length=80)
    delivery_note = models.CharField(max_length=80, blank=True)
    invoice_date = models.DateField(null=True, blank=True)
    received_date = models.DateField(default=timezone.localdate)
    invoice_total = models.DecimalField(max_digits=18, decimal_places=2, default=ZERO)
    warehouse = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    notes = models.TextField(blank=True)
    evidence = models.ForeignKey(Evidence, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    revision_of = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=30, default='draft', db_index=True)
    class Meta:
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=['vendor', 'invoice'], name='unique_vendor_invoice'), models.CheckConstraint(condition=Q(invoice_total__gte=0), name='receipt_total_positive')]
    def save(self, *args, **kwargs):
        self.invoice = normalized(self.invoice)
        super().save(*args, **kwargs)
    def __str__(self):
        return self.invoice

class Quantity(models.Model):
    rolls = models.PositiveIntegerField(default=0)
    yards = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    class Meta:
        abstract = True
        constraints = [models.CheckConstraint(condition=Q(yards__gte=0), name='%(class)s_yards_positive')]

class ReceiptLine(Quantity):
    receipt = models.ForeignKey(Receipt, on_delete=models.PROTECT, related_name='lines')
    material = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    color = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    unit = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    warehouse = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    notes = models.CharField(max_length=240, blank=True)
    def __str__(self):
        return f'{self.material.name} / {self.color.name}'

class Lot(Record):
    receipt_line = models.OneToOneField(ReceiptLine, on_delete=models.PROTECT, related_name='lot')
    code = models.CharField(max_length=50, unique=True)
    class Meta:
        ordering = ['-id']
    def __str__(self):
        return f'{self.code} · {self.receipt_line} · {self.receipt_line.receipt.invoice}'

class Order(Record):
    number = models.CharField(max_length=80, unique=True)
    product = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    target = models.PositiveIntegerField()
    cmts = models.ManyToManyField(Master, related_name='production_orders', blank=True)
    order_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField(null=True, blank=True)
    standard_usage = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=30, default='draft', db_index=True)
    over_resolved = models.BooleanField(default=False)
    archived = models.BooleanField(default=False)
    class Meta:
        ordering = ['-id']
        constraints = [models.CheckConstraint(condition=Q(target__gt=0), name='order_target_positive')]
    def save(self, *args, **kwargs):
        self.number = normalize_po(self.number)
        super().save(*args, **kwargs)
    def __str__(self):
        return self.number

class Allocation(Record):
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name='allocations')
    source_order = models.ForeignKey(Order, on_delete=models.PROTECT, null=True, blank=True, related_name='transfer_allocations')
    cmt = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    planned_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=30, default='draft', db_index=True)
    class Meta:
        ordering = ['-id']
        constraints = [models.CheckConstraint(
            condition=Q(status__in=['draft', 'allocated', 'partially_shipped', 'fully_shipped', 'cancelled', 'rejected']),
            name='valid_allocation_status',
        )]
    def __str__(self):
        return f'AL-{self.pk:05d}' if self.pk else 'Alokasi baru'

class AllocationLine(Quantity):
    allocation = models.ForeignKey(Allocation, on_delete=models.PROTECT, related_name='lines')
    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name='allocation_lines')
    warehouse = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    class Meta(Quantity.Meta):
        constraints = Quantity.Meta.constraints + [models.UniqueConstraint(fields=['allocation', 'lot', 'warehouse'], name='unique_allocation_lot_location')]

class AppendOnly(models.Model):
    class Meta:
        abstract = True
    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Riwayat bersifat permanen dan tidak dapat diubah.')
        super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValidationError('Riwayat tidak dapat dihapus.')

class Decision(AppendOnly):
    allocation = models.ForeignKey(Allocation, on_delete=models.PROTECT, related_name='decisions')
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    decision = models.CharField(max_length=30)
    notes = models.TextField(blank=True)
    version = models.PositiveIntegerField()

class Shipment(Record):
    allocation = models.ForeignKey(Allocation, on_delete=models.PROTECT, related_name='shipments')
    delivery_note = models.CharField(max_length=80)
    sent_date = models.DateField(default=timezone.localdate)
    evidence = models.ForeignKey(Evidence, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    status = models.CharField(max_length=30, default='draft', db_index=True)
    class Meta:
        ordering = ['-id']
    def __str__(self):
        return f'SJ-{self.pk:05d}' if self.pk else 'Pengiriman baru'

class ShipmentLine(Quantity):
    shipment = models.ForeignKey(Shipment, on_delete=models.PROTECT, related_name='lines')
    allocation_line = models.ForeignKey(AllocationLine, on_delete=models.PROTECT, related_name='shipment_lines')
    class Meta(Quantity.Meta):
        constraints = Quantity.Meta.constraints + [models.UniqueConstraint(fields=['shipment', 'allocation_line'], name='unique_shipment_allocation_line')]

class Report(Record):
    report_date = models.DateField(default=timezone.localdate)
    pic = models.CharField(max_length=100)
    medium = models.CharField(max_length=30, choices=[('whatsapp', 'WhatsApp'), ('phone', 'Telepon'), ('email', 'Email'), ('document', 'Dokumen'), ('direct', 'Langsung')])
    notes = models.TextField(blank=True)
    evidence = models.ForeignKey(Evidence, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    class Meta:
        abstract = True

class CMTReceipt(Report, Quantity):
    shipment_line = models.ForeignKey(ShipmentLine, on_delete=models.PROTECT, related_name='receipts')
    condition = models.CharField(max_length=30, choices=[('good', 'Baik'), ('discrepancy', 'Selisih'), ('damage', 'Rusak')], default='good')
    reversed = models.BooleanField(default=False)
    class Meta:
        constraints = [models.CheckConstraint(condition=Q(yards__gte=0), name='cmtreceipt_yards_positive')]
    def __str__(self):
        return f'TRM-CMT-{self.pk:05d}' if self.pk else 'Penerimaan CMT'

class Progress(Report):
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name='progress_updates')
    cmt = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    stage = models.CharField(max_length=30, choices=[('cutting', 'Potong'), ('sewing', 'Jahit'), ('finishing', 'Finishing'), ('qc', 'Pemeriksaan'), ('packing', 'Packing')])
    quantity = models.PositiveIntegerField(default=0)
    reject = models.PositiveIntegerField(default=0)
    eta = models.DateField(null=True, blank=True)
    def __str__(self):
        return f'PRG-{self.pk:05d} · {self.order}' if self.pk else 'Progres produksi'

class FinishedShipment(Report):
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name='finished_shipments')
    cmt = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    warehouse = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    delivery_note = models.CharField(max_length=80)
    quantity = models.PositiveIntegerField()
    status = models.CharField(max_length=30, default='dispatched')
    class Meta:
        ordering = ['-id']
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name='finished_shipment_positive')]
    def __str__(self):
        return f'HSL-{self.pk:05d} · {self.order}' if self.pk else 'Pengiriman hasil'

class WarehouseReceipt(Report):
    shipment = models.ForeignKey(FinishedShipment, on_delete=models.PROTECT, related_name='receipts')
    received = models.PositiveIntegerField()
    good = models.PositiveIntegerField()
    reject = models.PositiveIntegerField(default=0)
    discrepancy = models.BooleanField(default=False)
    reversed = models.BooleanField(default=False)
    class Meta:
        constraints = [models.CheckConstraint(condition=Q(received=models.F('good') + models.F('reject')), name='warehouse_good_reject_equal_received'), models.CheckConstraint(condition=Q(received__gt=0) | Q(discrepancy=True), name='warehouse_received_or_discrepancy')]
    def __str__(self):
        return f'TRM-GDG-{self.pk:05d}' if self.pk else 'Penerimaan gudang'

class Reconciliation(Report, Quantity):
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name='reconciliations')
    cmt = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name='reconciliations')
    action = models.CharField(max_length=30, choices=[('consumed', 'Terpakai'), ('returned', 'Retur'), ('waste', 'Waste'), ('damaged', 'Rusak'), ('transfer', 'Transfer ke PO')])
    warehouse = models.ForeignKey(Master, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    target_allocation = models.ForeignKey(AllocationLine, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    reversed = models.BooleanField(default=False)
    class Meta:
        constraints = [models.CheckConstraint(condition=Q(yards__gte=0), name='reconciliation_yards_positive')]
    def __str__(self):
        return f'REK-{self.pk:05d}' if self.pk else 'Rekonsiliasi'

class Correction(Record):
    kind = models.CharField(max_length=40)
    reason = models.TextField()
    evidence = models.ForeignKey(Evidence, on_delete=models.PROTECT, related_name='+')
    receipt = models.ForeignKey(Receipt, on_delete=models.PROTECT, null=True, blank=True)
    shipment = models.ForeignKey(Shipment, on_delete=models.PROTECT, null=True, blank=True)
    cmt_receipt = models.ForeignKey(CMTReceipt, on_delete=models.PROTECT, null=True, blank=True)
    warehouse_receipt = models.ForeignKey(WarehouseReceipt, on_delete=models.PROTECT, null=True, blank=True)
    reconciliation = models.ForeignKey(Reconciliation, on_delete=models.PROTECT, null=True, blank=True)
    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, null=True, blank=True)
    warehouse = models.ForeignKey(Master, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    order = models.ForeignKey(Order, on_delete=models.PROTECT, null=True, blank=True)
    cmt = models.ForeignKey(Master, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    rolls = models.IntegerField(default=0)
    yards = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    def __str__(self):
        return f'KOR-{self.pk:05d}' if self.pk else 'Koreksi'

class Discrepancy(Record):
    shipment_line = models.ForeignKey(ShipmentLine, on_delete=models.PROTECT, null=True, blank=True, related_name='discrepancies')
    finished_shipment = models.ForeignKey(FinishedShipment, on_delete=models.PROTECT, null=True, blank=True, related_name='discrepancies')
    description = models.TextField()
    evidence = models.ForeignKey(Evidence, on_delete=models.PROTECT, related_name='+')
    resolved = models.BooleanField(default=False)
    resolution = models.TextField(blank=True)
    correction = models.ForeignKey(Correction, on_delete=models.PROTECT, null=True, blank=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=(Q(shipment_line__isnull=False) & Q(finished_shipment__isnull=True)) | (Q(shipment_line__isnull=True) & Q(finished_shipment__isnull=False)), name='discrepancy_exactly_one_source')]
    def __str__(self):
        return f'SLS-{self.pk:05d}' if self.pk else 'Selisih'

class Movement(AppendOnly):
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name='movements')
    bucket = models.CharField(max_length=20, choices=[('physical', 'Fisik'), ('reserved', 'Reservasi'), ('transit', 'Perjalanan'), ('cmt', 'Di CMT')], db_index=True)
    location = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    order = models.ForeignKey(Order, on_delete=models.PROTECT, null=True, blank=True, related_name='movements')
    rolls = models.IntegerField()
    yards = models.DecimalField(max_digits=14, decimal_places=2)
    kind = models.CharField(max_length=40)
    receipt = models.ForeignKey(Receipt, on_delete=models.PROTECT, null=True, blank=True)
    allocation = models.ForeignKey(Allocation, on_delete=models.PROTECT, null=True, blank=True)
    shipment = models.ForeignKey(Shipment, on_delete=models.PROTECT, null=True, blank=True)
    shipment_line = models.ForeignKey(ShipmentLine, on_delete=models.PROTECT, null=True, blank=True)
    cmt_receipt = models.ForeignKey(CMTReceipt, on_delete=models.PROTECT, null=True, blank=True)
    reconciliation = models.ForeignKey(Reconciliation, on_delete=models.PROTECT, null=True, blank=True)
    correction = models.ForeignKey(Correction, on_delete=models.PROTECT, null=True, blank=True)
    class Meta:
        ordering = ['created_at', 'id']
        indexes = [models.Index(fields=['lot', 'bucket', 'location', 'order'])]
        constraints = [models.CheckConstraint(condition=Q(receipt__isnull=False) | Q(allocation__isnull=False) | Q(shipment__isnull=False) | Q(cmt_receipt__isnull=False) | Q(reconciliation__isnull=False) | Q(correction__isnull=False), name='movement_has_source'), models.CheckConstraint(condition=(Q(rolls__gte=0) & Q(yards__gte=0)) | (Q(rolls__lte=0) & Q(yards__lte=0)), name='movement_consistent_sign')]
    @property
    def reference(self):
        return self.correction or self.reconciliation or self.cmt_receipt or self.shipment or self.allocation or self.receipt

class Audit(AppendOnly):
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.ForeignKey(User, on_delete=models.PROTECT, null=True)
    role = models.CharField(max_length=20)
    action = models.CharField(max_length=40)
    entity = models.CharField(max_length=60)
    object_id = models.CharField(max_length=80)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    request_id = models.CharField(max_length=40, blank=True)
    class Meta:
        ordering = ['-id']

class Operation(models.Model):
    key = models.UUIDField(default=uuid.uuid4, unique=True)
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    action = models.CharField(max_length=80)
    result_id = models.PositiveBigIntegerField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

class LoginAttempt(models.Model):
    key = models.CharField(max_length=64, unique=True)
    failures = models.PositiveIntegerField(default=0)
    last_at = models.DateTimeField(default=timezone.now)
