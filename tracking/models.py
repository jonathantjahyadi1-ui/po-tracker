import re

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone


def normalize_name(value):
    return ' '.join(value.split())


def normalize_po(value):
    value = normalize_name(value).upper()
    return re.sub(r'^PO[\s_/-]*(?=\S)', 'PO ', value)


class User(AbstractUser):
    class Role(models.TextChoices):
        PURCHASING = 'purchasing', 'Purchasing'
        DIREKTUR = 'direktur', 'Direktur'
        ADMIN = 'admin', 'Admin'
        ACCOUNTING = 'accounting', 'Accounting'

    role = models.CharField(max_length=12, choices=Role.choices, default=Role.DIREKTUR)
    created_at = models.DateTimeField(default=timezone.now)

    def save(self, *args, **kwargs):
        if self.is_superuser:
            self.role = self.Role.ADMIN
        super().save(*args, **kwargs)


class Dated(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        abstract = True


class Master(Dated):
    class Kind(models.TextChoices):
        VENDOR = 'vendor', 'Vendor'
        CMT = 'cmt', 'CMT'
        MATERIAL = 'material', 'Bahan'
        COLOR = 'color', 'Warna'
        WAREHOUSE = 'warehouse', 'Gudang'
        PRODUCT = 'product', 'Produk'

    kind = models.CharField(max_length=12, choices=Kind.choices)
    name = models.CharField(max_length=160)
    active = models.BooleanField(default=True)
    price_per_yard = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ['kind', 'name']
        constraints = [
            models.UniqueConstraint('kind', Lower('name'), name='master_kind_name_ci'),
            models.CheckConstraint(
                condition=Q(price_per_yard__isnull=True) | Q(price_per_yard__gt=0),
                name='master_price_positive',
            ),
        ]

    def save(self, *args, **kwargs):
        self.name = normalize_name(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Po(Dated):
    product = models.ForeignKey(
        Master, on_delete=models.PROTECT, null=True, blank=True, related_name='product_orders'
    )
    legacy_completed = models.BooleanField(default=False)
    nomor = models.CharField(max_length=80, unique=True)
    cmt = models.ForeignKey(
        Master, on_delete=models.PROTECT, null=True, blank=True, related_name='purchase_orders'
    )
    produk = models.CharField(max_length=160, blank=True)
    tgl_order = models.DateField(null=True, blank=True)
    pemakaian_std = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    selesai = models.BooleanField(default=False)
    catatan = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        self.nomor = normalize_po(self.nomor)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.nomor


class Invoice(Dated):
    vendor = models.ForeignKey(Master, on_delete=models.PROTECT)
    nomor = models.CharField(max_length=80)
    surat_jalan = models.CharField(max_length=80, blank=True)
    tanggal = models.DateField()
    total_rp = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    payment_reconciled = models.BooleanField(default=True)
    catatan = models.TextField(blank=True)
    dibatalkan = models.BooleanField(default=False)
    dibuat_oleh = models.ForeignKey(User, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint('vendor', Lower('nomor'), name='invoice_vendor_nomor_ci'),
            models.CheckConstraint(condition=Q(total_rp__gte=0), name='invoice_total_nonnegative'),
        ]

    def __str__(self):
        return self.nomor


class InvoiceAttachment(Dated):
    invoice = models.OneToOneField(Invoice, on_delete=models.PROTECT, related_name='attachment')
    filename = models.CharField(max_length=180)
    content_type = models.CharField(max_length=40)
    size = models.PositiveIntegerField()
    content = models.BinaryField()


class InvoiceWrite(Dated):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='write_requests')
    request_id = models.CharField(max_length=64, unique=True)
    fingerprint = models.CharField(max_length=64)


class InvoicePayment(Dated):
    class Kind(models.TextChoices):
        LUNAS = 'lunas', 'Lunas'
        CICIL = 'cicil', 'Cicil'

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='payments')
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    payment_date = models.DateField()
    kind = models.CharField(max_length=5, choices=Kind.choices)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='invoice_payments')
    request_id = models.CharField(max_length=64, unique=True)
    fingerprint = models.CharField(max_length=64)
    proof_filename = models.CharField(max_length=180)
    proof_content_type = models.CharField(max_length=40)
    proof_size = models.PositiveIntegerField()
    proof_content = models.BinaryField()

    class Meta:
        ordering = ['payment_date', 'created_at', 'pk']
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name='invoice_payment_positive'),
            models.CheckConstraint(
                condition=Q(kind__in=['lunas', 'cicil']), name='invoice_payment_kind'
            ),
        ]
        indexes = [
            models.Index(fields=['invoice', 'payment_date'], name='invoice_payment_date_idx')
        ]


class InvoicePo(Dated):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='po_groups')
    urut = models.PositiveSmallIntegerField()
    po = models.ForeignKey(Po, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        ordering = ['urut']
        constraints = [
            models.CheckConstraint(
                condition=Q(urut__gte=1) & Q(urut__lte=10), name='invoice_po_urut_1_10'
            ),
            models.UniqueConstraint(fields=['invoice', 'urut'], name='invoice_po_urut_unique'),
            models.UniqueConstraint(
                fields=['invoice', 'po'],
                condition=Q(po__isnull=False),
                name='invoice_po_nomor_unique',
            ),
        ]


class InvoiceMaterial(Dated):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='material_groups')
    material = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    urut = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ['urut', 'pk']
        constraints = [
            models.UniqueConstraint(
                fields=['invoice', 'urut'], name='invoice_material_urut_unique'
            ),
            models.CheckConstraint(condition=Q(urut__gte=1), name='invoice_material_urut_positive'),
        ]


class InvoiceColor(Dated):
    group = models.ForeignKey(InvoiceMaterial, on_delete=models.PROTECT, related_name='color_rows')
    color = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    lokasi = models.ForeignKey(
        Master, on_delete=models.PROTECT, null=True, blank=True, related_name='+'
    )
    urut = models.PositiveIntegerField()

    class Meta:
        ordering = ['urut', 'pk']
        constraints = [
            models.UniqueConstraint(fields=['group', 'urut'], name='invoice_color_urut_unique'),
            models.CheckConstraint(condition=Q(urut__gte=1), name='invoice_color_urut_positive'),
        ]


class Alokasi(Dated):
    class Status(models.TextChoices):
        MENUNGGU = 'menunggu', 'Menunggu'
        DISETUJUI = 'disetujui', 'Disetujui'
        DITOLAK = 'ditolak', 'Ditolak'
        DIBATALKAN = 'dibatalkan', 'Dibatalkan'

    po = models.ForeignKey(Po, on_delete=models.PROTECT, null=True, blank=True)
    cmt = models.ForeignKey(Master, on_delete=models.PROTECT)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.MENUNGGU)
    dibuat_oleh = models.ForeignKey(User, on_delete=models.PROTECT, related_name='alokasi_dibuat')
    acc_oleh = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name='alokasi_acc', null=True, blank=True
    )
    acc_pada = models.DateTimeField(null=True, blank=True)
    alasan = models.TextField(blank=True)
    tgl_kirim = models.DateField(null=True, blank=True)
    sj_kirim = models.CharField(max_length=80, blank=True)


class Roll(Dated):
    class Status(models.TextChoices):
        TERSEDIA = 'tersedia', 'Tersedia'
        MENUNGGU = 'menunggu', 'Menunggu'
        SIAP_KIRIM = 'siap_kirim', 'Siap kirim'
        DIKIRIM = 'dikirim', 'Dikirim'
        DITERIMA = 'diterima', 'Diterima'
        TERPAKAI = 'terpakai', 'Terpakai'
        RUSAK = 'rusak', 'Rusak'
        DIRETUR = 'diretur', 'Diretur ke vendor'

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT)
    invoice_po = models.ForeignKey(
        InvoicePo, on_delete=models.PROTECT, null=True, blank=True, related_name='rolls'
    )
    invoice_color = models.ForeignKey(
        InvoiceColor, on_delete=models.PROTECT, null=True, blank=True, related_name='rolls'
    )
    material = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    color = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    lokasi = models.ForeignKey(
        Master, on_delete=models.PROTECT, related_name='+', null=True, blank=True
    )
    urut = models.PositiveIntegerField()
    yard = models.DecimalField(max_digits=14, decimal_places=6)
    # Transactional cache of active BS items; yard remains the original receipt.
    bs_yard = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.TERSEDIA)
    alokasi = models.ForeignKey(Alokasi, on_delete=models.PROTECT, null=True, blank=True)
    tgl_kirim = models.DateField(null=True, blank=True)
    sj_kirim = models.CharField(max_length=80, blank=True)
    tgl_terima = models.DateField(null=True, blank=True)
    catatan = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(yard__gt=0), name='roll_yard_positive'),
            models.CheckConstraint(
                condition=Q(bs_yard__gte=0) & Q(bs_yard__lte=models.F('yard')),
                name='roll_bs_within_yard',
            ),
            models.UniqueConstraint(
                fields=['invoice', 'material', 'color', 'urut'], name='roll_urut_unique'
            ),
        ]
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['invoice', 'material', 'color']),
            models.Index(fields=['alokasi']),
        ]

    @property
    def usable_yard(self):
        return self.yard - self.bs_yard

    @property
    def bs_label(self):
        return ('Sebagian BS' if self.usable_yard > 0 else 'BS') if self.bs_yard else ''


class FabricDefect(Dated):
    number = models.CharField(max_length=60, unique=True)
    date = models.DateField()
    cmt = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    note = models.TextField()
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    request_id = models.CharField(max_length=64, unique=True)
    fingerprint = models.CharField(max_length=64)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name='+', null=True, blank=True
    )
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        ordering = ['-date', '-pk']


class FabricDefectItem(Dated):
    defect = models.ForeignKey(FabricDefect, on_delete=models.PROTECT, related_name='items')
    roll = models.ForeignKey(Roll, on_delete=models.PROTECT, related_name='defect_items')
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='defect_items')
    vendor = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    po = models.ForeignKey(Po, on_delete=models.PROTECT, null=True, blank=True)
    yard = models.DecimalField(max_digits=14, decimal_places=6)
    price = models.DecimalField(max_digits=16, decimal_places=2)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    return_date = models.DateField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    returned_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name='+', null=True, blank=True
    )
    return_note = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['defect', 'roll'], name='defect_roll_unique'),
            models.CheckConstraint(condition=Q(yard__gt=0), name='defect_yard_positive'),
            models.CheckConstraint(condition=Q(price__gt=0), name='defect_price_positive'),
            models.CheckConstraint(condition=Q(amount__gt=0), name='defect_amount_positive'),
            models.CheckConstraint(
                condition=(
                    Q(returned_at__isnull=True, return_date__isnull=True, returned_by__isnull=True)
                    | Q(
                        returned_at__isnull=False,
                        return_date__isnull=False,
                        returned_by__isnull=False,
                    )
                ),
                name='defect_return_consistent',
            ),
        ]


class VendorRefund(Dated):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='refunds')
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    date = models.DateField()
    note = models.TextField(blank=True)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    request_id = models.CharField(max_length=64, unique=True)
    fingerprint = models.CharField(max_length=64)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name='+', null=True, blank=True
    )
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        ordering = ['date', 'created_at', 'pk']
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name='refund_amount_positive'),
        ]


class AlokasiRoll(Dated):
    """Preserve shipment membership after a rejected reservation releases its roll."""

    alokasi = models.ForeignKey(Alokasi, on_delete=models.PROTECT, related_name='items')
    roll = models.ForeignKey(Roll, on_delete=models.PROTECT, related_name='allocation_history')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['alokasi', 'roll'], name='alokasi_roll_unique')
        ]


class Hasil(Dated):
    sizes_complete = models.BooleanField(default=False)
    po = models.ForeignKey(Po, on_delete=models.PROTECT)
    material = models.ForeignKey(
        Master, on_delete=models.PROTECT, related_name='+', null=True, blank=True
    )
    color = models.ForeignKey(Master, on_delete=models.PROTECT)
    pcs = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['po', 'material', 'color'], name='hasil_po_material_color'
            ),
            models.UniqueConstraint(
                fields=['po', 'color'],
                condition=Q(material__isnull=True),
                name='hasil_po_color_legacy',
            ),
        ]


class KirimGudang(Dated):
    po = models.ForeignKey(Po, on_delete=models.PROTECT)
    material = models.ForeignKey(
        Master, on_delete=models.PROTECT, related_name='+', null=True, blank=True
    )
    color = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    tanggal = models.DateField()
    pcs = models.PositiveIntegerField()
    gudang = models.ForeignKey(
        Master, on_delete=models.PROTECT, related_name='+', null=True, blank=True
    )
    surat_jalan = models.CharField(max_length=80, blank=True)
    catatan = models.TextField(blank=True)
    request_id = models.CharField(max_length=64, unique=True, null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(pcs__gt=0), name='kirim_pcs_positive')]
        indexes = [
            models.Index(fields=['po', 'color']),
            models.Index(fields=['po', 'material', 'color']),
        ]


class Log(Dated):
    waktu = models.DateTimeField(default=timezone.now)
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    aksi = models.CharField(max_length=60)
    objek = models.CharField(max_length=200)
    detail = models.TextField(blank=True)


class MaterialUsage(Dated):
    product = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='material_usages')
    material = models.ForeignKey(
        Master, on_delete=models.PROTECT, related_name='+', null=True, blank=True
    )
    usage_yard_per_pcs = models.DecimalField(max_digits=20, decimal_places=8)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    updated_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='+')
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['material__name', 'pk']
        constraints = [
            models.UniqueConstraint(fields=['product', 'material'], name='usage_product_material'),
            models.UniqueConstraint(
                fields=['product'],
                condition=Q(material__isnull=True),
                name='usage_product_default',
            ),
            models.CheckConstraint(condition=Q(usage_yard_per_pcs__gt=0), name='usage_positive'),
        ]


class ProductionUnit(Dated):
    class Continuation(models.TextChoices):
        UNDECIDED = 'undecided', 'Belum ditentukan'
        FOLLOW_UP = 'follow_up', 'Masih ada kiriman menyusul'
        FINAL = 'final', 'Pengiriman sudah final'

    po = models.ForeignKey(Po, on_delete=models.PROTECT, related_name='production_units')
    product = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    material = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    color = models.ForeignKey(Master, on_delete=models.PROTECT, related_name='+')
    usage_source = models.ForeignKey(MaterialUsage, on_delete=models.PROTECT, related_name='units')
    usage_snapshot = models.DecimalField(max_digits=20, decimal_places=8)
    snapshot_at = models.DateTimeField(default=timezone.now)
    rounding = models.CharField(max_length=7, default='HALF_UP', editable=False)
    continuation = models.CharField(
        max_length=12, choices=Continuation.choices, default=Continuation.UNDECIDED
    )
    discrepancy_note = models.TextField(blank=True)
    finalized_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name='+', null=True, blank=True
    )
    finalized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['po', 'material', 'color'], name='unit_po_material_color'
            ),
            models.CheckConstraint(condition=Q(usage_snapshot__gt=0), name='unit_usage_positive'),
            models.CheckConstraint(condition=Q(rounding='HALF_UP'), name='unit_rounding_half_up'),
            models.CheckConstraint(
                condition=Q(continuation__in=['undecided', 'follow_up', 'final']),
                name='unit_continuation_valid',
            ),
            models.CheckConstraint(
                condition=(
                    Q(continuation='final', finalized_at__isnull=False, finalized_by__isnull=False)
                    | (
                        ~Q(continuation='final')
                        & Q(finalized_at__isnull=True, finalized_by__isnull=True)
                    )
                ),
                name='unit_finalization_consistent',
            ),
        ]


class HasilUkuran(Dated):
    hasil = models.ForeignKey(Hasil, on_delete=models.PROTECT, related_name='sizes')
    label = models.CharField(max_length=80)
    key = models.CharField(max_length=80)
    pcs = models.PositiveIntegerField()
    urut = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['urut', 'pk']
        constraints = [
            models.UniqueConstraint(fields=['hasil', 'key'], name='hasil_size_key_unique'),
            models.CheckConstraint(condition=~Q(key=''), name='hasil_size_key_required'),
        ]

    def save(self, *args, **kwargs):
        self.label = normalize_name(self.label)
        self.key = self.label.upper()
        super().save(*args, **kwargs)


class KirimUkuran(Dated):
    kiriman = models.ForeignKey(KirimGudang, on_delete=models.PROTECT, related_name='sizes')
    ukuran = models.ForeignKey(HasilUkuran, on_delete=models.PROTECT, related_name='shipments')
    pcs = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['kiriman', 'ukuran'], name='shipment_size_unique'),
            models.CheckConstraint(condition=Q(pcs__gt=0), name='shipment_size_positive'),
        ]
