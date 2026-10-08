from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tracking.models import Invoice, Master, User
from tracking.services import (
    ajukan_alokasi,
    kirim_gudang,
    master,
    putuskan_alokasi,
    simpan_invoice,
    tautkan_po,
    terima_alokasi,
)
from tracking.size_services import simpan_ukuran


class Command(BaseCommand):
    help = 'Buat data contoh lokal untuk meninjau tampilan.'

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError('seed_contoh hanya tersedia saat DEBUG=true.')
        user, created = User.objects.get_or_create(
            username='contoh_purchasing', defaults={'role': 'purchasing'}
        )
        if created:
            user.set_password('contoh123')
            user.save()
        if Invoice.objects.filter(nomor='INV-CONTOH').exists():
            self.stdout.write('Data contoh sudah ada.')
            return
        today = timezone.localdate()
        cmt = master('cmt', 'Ling Ling')
        invoice = simpan_invoice(
            {
                'vendor': 'Vendor Contoh',
                'nomor': 'INV-CONTOH',
                'surat_jalan': 'SJ-CONTOH',
                'tanggal': today - timedelta(days=30),
                'total_rp': Decimal('15000000'),
            },
            [
                {
                    'material': 'Cotton',
                    'color': 'Cream',
                    'lokasi': 'Gudang Utama',
                    'yards': [Decimal('118.20')] * 15,
                }
            ],
            user,
        )
        allocation = ajukan_alokasi(list(invoice.roll_set.values_list('id', flat=True)), cmt, user)
        putuskan_alokasi(allocation, 'acc', user, tgl_kirim=today - timedelta(days=20))
        ids = list(invoice.roll_set.values_list('id', flat=True))
        terima_alokasi(allocation, ids, today - timedelta(days=19), user)
        po = tautkan_po(allocation, 'PO 109', user, produk='Kemeja contoh')
        color = Master.objects.get(kind='color', name='Cream')
        material = invoice.roll_set.select_related('material').first().material
        hasil = simpan_ukuran(po, color, material, [{'label': 'All Size', 'pcs': 1072}], user)
        size = hasil.sizes.get()
        for index, pcs in enumerate([430, 247, 250, 145]):
            kirim_gudang(
                po,
                color,
                {
                    'tanggal': today,
                    'sizes': [{'id': size.pk, 'pcs': pcs}],
                    'request_id': f'contoh-{po.pk}-{index}',
                },
                user,
                material=material,
            )
        self.stdout.write(
            self.style.SUCCESS('Data contoh dibuat. Akun: contoh_purchasing / contoh123')
        )
