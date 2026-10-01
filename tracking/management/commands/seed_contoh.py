from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tracking.models import Hasil, Invoice, KirimGudang, Master, User
from tracking.services import (
    ajukan_alokasi,
    master,
    pindah_status,
    putuskan_alokasi,
    simpan_invoice,
)


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
                'catatan': '',
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
        allocation = ajukan_alokasi(
            list(invoice.roll_set.values_list('id', flat=True)), 'PO 109', cmt, user
        )
        putuskan_alokasi(allocation, 'acc', user)
        ids = list(invoice.roll_set.values_list('id', flat=True))
        pindah_status(ids, 'dikirim', user, tgl_kirim=today - timedelta(days=20))
        pindah_status(ids, 'diterima', user, tgl_terima=today - timedelta(days=19))
        pindah_status(ids, 'terpakai', user)
        color = Master.objects.get(kind='color', name='Cream')
        Hasil.objects.create(po=allocation.po, color=color, pcs=1072)
        for pcs in [430, 247, 250, 145]:
            KirimGudang.objects.create(po=allocation.po, color=color, tanggal=today, pcs=pcs)
        self.stdout.write(
            self.style.SUCCESS('Data contoh dibuat. Akun: contoh_purchasing / contoh123')
        )
