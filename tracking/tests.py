from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from threading import Barrier

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from .models import Hasil, Invoice, KirimGudang, Master, Po, Roll, User, normalize_po
from .parsers import import_yards, parse_yards, suspicious_yards
from .services import (
    ajukan_alokasi,
    done,
    pemakaian,
    pindah_status,
    po_balance,
    putuskan_alokasi,
    selesaikan_po,
    simpan_invoice,
    yard_po,
)


class ParserTests(TestCase):
    def test_formats_header_and_invalid_values(self):
        values, errors = parse_yards('Yard\n117.00 117,5 1.773 1.773,25 46')
        self.assertEqual(
            values,
            [
                Decimal('117.00'),
                Decimal('117.50'),
                Decimal('1773.00'),
                Decimal('1773.25'),
                Decimal('46.00'),
            ],
        )
        self.assertEqual(errors, [])
        values, errors = parse_yards('117.00; 93.00\n46.00\t83.00 60.00')
        self.assertEqual(sum(values), Decimal('399.00'))
        self.assertEqual(len(values), 5)
        self.assertEqual(errors, [])
        _, errors = parse_yards('abc\n0 -1')
        self.assertEqual(len(errors), 3)

    def test_suspicious_values(self):
        self.assertEqual(
            suspicious_yards([Decimal('100'), Decimal('100'), Decimal('400'), Decimal('10')]),
            [3, 4],
        )

    def test_import_csv_and_xlsx(self):
        csv_file = SimpleUploadedFile('yard.csv', 'Yard\n1.773,25\n117,5\n\n46'.encode())
        values, errors = import_yards(csv_file)
        self.assertEqual(values, [Decimal('1773.25'), Decimal('117.50'), Decimal('46.00')])
        self.assertEqual(errors, [])
        book = Workbook()
        book.active.append(['Yard'])
        book.active.append([117])
        book.active.append([93])
        output = BytesIO()
        book.save(output)
        values, errors = import_yards(SimpleUploadedFile('yard.xlsx', output.getvalue()))
        self.assertEqual(values, [Decimal('117.00'), Decimal('93.00')])
        self.assertEqual(errors, [])


class WorkflowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='buyer', password='password123', role='purchasing'
        )
        self.director = User.objects.create_user(
            username='director', password='password123', role='direktur'
        )
        self.admin = User.objects.create_superuser(username='admin', password='password123')
        self.cmt = Master.objects.create(kind='cmt', name='Ling Ling')
        self.color = Master.objects.create(kind='color', name='Cream')
        self.today = timezone.localdate()
        self.invoice = simpan_invoice(
            {
                'vendor': 'Vendor A',
                'nomor': 'INV-1',
                'surat_jalan': '',
                'tanggal': self.today - timedelta(days=10),
                'total_rp': Decimal('1000'),
                'catatan': '',
            },
            [
                {
                    'material': 'Cotton',
                    'color': 'Cream',
                    'lokasi': '',
                    'yards': [Decimal('118.20')] * 15,
                }
            ],
            self.user,
        )
        self.rolls = list(self.invoice.roll_set.order_by('id'))

    def test_po_normalization_and_duplicate_master(self):
        for raw in ('po-109', 'PO 109', 'PO - 109', 'PO_109', 'PO/109'):
            self.assertEqual(normalize_po(raw), 'PO 109')
        self.assertNotEqual(normalize_po('PO 0111'), normalize_po('PO 111'))
        other = simpan_invoice(
            {
                'vendor': '  vendor  a ',
                'nomor': 'INV-2',
                'surat_jalan': '',
                'tanggal': self.today,
                'total_rp': Decimal('0'),
                'catatan': '',
            },
            [
                {
                    'material': 'COTTON',
                    'color': 'cream',
                    'lokasi': '',
                    'yards': [Decimal('100')],
                }
            ],
            self.user,
        )
        self.assertEqual(other.vendor_id, self.invoice.vendor_id)
        self.assertEqual(Master.objects.filter(kind='material').count(), 1)

    def test_allocation_collision_and_status_table(self):
        first = ajukan_alokasi([self.rolls[0].pk], 'po-109', self.cmt, self.user)
        self.assertEqual(first.po.nomor, 'PO 109')
        with self.assertRaisesMessage(ValidationError, 'baru saja dialokasikan'):
            ajukan_alokasi([self.rolls[0].pk], 'PO 110', self.cmt, self.user)
        putuskan_alokasi(first, 'acc', self.user)
        roll = Roll.objects.get(pk=self.rolls[0].pk)
        self.assertEqual(roll.status, 'siap_kirim')
        with self.assertRaises(ValidationError):
            pindah_status([roll.pk], 'terpakai', self.user)
        pindah_status([roll.pk], 'dikirim', self.user, tgl_kirim=self.today)
        with self.assertRaises(ValidationError):
            pindah_status([roll.pk], 'siap_kirim', self.user)
        pindah_status([roll.pk], 'siap_kirim', self.user, alasan='Salah kirim')
        pindah_status([roll.pk], 'dikirim', self.user, tgl_kirim=self.today)
        pindah_status([roll.pk], 'diterima', self.user, tgl_terima=self.today)
        with self.assertRaises(ValidationError):
            pindah_status([roll.pk], 'rusak', self.user)
        pindah_status([roll.pk], 'rusak', self.user, catatan='Sobek')
        pindah_status([roll.pk], 'diterima', self.user, alasan='Salah input')
        pindah_status([roll.pk], 'terpakai', self.user)
        pindah_status([roll.pk], 'diterima', self.user, alasan='Salah input')
        pindah_status([roll.pk], 'tersedia', self.user)
        self.assertIsNone(Roll.objects.get(pk=roll.pk).alokasi_id)

    def test_reject_and_cancel(self):
        allocation = ajukan_alokasi([self.rolls[0].pk], 'PO 109', self.cmt, self.user)
        with self.assertRaises(ValidationError):
            putuskan_alokasi(allocation, 'tolak', self.user)
        putuskan_alokasi(allocation, 'tolak', self.user, 'Salah PO')
        self.assertEqual(Roll.objects.get(pk=self.rolls[0].pk).status, 'tersedia')
        allocation = ajukan_alokasi([self.rolls[0].pk], 'PO 109', self.cmt, self.user)
        putuskan_alokasi(allocation, 'acc', self.user)
        putuskan_alokasi(allocation, 'batalkan', self.user, 'Tidak jadi')
        self.assertEqual(Roll.objects.get(pk=self.rolls[0].pk).status, 'tersedia')

    def test_shipment_date_rules(self):
        allocation = ajukan_alokasi([self.rolls[0].pk], 'PO 109', self.cmt, self.user)
        putuskan_alokasi(allocation, 'acc', self.user)
        with self.assertRaises(ValidationError):
            pindah_status(
                [self.rolls[0].pk],
                'dikirim',
                self.user,
                tgl_kirim=self.today + timedelta(days=2),
            )
        with self.assertRaises(ValidationError):
            pindah_status(
                [self.rolls[0].pk],
                'dikirim',
                self.user,
                tgl_kirim=self.invoice.tanggal - timedelta(days=1),
            )
        pindah_status([self.rolls[0].pk], 'dikirim', self.user, tgl_kirim=self.today)
        with self.assertRaises(ValidationError):
            pindah_status(
                [self.rolls[0].pk],
                'diterima',
                self.user,
                tgl_terima=self.today - timedelta(days=1),
            )

    def test_balance_and_usage(self):
        allocation = ajukan_alokasi([roll.pk for roll in self.rolls], 'PO 109', self.cmt, self.user)
        putuskan_alokasi(allocation, 'acc', self.user)
        ids = [roll.pk for roll in self.rolls]
        pindah_status(ids, 'dikirim', self.user, tgl_kirim=self.today)
        self.assertEqual(yard_po(allocation.po, self.color), Decimal('1773.00'))
        Hasil.objects.create(po=allocation.po, color=self.color, pcs=1072)
        self.assertEqual(pemakaian(allocation.po, self.color), Decimal('1.65'))
        for pcs in (430, 247, 250, 145):
            KirimGudang.objects.create(
                po=allocation.po, color=self.color, tanggal=self.today, pcs=pcs
            )
        self.assertEqual(done(allocation.po, self.color), 0)
        self.assertTrue(po_balance(allocation.po))
        with self.assertRaises(ValidationError):
            selesaikan_po(allocation.po, self.user)
        pindah_status(ids, 'diterima', self.user, tgl_terima=self.today)
        pindah_status(ids, 'terpakai', self.user)
        selesaikan_po(allocation.po, self.user)
        allocation.po.refresh_from_db()
        self.assertTrue(allocation.po.selesai)

    def test_balance_requires_every_roll_color(self):
        allocation = ajukan_alokasi([self.rolls[0].pk], 'PO 109', self.cmt, self.user)
        Hasil.objects.create(po=allocation.po, color=self.color, pcs=0)
        other_color = Master.objects.create(kind='color', name='Blue')
        self.rolls[1].color = other_color
        self.rolls[1].save(update_fields=['color'])
        ajukan_alokasi([self.rolls[1].pk], 'PO 109', self.cmt, self.user)
        self.assertFalse(po_balance(allocation.po))

    def test_allocated_invoice_edit_and_detail_pages(self):
        allocation = ajukan_alokasi([self.rolls[0].pk], 'PO 109', self.cmt, self.user)
        self.client.force_login(self.user)
        for name, pk in (
            ('allocation_detail', allocation.pk),
            ('cmt_detail', self.cmt.pk),
            ('po_detail', allocation.po.pk),
            ('invoice_edit', self.invoice.pk),
        ):
            self.assertEqual(self.client.get(reverse(name, args=[pk])).status_code, 200)
        exported = self.client.get(reverse('invoice_export', args=[self.invoice.pk]))
        self.assertEqual(exported.status_code, 200)
        book = load_workbook(BytesIO(exported.content), read_only=True)
        self.assertEqual(book.active.max_row, 16)
        book.close()
        self.assertEqual(
            self.client.get(reverse('po_export', args=[allocation.po.pk])).status_code,
            200,
        )
        edit_url = reverse('invoice_edit', args=[self.invoice.pk])
        data = {
            'vendor': 'Vendor A',
            'nomor': 'INV-1',
            'surat_jalan': 'SJ-2',
            'tanggal': self.invoice.tanggal.isoformat(),
            'total_rp': '2000',
            'catatan': 'Revisi',
            'existing': '1',
            'material[]': ['Cotton'],
            'color[]': ['Blue'],
            'lokasi[]': [''],
            'catatan[]': [''],
            'yards[]': ['55'],
            f'existing-{self.rolls[0].pk}': '999',
        }
        self.assertEqual(self.client.post(edit_url, data).status_code, 200)
        self.assertEqual(Roll.objects.get(pk=self.rolls[0].pk).yard, Decimal('118.20'))
        data[f'existing-{self.rolls[0].pk}'] = '118.20'
        self.assertEqual(self.client.post(edit_url, data).status_code, 302)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.surat_jalan, 'SJ-2')
        self.assertEqual(self.invoice.roll_set.count(), 16)

    def test_query_budget(self):
        allocation = ajukan_alokasi([roll.pk for roll in self.rolls], 'PO 109', self.cmt, self.user)
        self.client.force_login(self.user)
        for url in (
            reverse('dashboard'),
            reverse('vendor_list'),
            reverse('vendor_detail', args=[self.invoice.vendor_id]),
            reverse('invoice_detail', args=[self.invoice.pk]),
            reverse('po_detail', args=[allocation.po.pk]),
        ):
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertLessEqual(len(queries), 15, url)

    def test_roles_and_pages(self):
        self.client.force_login(self.director)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)
        self.assertEqual(
            self.client.post(reverse('master'), {'kind': 'vendor', 'name': 'Other'}).status_code,
            403,
        )
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 403)
        self.assertEqual(self.client.get(reverse('accounts')).status_code, 200)
        self.client.force_login(self.user)
        for name in (
            'dashboard',
            'master',
            'vendor_list',
            'allocation_list',
            'cmt_list',
            'po_list',
            'invoice_new',
        ):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertEqual(
            self.client.get(reverse('invoice_detail', args=[self.invoice.pk])).status_code, 200
        )
        self.assertEqual(
            self.client.get(reverse('vendor_detail', args=[self.invoice.vendor_id])).status_code,
            200,
        )

    def test_invoice_post_paste(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('invoice_new'),
            {
                'vendor': 'Vendor Baru',
                'nomor': 'INV-NEW',
                'surat_jalan': '',
                'tanggal': self.today.isoformat(),
                'total_rp': '0',
                'catatan': '',
                'material[]': ['Cotton'],
                'color[]': ['Blue'],
                'lokasi[]': [''],
                'yards[]': ['117.00 93.00 46.00 83.00 60.00'],
            },
        )
        self.assertEqual(response.status_code, 302)
        invoice = Invoice.objects.get(nomor='INV-NEW')
        self.assertEqual(invoice.roll_set.count(), 5)
        self.assertEqual(sum(invoice.roll_set.values_list('yard', flat=True)), Decimal('399.00'))


class ConcurrencyTests(TransactionTestCase):
    def test_same_roll_cannot_be_allocated_twice(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Baris terkunci diuji pada PostgreSQL.')
        user = User.objects.create_user(username='buyer', password='password123', role='purchasing')
        cmt = Master.objects.create(kind='cmt', name='Ling Ling')
        invoice = simpan_invoice(
            {
                'vendor': 'Vendor A',
                'nomor': 'INV-1',
                'surat_jalan': '',
                'tanggal': timezone.localdate(),
                'total_rp': Decimal(0),
                'catatan': '',
            },
            [
                {
                    'material': 'Cotton',
                    'color': 'Cream',
                    'lokasi': '',
                    'yards': [Decimal(100)],
                }
            ],
            user,
        )
        roll_id = invoice.roll_set.values_list('id', flat=True).get()
        barrier = Barrier(2)

        def submit(number):
            barrier.wait()
            try:
                ajukan_alokasi([roll_id], f'PO {number}', cmt, user)
                return 'ok'
            except ValidationError:
                return 'conflict'

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, [109, 110]))
        self.assertCountEqual(results, ['ok', 'conflict'])
        self.assertEqual(Po.objects.count(), 1)
