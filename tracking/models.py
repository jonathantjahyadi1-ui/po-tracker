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

    kind = models.CharField(max_length=12, choices=Kind.choices)
    name = models.CharField(max_length=160)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['kind', 'name']
        constraints = [models.UniqueConstraint('kind', Lower('name'), name='master_kind_name_ci')]

    def save(self, *args, **kwargs):
        self.name = normalize_name(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Po(Dated):
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
    yard = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.TERSEDIA)
    alokasi = models.ForeignKey(Alokasi, on_delete=models.PROTECT, null=True, blank=True)
    tgl_kirim = models.DateField(null=True, blank=True)
    sj_kirim = models.CharField(max_length=80, blank=True)
    tgl_terima = models.DateField(null=True, blank=True)
    catatan = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(yard__gt=0), name='roll_yard_positive'),
            models.UniqueConstraint(
                fields=['invoice', 'material', 'color', 'urut'], name='roll_urut_unique'
            ),
        ]
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['invoice', 'material', 'color']),
            models.Index(fields=['alokasi']),
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
