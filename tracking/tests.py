from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from io import BytesIO, StringIO
from threading import Barrier
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from .models import (
    Alokasi,
    AlokasiRoll,
    Hasil,
    Invoice,
    InvoiceAttachment,
    InvoicePo,
    KirimGudang,
    Log,
    Master,
    Po,
    Roll,
    User,
    normalize_po,
)
from .parsers import import_yards, parse_yards, suspicious_yards
from .services import (
    ajukan_alokasi,
    done,
    kirim_gudang,
    map_legacy_production,
    pemakaian,
    pindah_status,
    po_balance,
    po_status,
    putuskan_alokasi,
    simpan_hasil,
    simpan_invoice,
    tautkan_po,
    terima_alokasi,
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
        self.assertEqual((sum(values), len(values), errors), (Decimal('399.00'), 5, []))
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
        for row in (['Yard'], [117], [93]):
            book.active.append(row)
        output = BytesIO()
        book.save(output)
        values, errors = import_yards(SimpleUploadedFile('yard.xlsx', output.getvalue()))
        self.assertEqual((values, errors), ([Decimal('117.00'), Decimal('93.00')], []))


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
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
        self.invoice = self.make_invoice('INV-1', yards=[Decimal('118.20')] * 15)
        self.rolls = list(self.invoice.roll_set.order_by('id'))

    def make_invoice(self, number, vendor='Vendor A', color='Cream', yards=None):
        return simpan_invoice(
            {
                'vendor': vendor,
                'nomor': number,
                'surat_jalan': '',
                'tanggal': self.today - timedelta(days=10),
                'total_rp': Decimal('1000'),
            },
            [
                {
                    'material': 'Cotton',
                    'color': color,
                    'lokasi': '',
                    'yards': yards if yards is not None else [Decimal('100')],
                }
            ],
            self.user,
        )

    def ready_po(self, rolls=None, number='PO 109'):
        ids = [roll.pk for roll in (rolls if rolls is not None else self.rolls)]
        allocation = ajukan_alokasi(ids, self.cmt, self.user)
        putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today)
        terima_alokasi(allocation, ids, self.today, self.user)
        po = tautkan_po(allocation, number, self.user)
        allocation.refresh_from_db()
        self.assertEqual(allocation.po_id, po.pk)
        return allocation, po

    def invoice_payload(self, number, **extra):
        payload = {
            'vendor': 'Vendor Baru',
            'nomor': number,
            'tanggal': self.today.isoformat(),
            'surat_jalan': '',
            'total_rp': '0',
            'material[]': ['Cotton'],
            'color[]': ['Cream'],
            'lokasi[]': [''],
            'yards[]': ['117 93'],
        }
        payload.update(extra)
        return payload

    def shipment(self, po, pcs, color=None, request_id=None):
        data = {'tanggal': self.today, 'pcs': pcs, 'surat_jalan': '', 'catatan': ''}
        if request_id is not None:
            data['request_id'] = request_id
        return kirim_gudang(po, color or self.color, data, self.user)

    def test_po_normalization_duplicate_master_and_invoice(self):
        for raw in ('po-109', 'PO 109', 'PO - 109', 'PO_109', 'PO/109'):
            self.assertEqual(normalize_po(raw), 'PO 109')
        self.assertNotEqual(normalize_po('PO 0111'), normalize_po('PO 111'))
        other = self.make_invoice('INV-2', vendor='  vendor  a ', color='cream')
        self.assertEqual(other.vendor_id, self.invoice.vendor_id)
        self.assertEqual(Master.objects.filter(kind='material').count(), 1)
        with self.assertRaises(ValidationError):
            self.make_invoice('  inv-1  ', vendor='vendor a')
        self.assertEqual(Invoice.objects.count(), 2)

    def test_allocation_without_po_and_collision(self):
        first = ajukan_alokasi([self.rolls[0].pk], self.cmt, self.user)
        self.assertIsNone(first.po_id)
        self.assertEqual(Po.objects.count(), 0)
        self.assertEqual(Roll.objects.get(pk=self.rolls[0].pk).status, 'menunggu')
        with self.assertRaisesMessage(ValidationError, 'baru saja dialokasikan'):
            ajukan_alokasi([self.rolls[0].pk], self.cmt, self.user)
        putuskan_alokasi(first, 'acc', self.user, tgl_kirim=self.today, sj_kirim='SJ-KAIN')
        roll = Roll.objects.get(pk=self.rolls[0].pk)
        self.assertEqual(
            (roll.status, roll.tgl_kirim, roll.sj_kirim), ('dikirim', self.today, 'SJ-KAIN')
        )
        first.refresh_from_db()
        self.assertEqual(first.acc_oleh_id, self.user.pk)
        self.assertIsNotNone(first.acc_pada)

    def test_http_partial_receipt_then_po(self):
        self.client.force_login(self.user)
        ids = [roll.pk for roll in self.rolls[:2]]
        self.assertEqual(
            self.client.get(reverse('invoice_detail', args=[self.invoice.pk])).status_code, 200
        )
        response = self.client.post(reverse('allocation_create'), {'roll': ids, 'cmt': self.cmt.pk})
        allocation = Roll.objects.get(pk=ids[0]).alokasi
        detail = reverse('allocation_detail', args=[allocation.pk])
        self.assertRedirects(response, detail)
        self.assertIsNone(allocation.po_id)
        self.assertEqual(Po.objects.count(), 0)
        for name in ('allocation_list', 'cmt_list', 'po_list', 'dashboard'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertRedirects(
            self.client.post(
                detail,
                {
                    'action': 'acc',
                    'tanggal': self.today.isoformat(),
                    'surat_jalan': 'SJ-KAIN',
                },
            ),
            detail,
        )
        self.assertEqual(
            set(Roll.objects.filter(pk__in=ids).values_list('status', flat=True)), {'dikirim'}
        )
        cmt_url = reverse('cmt_detail', args=[self.cmt.pk])
        self.assertContains(self.client.get(cmt_url), 'Belum diisi')
        receipt = {
            'action': 'receive',
            'allocation': allocation.pk,
            'roll': [ids[0]],
            'tanggal': self.today.isoformat(),
        }
        self.assertRedirects(
            self.client.post(cmt_url, receipt), f'{cmt_url}#pengiriman-{allocation.pk}'
        )
        self.assertEqual(Roll.objects.get(pk=ids[0]).status, 'diterima')
        self.assertEqual(Roll.objects.get(pk=ids[1]).status, 'dikirim')
        assignment = {
            'action': 'assign_po',
            'allocation': allocation.pk,
            'nomor_po': 'po-109',
            'produk': 'Kemeja',
        }
        self.client.post(cmt_url, assignment)
        self.assertEqual(Po.objects.count(), 0)
        receipt['roll'] = [ids[1]]
        self.client.post(cmt_url, receipt)
        self.client.post(cmt_url, assignment)
        allocation.refresh_from_db()
        self.assertEqual(
            (allocation.po.nomor, allocation.po.cmt_id, allocation.po.produk),
            ('PO 109', self.cmt.pk, 'Kemeja'),
        )
        self.assertContains(self.client.get(reverse('po_cmt_list', args=[self.cmt.pk])), 'PO 109')
        self.assertEqual(
            self.client.get(reverse('po_detail', args=[allocation.po_id])).status_code, 200
        )
        self.client.post(cmt_url, assignment)
        self.assertEqual(Po.objects.count(), 1)

    def test_cmt_link_and_posts_keep_older_allocation_on_its_page(self):
        invoice = self.make_invoice('INV-PAGED-CMT', yards=[Decimal('100')] * 21)
        rolls = list(invoice.roll_set.order_by('pk'))
        allocations = Alokasi.objects.bulk_create(
            [
                Alokasi(
                    cmt=self.cmt, dibuat_oleh=self.user, status='disetujui', tgl_kirim=self.today
                )
                for _ in rolls
            ]
        )
        for roll, allocation in zip(rolls, allocations):
            roll.alokasi = allocation
            roll.status = 'dikirim'
            roll.tgl_kirim = self.today
        Roll.objects.bulk_update(rolls, ['alokasi', 'status', 'tgl_kirim'])
        AlokasiRoll.objects.bulk_create(
            [
                AlokasiRoll(alokasi=allocation, roll=roll)
                for roll, allocation in zip(rolls, allocations)
            ]
        )
        oldest, received_roll = allocations[0], rolls[0]
        self.client.force_login(self.user)
        self.assertContains(
            self.client.get(reverse('allocation_detail', args=[oldest.pk])),
            f'?allocation={oldest.pk}',
        )
        cmt_url = reverse('cmt_detail', args=[self.cmt.pk])
        target = f'{cmt_url}?allocation={oldest.pk}'
        page = self.client.get(target)
        self.assertEqual(page.context['page_obj'].number, 2)
        self.assertEqual([item.pk for item in page.context['allocations']], [oldest.pk])
        self.assertContains(page, f'id="pengiriman-{oldest.pk}"')
        redirect_url = f'{target}#pengiriman-{oldest.pk}'
        self.assertRedirects(
            self.client.post(
                target,
                {
                    'action': 'receive',
                    'allocation': oldest.pk,
                    'rolls': [received_roll.pk],
                    'tanggal': self.today.isoformat(),
                },
            ),
            redirect_url,
        )
        received_roll.refresh_from_db()
        self.assertEqual(received_roll.status, 'diterima')
        self.assertRedirects(
            self.client.post(
                target,
                {
                    'action': 'assign_po',
                    'allocation': oldest.pk,
                    'nomor_po': 'PO OLDEST',
                    'produk': 'Produk awal',
                },
            ),
            redirect_url,
        )
        oldest.refresh_from_db()
        self.assertEqual(oldest.po.nomor, 'PO OLDEST')
        refreshed = self.client.get(target)
        self.assertEqual(refreshed.context['page_obj'].number, 2)
        self.assertContains(refreshed, 'PO OLDEST')

    def test_malformed_cmt_allocation_id_has_no_mutation(self):
        allocation = ajukan_alokasi([self.rolls[0].pk], self.cmt, self.user)
        self.client.force_login(self.user)
        before = (Po.objects.count(), Log.objects.count(), allocation.status)
        url = reverse('cmt_detail', args=[self.cmt.pk])
        for invalid in ('', 'not-a-number', '1.5', '-2'):
            response = self.client.post(
                url,
                {
                    'action': 'assign_po',
                    'allocation': invalid,
                    'nomor_po': 'PO INJECTED',
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['cmt_error'])
        allocation.refresh_from_db()
        self.assertEqual((Po.objects.count(), Log.objects.count(), allocation.status), before)
        self.assertEqual(Roll.objects.get(pk=self.rolls[0].pk).status, 'menunggu')

    def test_acc_idempotent_and_invalid_batch_atomic(self):
        allocation = ajukan_alokasi([roll.pk for roll in self.rolls[:2]], self.cmt, self.user)
        putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today)
        allocation.refresh_from_db()
        approved_at, count = allocation.acc_pada, Log.objects.count()
        putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today - timedelta(days=1))
        allocation.refresh_from_db()
        self.assertEqual(allocation.acc_pada, approved_at)
        self.assertEqual(Log.objects.count(), count)
        self.assertEqual(set(allocation.roll_set.values_list('tgl_kirim', flat=True)), {self.today})
        other = ajukan_alokasi([roll.pk for roll in self.rolls[2:4]], self.cmt, self.user)
        Roll.objects.filter(pk=self.rolls[3].pk).update(status='tersedia')
        with self.assertRaises(ValidationError):
            putuskan_alokasi(other, 'acc', self.user, tgl_kirim=self.today)
        other.refresh_from_db()
        self.assertEqual((other.status, other.acc_pada), ('menunggu', None))
        self.assertEqual(Roll.objects.get(pk=self.rolls[2].pk).status, 'menunggu')

    def test_rejection_reason_history_and_no_release_after_receipt(self):
        allocation = ajukan_alokasi([self.rolls[0].pk], self.cmt, self.user)
        with self.assertRaises(ValidationError):
            putuskan_alokasi(allocation, 'tolak', self.user)
        putuskan_alokasi(allocation, 'tolak', self.user, 'CMT tidak sesuai')
        allocation.refresh_from_db()
        self.assertEqual((allocation.status, allocation.alasan), ('ditolak', 'CMT tidak sesuai'))
        self.assertEqual(Roll.objects.get(pk=self.rolls[0].pk).status, 'tersedia')
        ready, po = self.ready_po(self.rolls[:1])
        with self.assertRaises(ValidationError):
            putuskan_alokasi(ready, 'batalkan', self.user, 'Tidak jadi')
        self.assertEqual(Roll.objects.get(pk=self.rolls[0].pk).alokasi_id, ready.pk)
        self.assertEqual(po.cmt_id, self.cmt.pk)

    def test_shipment_and_receipt_date_rules(self):
        allocation = ajukan_alokasi([self.rolls[0].pk], self.cmt, self.user)
        for date in (self.today + timedelta(days=2), self.invoice.tanggal - timedelta(days=1)):
            with self.assertRaises(ValidationError):
                putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=date)
            self.assertEqual(Roll.objects.get(pk=self.rolls[0].pk).status, 'menunggu')
        putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today)
        with self.assertRaises(ValidationError):
            terima_alokasi(
                allocation, [self.rolls[0].pk], self.today - timedelta(days=1), self.user
            )
        with self.assertRaises(ValidationError):
            terima_alokasi(allocation, [self.rolls[1].pk], self.today, self.user)
        self.assertEqual(Roll.objects.get(pk=self.rolls[0].pk).status, 'dikirim')

    def test_receipt_required_same_cmt_po_reuse_preserves_product(self):
        allocation = ajukan_alokasi([self.rolls[0].pk], self.cmt, self.user)
        with self.assertRaises(ValidationError):
            tautkan_po(allocation, 'PO 109', self.user)
        putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today)
        with self.assertRaises(ValidationError):
            tautkan_po(allocation, 'PO 109', self.user)
        terima_alokasi(allocation, [self.rolls[0].pk], self.today, self.user)
        po = tautkan_po(allocation, 'PO 109', self.user, produk='Produk lama')
        _, same = self.ready_po(self.rolls[1:2])
        self.assertEqual(po.pk, same.pk)
        same.refresh_from_db()
        self.assertEqual(same.produk, 'Produk lama')
        other_cmt = Master.objects.create(kind='cmt', name='Konveksi B')
        other = ajukan_alokasi([self.rolls[2].pk], other_cmt, self.user)
        putuskan_alokasi(other, 'acc', self.user, tgl_kirim=self.today)
        terima_alokasi(other, [self.rolls[2].pk], self.today, self.user)
        with self.assertRaises(ValidationError):
            tautkan_po(other, 'PO 109', self.user, produk='Tidak boleh menimpa')
        other.refresh_from_db()
        self.assertIsNone(other.po_id)
        self.assertEqual(Po.objects.count(), 1)

    def test_balance_usage_done_without_used_rolls(self):
        _, po = self.ready_po()
        self.assertEqual(yard_po(po, self.color), Decimal('1773.00'))
        simpan_hasil(po, self.color, 1072, self.user)
        self.assertEqual(pemakaian(po, self.color), Decimal('1.65'))
        for pcs in (430, 247, 250, 145):
            self.shipment(po, pcs)
        self.assertEqual(done(po, self.color), 0)
        self.assertTrue(po_balance(po))
        self.assertTrue(po_status(po)['is_done'])
        self.assertEqual(
            set(Roll.objects.filter(alokasi__po=po).values_list('status', flat=True)), {'diterima'}
        )

    def test_200_150_50_done_then_230_reopens(self):
        _, po = self.ready_po(self.rolls[:1])
        simpan_hasil(po, self.color, 200, self.user)
        self.shipment(po, 150)
        state = po_status(po)
        self.assertEqual(
            (state['label'], state['remaining'], state['terkirim']), ('Kurang kirim', 50, 150)
        )
        self.shipment(po, 50)
        self.assertEqual(po_status(po)['label'], 'Done')
        self.assertTrue(po_balance(po))
        simpan_hasil(po, self.color, 230, self.user)
        self.assertEqual((po_status(po)['label'], po_status(po)['remaining']), ('Kurang kirim', 30))
        self.assertEqual(Hasil.objects.get(po=po, color=self.color).pcs, 230)

    def test_missing_zero_and_per_color_anomalies_not_done(self):
        blue = Master.objects.create(kind='color', name='Blue')
        Roll.objects.filter(pk=self.rolls[1].pk).update(color=blue)
        _, po = self.ready_po(self.rolls[:2])
        self.assertEqual(po_status(po)['label'], 'Belum ada hasil')
        self.assertTrue(all(row['hasil'] is None for row in po_status(po)['rows']))
        simpan_hasil(po, self.color, 0, self.user)
        self.assertEqual(po_status(po)['label'], 'Belum lengkap')
        simpan_hasil(po, blue, 0, self.user)
        self.assertEqual(po_status(po)['label'], 'Belum ada hasil produksi')
        self.assertFalse(po_balance(po))
        simpan_hasil(po, self.color, 100, self.user)
        simpan_hasil(po, blue, 100, self.user)
        # Direct insertion represents retained legacy anomalies, bypassing new-write validation.
        material = self.rolls[0].material
        KirimGudang.objects.create(
            po=po, material=material, color=self.color, tanggal=self.today, pcs=90
        )
        KirimGudang.objects.create(
            po=po, material=material, color=blue, tanggal=self.today, pcs=110
        )
        state = po_status(po)
        self.assertEqual((state['hasil'], state['terkirim']), (200, 200))
        self.assertEqual(
            (state['label'], state['remaining'], state['over']), ('Lebih kirim', 10, 10)
        )
        self.assertFalse(state['is_done'])

    def test_zero_color_can_balance_alongside_positive_color(self):
        blue = Master.objects.create(kind='color', name='Blue')
        Roll.objects.filter(pk=self.rolls[1].pk).update(color=blue)
        _, po = self.ready_po(self.rolls[:2])
        simpan_hasil(po, self.color, 0, self.user)
        simpan_hasil(po, blue, 100, self.user)
        self.shipment(po, 100, color=blue)
        self.assertEqual(po_status(po)['label'], 'Done')

    def test_excess_and_result_reduction_rejected(self):
        _, po = self.ready_po(self.rolls[:1])
        with self.assertRaises(ValidationError):
            self.shipment(po, 1)
        simpan_hasil(po, self.color, 200, self.user)
        self.shipment(po, 150)
        with self.assertRaises(ValidationError):
            self.shipment(po, 51)
        with self.assertRaises(ValidationError):
            simpan_hasil(po, self.color, 149, self.user)
        self.assertEqual(Hasil.objects.get(po=po, color=self.color).pcs, 200)
        self.assertEqual(KirimGudang.objects.filter(po=po).count(), 1)
        for pcs in (0, -1):
            with self.assertRaises(ValidationError):
                self.shipment(po, pcs)

    def test_request_identity_retry_and_identical_valid_transactions(self):
        _, po = self.ready_po(self.rolls[:1])
        simpan_hasil(po, self.color, 200, self.user)
        identity = str(uuid4())
        self.shipment(po, 50, request_id=identity)
        count = Log.objects.count()
        self.shipment(po, 50, request_id=identity)
        self.assertEqual(
            (KirimGudang.objects.filter(po=po).count(), Log.objects.count()), (1, count)
        )
        self.shipment(po, 50, request_id=str(uuid4()))
        self.assertEqual(
            (KirimGudang.objects.filter(po=po).count(), po_status(po)['terkirim']), (2, 100)
        )

    def test_result_color_must_belong_to_po(self):
        _, po = self.ready_po(self.rolls[:1])
        alien = Master.objects.create(kind='color', name='Alien')
        with self.assertRaises(ValidationError):
            simpan_hasil(po, alien, 10, self.user)
        with self.assertRaises(ValidationError):
            self.shipment(po, 1, color=alien)

    def test_legacy_first_shipment_and_historical_production_states_retained(self):
        po = Po.objects.create(nomor='PO LEGACY', cmt=self.cmt)
        allocation = Alokasi.objects.create(
            po=po, cmt=self.cmt, dibuat_oleh=self.user, status='disetujui'
        )
        roll = self.rolls[0]
        Roll.objects.filter(pk=roll.pk).update(alokasi=allocation, status='siap_kirim')
        pindah_status([roll.pk], 'dikirim', self.user, tgl_kirim=self.today)
        pindah_status([roll.pk], 'diterima', self.user, tgl_terima=self.today)
        with self.assertRaises(ValidationError):
            pindah_status([roll.pk], 'rusak', self.user)
        # Legacy states remain data; removed manual production controls are not revived.
        self.client.force_login(self.user)
        for state in ('terpakai', 'rusak'):
            Roll.objects.filter(pk=roll.pk).update(status=state, catatan='Catatan lama')
            self.assertEqual(Roll.objects.get(pk=roll.pk).catatan, 'Catatan lama')
            self.assertEqual(self.client.get(reverse('po_detail', args=[po.pk])).status_code, 200)
            self.assertFalse(po_status(po)['is_done'])

    def test_allocated_invoice_edits_and_metadata_retained(self):
        allocation, po = self.ready_po(self.rolls[:1])
        self.invoice.catatan = 'Catatan historis invoice'
        self.invoice.save(update_fields=['catatan'])
        po.produk, po.pemakaian_std, po.catatan = 'Produk historis', Decimal('1.70'), 'Audit lama'
        po.save(update_fields=['produk', 'pemakaian_std', 'catatan'])
        self.client.force_login(self.user)
        for name, pk in (
            ('allocation_detail', allocation.pk),
            ('cmt_detail', self.cmt.pk),
            ('po_detail', po.pk),
            ('invoice_edit', self.invoice.pk),
        ):
            self.assertEqual(self.client.get(reverse(name, args=[pk])).status_code, 200)
        edit_url = reverse('invoice_edit', args=[self.invoice.pk])
        data = {
            'vendor': 'Vendor A',
            'nomor': 'INV-1',
            'surat_jalan': 'SJ-2',
            'tanggal': self.invoice.tanggal.isoformat(),
            'total_rp': '2000',
            'existing': '1',
            'material[]': ['Cotton'],
            'color[]': ['Blue'],
            'lokasi[]': [''],
            'yards[]': ['55'],
            f'existing-{self.rolls[0].pk}': '999',
        }
        self.assertEqual(self.client.post(edit_url, data).status_code, 200)
        self.assertEqual(Roll.objects.get(pk=self.rolls[0].pk).yard, Decimal('118.20'))
        data[f'existing-{self.rolls[0].pk}'] = '118.20'
        self.assertEqual(self.client.post(edit_url, data).status_code, 302)
        self.invoice.refresh_from_db()
        self.assertEqual(
            (self.invoice.catatan, self.invoice.surat_jalan), ('Catatan historis invoice', 'SJ-2')
        )
        self.assertEqual(self.invoice.roll_set.count(), 16)
        self.client.post(
            reverse('po_detail', args=[po.pk]),
            {'action': 'info', 'tgl_order': self.today.isoformat()},
        )
        po.refresh_from_db()
        self.assertEqual(
            (po.produk, po.pemakaian_std, po.catatan, po.tgl_order),
            ('Produk historis', Decimal('1.70'), 'Audit lama', self.today),
        )

    def test_invoice_edit_append_request_replay_does_not_duplicate_rolls(self):
        self.client.force_login(self.user)
        payload = {
            'vendor': 'Vendor A',
            'nomor': 'INV-1',
            'tanggal': self.invoice.tanggal.isoformat(),
            'total_rp': '1000',
            'surat_jalan': '',
            'existing': '1',
            'material[]': ['Cotton'],
            'color[]': ['Cream'],
            'lokasi[]': [''],
            'yards[]': ['55'],
            'request_id': str(uuid4()),
        }
        url = reverse('invoice_edit', args=[self.invoice.pk])
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.assertEqual(self.invoice.roll_set.count(), 16)
        count = Log.objects.count()
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.assertEqual(self.invoice.roll_set.count(), 16)
        self.assertEqual(Log.objects.count(), count)
        payload['request_id'] = str(uuid4())
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.assertEqual(self.invoice.roll_set.count(), 17)

    def test_query_budget(self):
        _, po = self.ready_po()
        self.client.force_login(self.user)
        for url in (
            reverse('dashboard'),
            reverse('vendor_list'),
            reverse('vendor_detail', args=[self.invoice.vendor_id]),
            reverse('invoice_detail', args=[self.invoice.pk]),
            reverse('po_detail', args=[po.pk]),
        ):
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertLessEqual(len(queries), 20, url)

    def test_roles_read_and_operational_endpoints_enforced(self):
        allocation, po = self.ready_po(self.rolls[:1])
        operations = [
            (reverse('master'), {'kind': 'vendor', 'name': 'Other'}),
            (reverse('invoice_new'), self.invoice_payload('UNAUTHORIZED')),
            (reverse('invoice_edit', args=[self.invoice.pk]), {'existing': '1'}),
            (reverse('invoice_cancel', args=[self.invoice.pk]), {}),
            (reverse('allocation_create'), {'roll': [self.rolls[1].pk], 'cmt': self.cmt.pk}),
            (reverse('allocation_detail', args=[allocation.pk]), {'action': 'acc'}),
            (
                reverse('cmt_detail', args=[self.cmt.pk]),
                {'action': 'assign_po', 'allocation': allocation.pk, 'nomor_po': 'PO OTHER'},
            ),
            (
                reverse('cmt_detail', args=[self.cmt.pk]),
                {'action': 'receive', 'allocation': allocation.pk, 'roll': [self.rolls[0].pk]},
            ),
            (
                reverse('po_detail', args=[po.pk]),
                {'action': 'hasil', 'color': self.color.pk, 'pcs': 999},
            ),
            (reverse('roll_status'), {'action': 'terpakai', 'roll': [self.rolls[0].pk]}),
        ]
        for user in (self.director, self.admin):
            self.client.force_login(user)
            for url, payload in operations:
                self.assertEqual(self.client.post(url, payload).status_code, 403, (user.role, url))
        self.client.force_login(self.director)
        for name, args in (
            ('dashboard', []),
            ('po_detail', [po.pk]),
            ('invoice_export', [self.invoice.pk]),
            ('po_export', [po.pk]),
        ):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
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

    def test_csrf_and_post_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(
            client.post(
                reverse('allocation_create'), {'roll': [self.rolls[0].pk], 'cmt': self.cmt.pk}
            ).status_code,
            403,
        )
        self.assertFalse(Alokasi.objects.exists())
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('allocation_create')).status_code, 405)
        self.assertEqual(
            self.client.get(reverse('invoice_cancel', args=[self.invoice.pk])).status_code, 405
        )

    def test_legacy_paste_format_creates_no_po(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('invoice_new'),
            self.invoice_payload(
                'INV-NEW',
                **{
                    'color[]': ['Blue'],
                    'yards[]': ['117.00 93.00 46.00 83.00 60.00'],
                    'po[]': ['PO SHOULD NOT EXIST'],
                },
            ),
        )
        self.assertEqual(response.status_code, 302)
        invoice = Invoice.objects.get(nomor='INV-NEW')
        self.assertEqual(
            (invoice.roll_set.count(), sum(invoice.roll_set.values_list('yard', flat=True))),
            (5, Decimal('399.00')),
        )
        self.assertEqual(Po.objects.count(), 0)

    def test_forms_and_removed_controls_match_prd(self):
        self.client.force_login(self.user)
        invoice_page = self.client.get(reverse('invoice_new'))
        self.assertNotIn('catatan', invoice_page.context['form'].fields)
        for text in ('Tambah baris', 'Tambah nama bahan', 'Nama bahan'):
            self.assertContains(invoice_page, text)
        for text in ('Tambah PO', 'name="po[]"', 'name="catatan"'):
            self.assertNotContains(invoice_page, text)
        _, po = self.ready_po(self.rolls[:1])
        po_page = self.client.get(reverse('po_detail', args=[po.pk]))
        self.assertEqual(list(po_page.context['form'].fields), ['tgl_order'])
        for text in ('Roll PO', 'Tandai PO selesai', 'Tandai terpakai', 'Tandai rusak'):
            self.assertNotContains(po_page, text)
        self.assertContains(self.client.get(reverse('po_list')), self.cmt.name)

    def test_import_preview_valid_corrupt_and_wrong_role(self):
        self.client.force_login(self.user)
        url = reverse('invoice_yard_preview')
        response = self.client.post(url, {'file': SimpleUploadedFile('yard.csv', b'Yard\n117\n93')})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['yards'], ['117.00', '93.00'])
        self.assertEqual(response.json()['roll_count'], 2)
        self.assertEqual(Decimal(response.json()['total_yard']), Decimal('210.00'))
        self.assertEqual(
            self.client.post(
                url,
                {
                    'file': SimpleUploadedFile('broken.xlsx', b'PK\x03\x04broken workbook'),
                },
            ).status_code,
            400,
        )
        self.assertEqual(Invoice.objects.count(), 1)
        payload = self.invoice_payload(
            'INV-CORRUPT-YARD',
            **{
                'file-0': SimpleUploadedFile('broken.xlsx', b'broken workbook'),
            },
        )
        response = self.client.post(reverse('invoice_new'), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'File yard tidak dapat dibaca')
        self.assertFalse(Invoice.objects.filter(nomor='INV-CORRUPT-YARD').exists())
        self.client.force_login(self.director)
        self.assertEqual(
            self.client.post(
                url,
                {
                    'file': SimpleUploadedFile('yard.csv', b'Yard\n117'),
                },
            ).status_code,
            403,
        )

    def test_legacy_individual_roll_overrides_regroup_without_yard_loss(self):
        self.client.force_login(self.user)
        payload = self.invoice_payload(
            'INV-OVERRIDES',
            **{
                'po[]': ['PO IGNORED', ''],
                'material[]': ['Cotton', 'Polyester'],
                'color[]': ['Cream', 'Blue'],
                'lokasi[]': ['', ''],
                'yards[]': ['117 93', '46 83 60'],
                'roll_details[]': [
                    '[{}, {"material": "Linen", "color": "Navy", "lokasi": "Gudang A"}]',
                    '[{}, {}, {}]',
                ],
            },
        )
        self.assertEqual(self.client.post(reverse('invoice_new'), payload).status_code, 302)
        invoice = Invoice.objects.get(nomor='INV-OVERRIDES')
        self.assertEqual(invoice.material_groups.count(), 3)
        rolls = list(invoice.roll_set.select_related('material', 'color', 'lokasi').order_by('id'))
        self.assertEqual(
            [roll.yard for roll in rolls],
            [Decimal('117'), Decimal('93'), Decimal('46'), Decimal('83'), Decimal('60')],
        )
        self.assertEqual(
            (rolls[1].material.name, rolls[1].color.name, rolls[1].lokasi.name),
            ('Linen', 'Navy', 'Gudang A'),
        )
        self.assertEqual(Po.objects.count(), 0)
        response = self.client.post(
            reverse('invoice_edit', args=[invoice.pk]),
            {
                'vendor': 'Vendor Baru',
                'nomor': 'INV-OVERRIDES',
                'tanggal': self.today.isoformat(),
                'total_rp': '0',
                'existing': '1',
                f'existing-{rolls[1].pk}': '94',
            },
        )
        self.assertEqual(response.status_code, 302)
        rolls[1].refresh_from_db()
        self.assertEqual(
            (rolls[1].yard, rolls[1].material.name, rolls[1].color.name),
            (Decimal('94'), 'Linen', 'Navy'),
        )
        payload['nomor'] = 'INV-BAD-DETAIL'
        payload['roll_details[]'] = ['[{}, {}, {}]', '[{}, {}, {}]']
        self.assertEqual(self.client.post(reverse('invoice_new'), payload).status_code, 200)
        self.assertFalse(Invoice.objects.filter(nomor='INV-BAD-DETAIL').exists())

    def test_nested_invoice_two_materials_three_colors_seven_rolls(self):
        self.client.force_login(self.user)
        payload = {
            'vendor': 'Vendor Baru',
            'nomor': 'INV-NESTED',
            'tanggal': self.today.isoformat(),
            'total_rp': '0',
            'group_count': '2',
            'groups-0-material': 'Cotton',
            'groups-0-row_count': '2',
            'groups-0-rows-0-color': 'Hitam',
            'groups-0-rows-0-lokasi': '',
            'groups-0-rows-0-yards': '80;82,5;79',
            'groups-0-rows-1-color': 'Cream',
            'groups-0-rows-1-lokasi': '',
            'groups-0-rows-1-yards': '75;77',
            'groups-1-material': 'Polyester',
            'groups-1-row_count': '1',
            'groups-1-rows-0-color': 'Navy',
            'groups-1-rows-0-lokasi': 'Gudang A',
            'groups-1-rows-0-yards': '90;91',
        }
        self.assertEqual(self.client.post(reverse('invoice_new'), payload).status_code, 302)
        invoice = Invoice.objects.get(nomor='INV-NESTED')
        self.assertEqual((invoice.material_groups.count(), invoice.roll_set.count()), (2, 7))
        self.assertEqual(sum(invoice.roll_set.values_list('yard', flat=True)), Decimal('574.50'))
        self.assertEqual(set(invoice.roll_set.values_list('status', flat=True)), {'tersedia'})
        self.assertEqual(Po.objects.count(), 0)
        actual = list(
            invoice.roll_set.order_by('id').values_list('material__name', 'color__name', 'yard')
        )
        self.assertEqual(
            actual,
            [
                ('Cotton', 'Hitam', Decimal('80.00')),
                ('Cotton', 'Hitam', Decimal('82.50')),
                ('Cotton', 'Hitam', Decimal('79.00')),
                ('Cotton', 'Cream', Decimal('75.00')),
                ('Cotton', 'Cream', Decimal('77.00')),
                ('Polyester', 'Navy', Decimal('90.00')),
                ('Polyester', 'Navy', Decimal('91.00')),
            ],
        )
        allocation = ajukan_alokasi(
            list(invoice.roll_set.values_list('pk', flat=True)), self.cmt, self.user
        )
        self.assertEqual(allocation.roll_set.count(), 7)
        self.assertIsNone(allocation.po_id)
        payload['nomor'], payload['groups-1-rows-0-yards'] = 'INV-INVALID-NESTED', '91;0'
        response = self.client.post(reverse('invoice_new'), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'INV-INVALID-NESTED')
        self.assertContains(response, '82,5')
        self.assertFalse(Invoice.objects.filter(nomor='INV-INVALID-NESTED').exists())

    def test_material_group_limit_and_cross_invoice_allocation(self):
        self.client.force_login(self.user)
        data = self.invoice_payload(
            'INV-TOO-MANY',
            **{
                'material[]': [f'Bahan {n}' for n in range(11)],
                'color[]': ['Cream'] * 11,
                'yards[]': ['1'] * 11,
                'lokasi[]': [''] * 11,
            },
        )
        response = self.client.post(reverse('invoice_new'), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '10')
        self.assertFalse(Invoice.objects.filter(nomor='INV-TOO-MANY').exists())
        other = self.make_invoice('INV-OTHER')
        with self.assertRaises(ValidationError):
            ajukan_alokasi([self.rolls[0].pk, other.roll_set.get().pk], self.cmt, self.user)
        self.assertFalse(Alokasi.objects.exists())

    def test_removed_invoice_po_field_preserves_legacy_link(self):
        legacy_po = Po.objects.create(nomor='PO LEGACY')
        group = InvoicePo.objects.create(invoice=self.invoice, urut=1, po=legacy_po)
        Roll.objects.filter(pk=self.rolls[0].pk).update(invoice_po=group)
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('invoice_edit', args=[self.invoice.pk]),
            {
                'vendor': 'Vendor A',
                'nomor': 'INV-1',
                'surat_jalan': '',
                'tanggal': self.invoice.tanggal.isoformat(),
                'total_rp': '1000',
                'existing': '1',
                f'existing-po-{group.pk}': 'PO INJECTED',
            },
        )
        self.assertEqual(response.status_code, 302)
        group.refresh_from_db()
        self.assertEqual(group.po_id, legacy_po.pk)
        self.assertFalse(Po.objects.filter(nomor='PO INJECTED').exists())

    def test_vendor_search_scoped_case_insensitive_before_pagination(self):
        for index in range(35):
            self.make_invoice(f'LATEST-{index:03}')
        target = self.make_invoice('special-find-002')
        target.tanggal = self.today - timedelta(days=20)
        target.save(update_fields=['tanggal'])
        self.make_invoice('SPECIAL-FIND-OTHER', vendor='Vendor B')
        self.client.force_login(self.director)
        url = reverse('vendor_detail', args=[self.invoice.vendor_id])
        self.assertContains(self.client.get(url, {'q': 'FIND'}), 'special-find-002')
        self.assertNotContains(self.client.get(url, {'q': 'find'}), 'SPECIAL-FIND-OTHER')
        response = self.client.get(url, {'q': 'no-such-invoice'})
        self.assertContains(response, 'Tidak ada invoice yang cocok dengan pencarian ini.')
        self.assertContains(response, 'Hapus pencarian')
        page = self.client.get(url, {'q': 'LATEST', 'page': 2})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'q=LATEST')

    def test_status_exports_http_shipment_retry_consistency(self):
        _, po = self.ready_po(self.rolls[:1])
        self.client.force_login(self.user)
        detail = reverse('po_detail', args=[po.pk])
        self.client.post(detail, {'action': 'hasil', 'color': self.color.pk, 'pcs': '200'})
        payload = {
            'action': 'kirim',
            'color': self.color.pk,
            'tanggal': self.today.isoformat(),
            'pcs': '150',
            'request_id': str(uuid4()),
        }
        self.client.post(detail, payload)
        self.client.post(detail, payload)
        self.assertEqual(KirimGudang.objects.filter(po=po).count(), 1)
        self.assertEqual(po_status(po)['remaining'], 50)
        for url in (
            detail,
            reverse('po_cmt_list', args=[self.cmt.pk]),
            reverse('cmt_detail', args=[self.cmt.pk]),
        ):
            self.assertContains(self.client.get(url), 'Kurang kirim')
        dashboard = self.client.get(reverse('dashboard'))
        self.assertEqual(dashboard.context['unbalanced_count'], 1)
        self.assertEqual(dashboard.context['remaining_pcs'], 50)
        self.assertContains(dashboard, po.nomor)
        export = self.client.get(reverse('po_export', args=[po.pk]))
        self.assertEqual(export.status_code, 200)
        book = load_workbook(BytesIO(export.content), read_only=True)
        values = [cell for sheet in book for row in sheet.values for cell in row]
        for expected in ('Kurang kirim', self.cmt.name, 50):
            self.assertIn(expected, values)
        book.close()
        book = load_workbook(
            BytesIO(self.client.get(reverse('invoice_export', args=[self.invoice.pk])).content),
            read_only=True,
        )
        self.assertEqual(book.active.max_row, 16)
        self.assertIn('Bahan', list(next(book.active.values)))
        book.close()
        payload.update(pcs='50', request_id=str(uuid4()))
        self.client.post(detail, payload)
        self.assertEqual(po_status(po)['label'], 'Done')
        self.assertContains(self.client.get(detail), 'Done')

    def test_export_distinguishes_missing_zero_and_per_color_excess(self):
        blue = Master.objects.create(kind='color', name='Blue')
        Roll.objects.filter(pk=self.rolls[1].pk).update(color=blue)
        _, po = self.ready_po(self.rolls[:2])
        simpan_hasil(po, blue, 0, self.user)
        self.client.force_login(self.director)
        url = reverse('po_export', args=[po.pk])

        def records():
            book = load_workbook(BytesIO(self.client.get(url).content), read_only=True)
            rows = list(book.active.values)
            data = [dict(zip(rows[0], row)) for row in rows[1:]]
            book.close()
            return {row['Warna']: row for row in data}

        data = records()
        self.assertIsNone(data['Cream']['Hasil produksi (pcs)'])
        self.assertIsNone(data['Cream']['Sisa kirim (pcs)'])
        self.assertEqual(data['Blue']['Hasil produksi (pcs)'], 0)
        self.assertEqual({row['Status PO'] for row in data.values()}, {'Belum lengkap'})
        simpan_hasil(po, self.color, 100, self.user)
        simpan_hasil(po, blue, 100, self.user)
        material = self.rolls[0].material
        KirimGudang.objects.create(
            po=po, material=material, color=self.color, tanggal=self.today, pcs=90
        )
        KirimGudang.objects.create(
            po=po, material=material, color=blue, tanggal=self.today, pcs=110
        )
        data = records()
        self.assertEqual(data['Cream']['Sisa kirim (pcs)'], 10)
        self.assertEqual(data['Blue']['Lebih kirim (pcs)'], 10)
        self.assertEqual({row['Status PO'] for row in data.values()}, {'Lebih kirim'})

    def test_attachment_upload_download_and_replacement(self):
        self.client.force_login(self.user)
        payload = self.invoice_payload(
            'INV-UPLOAD',
            invoice_file=SimpleUploadedFile(
                'invoice.pdf', b'%PDF-1.7\nexample', content_type='application/pdf'
            ),
        )
        self.assertEqual(self.client.post(reverse('invoice_new'), payload).status_code, 302)
        invoice = Invoice.objects.get(nomor='INV-UPLOAD')
        attachment = InvoiceAttachment.objects.get(invoice=invoice)
        self.assertEqual(
            (attachment.filename, attachment.size), ('invoice.pdf', len(b'%PDF-1.7\nexample'))
        )
        download = self.client.get(reverse('invoice_attachment', args=[invoice.pk]))
        self.assertEqual((download.status_code, download.content), (200, b'%PDF-1.7\nexample'))
        self.assertIn('attachment;', download['Content-Disposition'])
        for user, status in ((self.director, 200), (self.admin, 403)):
            self.client.force_login(user)
            self.assertEqual(
                self.client.get(reverse('invoice_attachment', args=[invoice.pk])).status_code,
                status,
            )
        self.client.force_login(self.user)
        edit = {
            'vendor': 'Vendor Baru',
            'nomor': 'INV-UPLOAD',
            'surat_jalan': '',
            'tanggal': self.today.isoformat(),
            'total_rp': '0',
            'existing': '1',
        }
        url = reverse('invoice_edit', args=[invoice.pk])
        self.assertEqual(self.client.post(url, edit).status_code, 302)
        attachment.refresh_from_db()
        self.assertEqual(attachment.filename, 'invoice.pdf')
        edit['invoice_file'] = SimpleUploadedFile(
            'scan.png', b'\x89PNG\r\n\x1a\nreplacement', content_type='image/png'
        )
        self.assertEqual(self.client.post(url, edit).status_code, 302)
        attachment.refresh_from_db()
        self.assertEqual((attachment.filename, attachment.content_type), ('scan.png', 'image/png'))

    def test_invalid_attachment_atomic(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('invoice_new'),
            self.invoice_payload(
                'INV-BAD-FILE',
                invoice_file=SimpleUploadedFile('invoice.pdf', b'not a pdf'),
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'PDF, JPG, atau PNG yang valid')
        self.assertFalse(Invoice.objects.filter(nomor='INV-BAD-FILE').exists())


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Penguncian dan transaksi bersamaan harus diuji pada PostgreSQL.')
        self.user = User.objects.create_user(username='buyer', role='purchasing')
        self.cmt = Master.objects.create(kind='cmt', name='Ling Ling')
        self.today = timezone.localdate()
        invoice = simpan_invoice(
            {
                'vendor': 'Vendor A',
                'nomor': 'INV-1',
                'surat_jalan': '',
                'tanggal': self.today,
                'total_rp': Decimal(0),
            },
            [{'material': 'Cotton', 'color': 'Cream', 'lokasi': '', 'yards': [Decimal(100)]}],
            self.user,
        )
        self.roll = invoice.roll_set.select_related('color').get()

    def concurrent(self, callback):
        barrier = Barrier(2)

        def submit(number):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                callback(number)
                return 'ok'
            except ValidationError:
                return 'conflict'
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(submit, [1, 2]))

    def test_same_roll_cannot_be_allocated_twice(self):
        results = self.concurrent(lambda _: ajukan_alokasi([self.roll.pk], self.cmt, self.user))
        self.assertCountEqual(results, ['ok', 'conflict'])
        self.assertEqual((Alokasi.objects.count(), Po.objects.count()), (1, 0))

    def test_combined_shipments_cannot_exceed_result(self):
        allocation = ajukan_alokasi([self.roll.pk], self.cmt, self.user)
        putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today)
        terima_alokasi(allocation, [self.roll.pk], self.today, self.user)
        po = tautkan_po(allocation, 'PO 109', self.user)
        simpan_hasil(po, self.roll.color, 200, self.user)

        def ship(number):
            kirim_gudang(
                po,
                self.roll.color,
                {'tanggal': self.today, 'pcs': 150, 'request_id': f'concurrent-{number}'},
                self.user,
            )

        self.assertCountEqual(self.concurrent(ship), ['ok', 'conflict'])
        self.assertEqual(KirimGudang.objects.get().pcs, 150)
        self.assertEqual(po_status(po)['remaining'], 50)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class MaterialProductionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='material-buyer', role='purchasing')
        self.director = User.objects.create_user(username='material-director', role='direktur')
        self.cmt = Master.objects.create(kind='cmt', name='CMT Bahan')
        self.today = timezone.localdate()
        self.invoice = simpan_invoice(
            {
                'vendor': 'Vendor Bahan',
                'nomor': 'INV-MATERIAL',
                'tanggal': self.today,
                'total_rp': Decimal(1000),
                'surat_jalan': 'SJ-MATERIAL',
            },
            [
                {'material': 'Cotton', 'rows': [{'color': 'Cream', 'yards': [Decimal(80)]}]},
                {'material': 'Polyester', 'rows': [{'color': 'Cream', 'yards': [Decimal(90)]}]},
            ],
            self.user,
        )
        rolls = list(self.invoice.roll_set.select_related('material', 'color').order_by('pk'))
        self.cotton, self.polyester, self.color = (
            rolls[0].material,
            rolls[1].material,
            rolls[0].color,
        )
        ids = [roll.pk for roll in rolls]
        allocation = ajukan_alokasi(ids, self.cmt, self.user)
        putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today)
        terima_alokasi(allocation, ids, self.today, self.user)
        self.po = tautkan_po(allocation, 'PO MATERIAL', self.user)
        self.client.force_login(self.user)

    def result(self, material, pcs):
        return simpan_hasil(self.po, self.color, pcs, self.user, material=material)

    def ship(self, material, pcs, identity=None):
        return kirim_gudang(
            self.po,
            self.color,
            {'tanggal': self.today, 'pcs': pcs, 'request_id': identity or str(uuid4())},
            self.user,
            material=material,
        )

    def rows(self):
        return {(row['material_id'], row['color_id']): row for row in po_status(self.po)['rows']}

    def export_rows(self):
        response = self.client.get(reverse('po_export', args=[self.po.pk]))
        self.assertEqual(response.status_code, 200)
        book = load_workbook(BytesIO(response.content), read_only=True)
        values = list(book.active.values)
        rows = [dict(zip(values[0], row)) for row in values[1:]]
        book.close()
        return rows

    def test_same_color_material_results_and_limits_are_independent(self):
        self.result(self.cotton, 200)
        self.result(self.polyester, 70)
        self.ship(self.cotton, 150)
        self.ship(self.polyester, 40)
        rows = self.rows()
        self.assertEqual(
            (
                rows[(self.cotton.pk, self.color.pk)]['hasil'],
                rows[(self.cotton.pk, self.color.pk)]['shipped'],
                rows[(self.cotton.pk, self.color.pk)]['remaining'],
            ),
            (200, 150, 50),
        )
        self.assertEqual(
            (
                rows[(self.polyester.pk, self.color.pk)]['hasil'],
                rows[(self.polyester.pk, self.color.pk)]['shipped'],
                rows[(self.polyester.pk, self.color.pk)]['remaining'],
            ),
            (70, 40, 30),
        )
        self.assertEqual(
            (
                po_status(self.po)['hasil'],
                po_status(self.po)['terkirim'],
                po_status(self.po)['remaining'],
            ),
            (270, 190, 80),
        )
        self.assertEqual(yard_po(self.po, self.color, material=self.cotton), Decimal(80))
        self.assertEqual(yard_po(self.po, self.color, material=self.polyester), Decimal(90))
        self.assertEqual(done(self.po, self.color, material=self.cotton), 50)
        self.assertEqual(done(self.po, self.color, material=self.polyester), 30)
        self.assertEqual(pemakaian(self.po, self.color, material=self.cotton), Decimal('0.40'))
        self.assertEqual(pemakaian(self.po, self.color, material=self.polyester), Decimal('1.29'))
        with self.assertRaises(ValidationError):
            self.ship(self.polyester, 31)
        with self.assertRaises(ValidationError):
            self.result(self.polyester, 39)
        self.result(self.cotton, 230)
        self.assertEqual(Hasil.objects.get(po=self.po, material=self.polyester).pcs, 70)
        self.assertEqual(po_status(self.po)['remaining'], 110)
        self.assertEqual(KirimGudang.objects.filter(po=self.po).count(), 2)

    def test_ambiguous_and_unrelated_material_selection_rejected(self):
        with self.assertRaises(ValidationError):
            simpan_hasil(self.po, self.color, 10, self.user)
        with self.assertRaises(ValidationError):
            kirim_gudang(self.po, self.color, {'tanggal': self.today, 'pcs': 1}, self.user)
        alien = Master.objects.create(kind='material', name='Bahan lain')
        with self.assertRaises(ValidationError):
            self.result(alien, 10)
        with self.assertRaises(ValidationError):
            self.result(self.color, 10)
        response = self.client.post(
            reverse('po_detail', args=[self.po.pk]),
            {'action': 'hasil', 'color': self.color.pk, 'pcs': '10'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['detail_error'])
        self.assertFalse(Hasil.objects.filter(po=self.po).exists())
        self.assertFalse(KirimGudang.objects.filter(po=self.po).exists())

    def test_all_pairs_needed_for_done_and_result_increase_reopens(self):
        self.result(self.cotton, 200)
        self.result(self.polyester, 70)
        self.ship(self.cotton, 200)
        self.assertEqual(self.rows()[(self.cotton.pk, self.color.pk)]['status_label'], 'Done')
        self.assertFalse(po_status(self.po)['is_done'])
        self.assertEqual(po_status(self.po)['remaining'], 70)
        self.ship(self.polyester, 70)
        self.assertTrue(po_status(self.po)['is_done'])
        self.result(self.cotton, 230)
        self.assertEqual(po_status(self.po)['remaining'], 30)
        self.assertFalse(po_status(self.po)['is_done'])

    def test_same_color_excess_cannot_cancel_other_material_shortage(self):
        self.result(self.cotton, 100)
        self.result(self.polyester, 100)
        KirimGudang.objects.create(
            po=self.po, material=self.cotton, color=self.color, tanggal=self.today, pcs=90
        )
        KirimGudang.objects.create(
            po=self.po, material=self.polyester, color=self.color, tanggal=self.today, pcs=110
        )
        summary = po_status(self.po)
        self.assertEqual((summary['hasil'], summary['terkirim']), (200, 200))
        self.assertEqual((summary['remaining'], summary['over']), (10, 10))
        self.assertEqual(summary['label'], 'Lebih kirim')
        self.assertFalse(summary['is_done'])

    def test_shipment_replay_identity_is_bound_to_material(self):
        self.result(self.cotton, 100)
        self.result(self.polyester, 100)
        identity = str(uuid4())
        original = self.ship(self.cotton, 40, identity)
        logs = Log.objects.count()
        self.assertEqual(self.ship(self.cotton, 40, identity).pk, original.pk)
        self.assertEqual(Log.objects.count(), logs)
        with self.assertRaises(ValidationError):
            self.ship(self.polyester, 40, identity)
        other = self.ship(self.polyester, 40)
        self.assertNotEqual(original.pk, other.pk)
        self.assertEqual(KirimGudang.objects.filter(po=self.po).count(), 2)
        self.assertEqual({row['shipped'] for row in self.rows().values()}, {40})

    def test_http_table_history_and_excel_split_same_color_materials(self):
        detail = reverse('po_detail', args=[self.po.pk])
        for material, hasil, sent in ((self.cotton, 200, 150), (self.polyester, 70, 40)):
            result = self.client.post(
                detail,
                {'action': 'hasil', 'material': material.pk, 'color': self.color.pk, 'pcs': hasil},
            )
            self.assertEqual(result.status_code, 302)
            self.assertEqual(
                self.client.post(
                    detail,
                    {
                        'action': 'kirim',
                        'material': material.pk,
                        'color': self.color.pk,
                        'tanggal': self.today.isoformat(),
                        'pcs': sent,
                        'request_id': str(uuid4()),
                    },
                ).status_code,
                302,
            )
        page = self.client.get(detail)
        rows = {row['material_id']: row for row in page.context['rows']}
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            (rows[self.cotton.pk]['rolls'], rows[self.cotton.pk]['yard']), (1, Decimal(80))
        )
        self.assertEqual(
            (rows[self.polyester.pk]['rolls'], rows[self.polyester.pk]['yard']), (1, Decimal(90))
        )
        self.assertEqual(rows[self.cotton.pk]['sent'][self.today], 150)
        self.assertEqual(rows[self.polyester.pk]['sent'][self.today], 40)
        for material in (self.cotton, self.polyester):
            self.assertContains(page, f'id="hasil-{material.pk}-{self.color.pk}"')
        exported = {row['Bahan']: row for row in self.export_rows()}
        for material, hasil, sent, sisa, yard in (
            (self.cotton, 200, 150, 50, 80),
            (self.polyester, 70, 40, 30, 90),
        ):
            row = exported[material.name]
            self.assertEqual(
                (
                    row['Hasil produksi (pcs)'],
                    row['Total terkirim (pcs)'],
                    row['Sisa kirim (pcs)'],
                    row['Total yard alokasi'],
                ),
                (hasil, sent, sisa, yard),
            )
            self.assertEqual(row[self.today.isoformat()], sent)
            self.assertEqual(row['Invoice sumber'], self.invoice.nomor)
        self.assertEqual(sum(row['Hasil produksi (pcs)'] for row in exported.values()), 270)

    def test_ambiguous_legacy_totals_count_once_then_map_explicitly(self):
        historical = Hasil.objects.create(po=self.po, color=self.color, pcs=200)
        shipment = KirimGudang.objects.create(
            po=self.po, color=self.color, tanggal=self.today, pcs=150
        )
        before = (historical.pk, shipment.pk, historical.pcs, shipment.pcs)
        summary = po_status(self.po)
        self.assertTrue(summary['unmapped_material'])
        self.assertEqual((summary['hasil'], summary['terkirim']), (200, 150))
        self.assertFalse(summary['is_done'])
        self.assertEqual(len([row for row in summary['rows'] if row['material_id'] is None]), 1)
        for material in (self.cotton, self.polyester):
            with self.assertRaises(ValidationError):
                self.result(material, 100)
            with self.assertRaises(ValidationError):
                self.ship(material, 1)
        page = self.client.get(reverse('po_detail', args=[self.po.pk]))
        self.assertContains(page, 'Bahan belum ditentukan')
        for material in (self.cotton, self.polyester):
            self.assertContains(page, f'id="hasil-{material.pk}-{self.color.pk}" disabled')
        legacy = [row for row in page.context['rows'] if row['material_id'] is None]
        self.assertEqual((legacy[0]['rolls'], legacy[0]['yard']), (None, None))
        exported = self.export_rows()
        self.assertEqual(sum(row['Roll'] or 0 for row in exported), 2)
        self.assertEqual(sum(row['Total yard alokasi'] or 0 for row in exported), 170)
        self.assertEqual(sum(row['Hasil produksi (pcs)'] or 0 for row in exported), 200)
        self.assertEqual(sum(row['Total terkirim (pcs)'] or 0 for row in exported), 150)
        response = self.client.post(
            reverse('po_detail', args=[self.po.pk]),
            {
                'action': 'map_material',
                'color': self.color.pk,
                'material': self.cotton.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        historical.refresh_from_db()
        shipment.refresh_from_db()
        self.assertEqual((historical.pk, shipment.pk, historical.pcs, shipment.pcs), before)
        self.assertEqual(
            (historical.material_id, shipment.material_id), (self.cotton.pk, self.cotton.pk)
        )
        self.assertFalse(po_status(self.po)['unmapped_material'])
        self.assertEqual(self.rows()[(self.cotton.pk, self.color.pk)]['remaining'], 50)
        self.result(self.polyester, 0)
        self.ship(self.cotton, 50)
        self.assertTrue(po_status(self.po)['is_done'])

    def test_mapping_rejects_existing_target_and_wrong_role_without_merging_totals(self):
        historical = Hasil.objects.create(po=self.po, color=self.color, pcs=200)
        target = Hasil.objects.create(po=self.po, material=self.cotton, color=self.color, pcs=30)
        shipment = KirimGudang.objects.create(
            po=self.po, color=self.color, tanggal=self.today, pcs=150
        )
        logs = Log.objects.count()
        with self.assertRaises(ValidationError):
            map_legacy_production(self.po, self.color, self.cotton, self.user)
        self.client.force_login(self.director)
        self.assertEqual(
            self.client.post(
                reverse('po_detail', args=[self.po.pk]),
                {
                    'action': 'map_material',
                    'color': self.color.pk,
                    'material': self.polyester.pk,
                },
            ).status_code,
            403,
        )
        historical.refresh_from_db()
        target.refresh_from_db()
        shipment.refresh_from_db()
        self.assertEqual(
            (
                historical.material_id,
                historical.pcs,
                target.pcs,
                shipment.material_id,
                shipment.pcs,
            ),
            (None, 200, 30, None, 150),
        )
        self.assertEqual(Log.objects.count(), logs)


class HistoricalMigrationTests(TransactionTestCase):
    """Conserve real legacy identities and expose unmapped/multi-CMT exceptions."""

    migrate_from = [('tracking', '0003_invoicepo_roll_invoice_po_and_more')]
    migrate_to = [('tracking', '0007_production_material')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.old = executor.loader.project_state(self.migrate_from).apps
        self.addCleanup(self.restore_latest)

    def restore_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_conservation_overrides_and_unmapped_cmt_exceptions(self):
        models = {
            name: self.old.get_model('tracking', name)
            for name in (
                'User',
                'Master',
                'Invoice',
                'InvoicePo',
                'Po',
                'Alokasi',
                'Roll',
                'InvoiceAttachment',
                'Hasil',
                'KirimGudang',
                'Log',
            )
        }
        user = models['User'].objects.create(username='historical', role='purchasing')
        master = models['Master']
        vendor = master.objects.create(kind='vendor', name='Vendor historis')
        cmts = [master.objects.create(kind='cmt', name=f'CMT {n}') for n in (1, 2)]
        materials = [master.objects.create(kind='material', name=f'Bahan {n}') for n in (1, 2)]
        colors = [master.objects.create(kind='color', name=f'Warna {n}') for n in (1, 2)]
        date = timezone.localdate() - timedelta(days=20)
        invoice = models['Invoice'].objects.create(
            vendor=vendor,
            nomor='INV-HISTORY',
            tanggal=date,
            total_rp=12345,
            dibuat_oleh=user,
            catatan='Tetap tersimpan',
            surat_jalan='SJ-INVOICE',
        )
        pos = [
            models['Po'].objects.create(
                nomor=f'PO {n}',
                produk='Produk lama',
                catatan='Catatan lama',
                pemakaian_std=Decimal('1.23'),
            )
            for n in (1, 2, 3)
        ]
        groups = [
            models['InvoicePo'].objects.create(invoice=invoice, po=po, urut=i + 1)
            for i, po in enumerate(pos)
        ]
        allocations = [
            models['Alokasi'].objects.create(
                po=pos[0], cmt=cmts[0], dibuat_oleh=user, status='disetujui'
            ),
            models['Alokasi'].objects.create(
                po=pos[1], cmt=cmts[0], dibuat_oleh=user, status='disetujui'
            ),
            models['Alokasi'].objects.create(
                po=pos[1], cmt=cmts[1], dibuat_oleh=user, status='disetujui'
            ),
        ]
        for index, status in enumerate(('siap_kirim', 'terpakai', 'rusak', 'diterima')):
            allocation = allocations[min(index, 2)] if index < 3 else None
            material = materials[index % 2]
            color = colors[index % 2]
            models['Roll'].objects.create(
                invoice=invoice,
                invoice_po=groups[min(index, 2)],
                material=material,
                color=color,
                urut=index + 1,
                yard=Decimal('80.25') + index,
                status=status,
                alokasi=allocation,
                catatan='Catatan roll lama',
                tgl_kirim=date + timedelta(days=1) if status != 'siap_kirim' else None,
                tgl_terima=date + timedelta(days=2) if status in ('terpakai', 'diterima') else None,
                sj_kirim='SJ-KAIN' if status != 'siap_kirim' else '',
            )
        models['InvoiceAttachment'].objects.create(
            invoice=invoice,
            filename='lama.pdf',
            content_type='application/pdf',
            content=b'%PDF-1.7\nhistorical',
            size=len(b'%PDF-1.7\nhistorical'),
        )
        models['Hasil'].objects.create(po=pos[0], color=colors[0], pcs=200)
        models['KirimGudang'].objects.create(po=pos[0], color=colors[0], tanggal=date, pcs=150)
        models['Hasil'].objects.create(po=pos[1], color=colors[1], pcs=0)
        models['KirimGudang'].objects.create(po=pos[1], color=colors[1], tanggal=date, pcs=10)
        models['Log'].objects.create(
            user=user, aksi='historis', objek='Invoice lama', detail='Tetap tersimpan'
        )
        conserved_names = (
            'Invoice',
            'InvoicePo',
            'Po',
            'Alokasi',
            'Roll',
            'InvoiceAttachment',
            'Hasil',
            'KirimGudang',
            'Log',
        )
        old_fields = {
            name: [field.attname for field in models[name]._meta.concrete_fields]
            for name in conserved_names
        }
        before = {
            name: list(models[name].objects.order_by('pk').values_list(*old_fields[name]))
            for name in conserved_names
        }
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        new_apps = executor.loader.project_state(self.migrate_to).apps
        after = {
            name: list(
                new_apps.get_model('tracking', name)
                .objects.order_by('pk')
                .values_list(*old_fields[name])
            )
            for name in conserved_names
        }
        self.assertEqual(before, after)
        migrated_po = new_apps.get_model('tracking', 'Po')
        self.assertEqual(migrated_po.objects.get(pk=pos[0].pk).cmt_id, cmts[0].pk)
        self.assertIsNone(migrated_po.objects.get(pk=pos[1].pk).cmt_id)
        self.assertIsNone(migrated_po.objects.get(pk=pos[2].pk).cmt_id)
        new_roll = new_apps.get_model('tracking', 'Roll')
        self.assertEqual(new_roll.objects.filter(invoice_color_id=None).count(), 0)
        for roll in new_roll.objects.select_related('invoice_color__group'):
            self.assertEqual(roll.material_id, roll.invoice_color.group.material_id)
            self.assertEqual(roll.color_id, roll.invoice_color.color_id)
        self.assertEqual(new_apps.get_model('tracking', 'InvoiceMaterial').objects.count(), 2)
        self.assertEqual(new_apps.get_model('tracking', 'AlokasiRoll').objects.count(), 3)
        migration = import_module('tracking.migrations.0005_migrate_material_history')
        with connection.schema_editor() as editor:
            migration.migrate_history(new_apps, editor)
        self.assertEqual(new_apps.get_model('tracking', 'InvoiceMaterial').objects.count(), 2)
        self.assertEqual(new_apps.get_model('tracking', 'AlokasiRoll').objects.count(), 3)
        output = StringIO()
        call_command('audit_workflow', '--json', stdout=output)
        import json

        audit = json.loads(output.getvalue())
        reasons = {item['reason'] for item in audit['unmapped_pos']}
        self.assertEqual(reasons, {'no_cmt', 'multiple_cmt'})
        self.assertEqual(new_roll.objects.get(status='siap_kirim').tgl_kirim, None)


class ProductionMaterialMigrationTests(TransactionTestCase):
    migrate_from = [('tracking', '0006_invoice_write_requests')]
    migrate_to = [('tracking', '0007_production_material')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.old = executor.loader.project_state(self.migrate_from).apps
        self.addCleanup(self.restore_latest)

    def restore_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_single_material_maps_and_ambiguous_orphan_records_stay_unassigned(self):
        def get(name):
            return self.old.get_model('tracking', name)
        user = get('User').objects.create(username='production-migration', role='purchasing')
        vendor = get('Master').objects.create(kind='vendor', name='Migration vendor')
        cmt = get('Master').objects.create(kind='cmt', name='Migration CMT')
        cotton = get('Master').objects.create(kind='material', name='Cotton')
        polyester = get('Master').objects.create(kind='material', name='Polyester')
        cream = get('Master').objects.create(kind='color', name='Cream')
        navy = get('Master').objects.create(kind='color', name='Navy')
        orphan = get('Master').objects.create(kind='color', name='Orphan')
        today = timezone.localdate()
        po = get('Po').objects.create(nomor='PO SPLIT MIGRATION', cmt=cmt)
        invoice = get('Invoice').objects.create(
            vendor=vendor,
            nomor='INV SPLIT MIGRATION',
            tanggal=today,
            dibuat_oleh=user,
            total_rp=100,
        )
        allocation = get('Alokasi').objects.create(
            po=po, cmt=cmt, dibuat_oleh=user, status='disetujui'
        )
        for index, (material, color, yard) in enumerate(
            ((cotton, cream, 80), (polyester, cream, 90), (cotton, navy, 100)), 1
        ):
            get('Roll').objects.create(
                invoice=invoice,
                alokasi=allocation,
                material=material,
                color=color,
                urut=index,
                yard=yard,
                status='diterima',
                tgl_kirim=today,
                tgl_terima=today,
            )
        result_ids, shipment_ids = {}, {}
        for color, result, shipped in ((cream, 200, 150), (navy, 50, 50), (orphan, 30, 10)):
            result_ids[color.pk] = get('Hasil').objects.create(po=po, color=color, pcs=result).pk
            shipment_ids[color.pk] = (
                get('KirimGudang')
                .objects.create(
                    po=po,
                    color=color,
                    pcs=shipped,
                    tanggal=today,
                    request_id=f'history-{color.pk}',
                    surat_jalan='SJ-HISTORY',
                    catatan='Retain',
                )
                .pk
            )
        # An invoice-only legacy link still gives a deterministic known material.
        invoice_only_po = get('Po').objects.create(nomor='PO INVOICE ONLY')
        group = get('InvoicePo').objects.create(invoice=invoice, po=invoice_only_po, urut=1)
        get('Roll').objects.create(
            invoice=invoice,
            invoice_po=group,
            material=polyester,
            color=navy,
            urut=4,
            yard=Decimal('75.5'),
        )
        invoice_only_result = get('Hasil').objects.create(po=invoice_only_po, color=navy, pcs=40)
        invoice_only_shipment = get('KirimGudang').objects.create(
            po=invoice_only_po, color=navy, pcs=20, tanggal=today
        )
        names = ('Roll', 'Po', 'Invoice', 'InvoicePo', 'Alokasi', 'Hasil', 'KirimGudang')
        old_fields = {
            name: [field.attname for field in get(name)._meta.concrete_fields] for name in names
        }
        before = {
            name: list(get(name).objects.order_by('pk').values_list(*old_fields[name]))
            for name in names
        }
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        current = executor.loader.project_state(self.migrate_to).apps
        after = {
            name: list(
                current.get_model('tracking', name)
                .objects.order_by('pk')
                .values_list(*old_fields[name])
            )
            for name in names
        }
        self.assertEqual(before, after)
        result_model = current.get_model('tracking', 'Hasil')
        shipment_model = current.get_model('tracking', 'KirimGudang')
        for color in (cream, orphan):
            self.assertIsNone(result_model.objects.get(pk=result_ids[color.pk]).material_id)
            self.assertIsNone(shipment_model.objects.get(pk=shipment_ids[color.pk]).material_id)
        self.assertEqual(result_model.objects.get(pk=result_ids[navy.pk]).material_id, cotton.pk)
        self.assertEqual(
            shipment_model.objects.get(pk=shipment_ids[navy.pk]).material_id, cotton.pk
        )
        self.assertEqual(
            result_model.objects.get(pk=invoice_only_result.pk).material_id, polyester.pk
        )
        self.assertEqual(
            shipment_model.objects.get(pk=invoice_only_shipment.pk).material_id, polyester.pk
        )
        self.assertEqual((result_model.objects.count(), shipment_model.objects.count()), (4, 4))
        self.assertEqual(sum(result_model.objects.values_list('pcs', flat=True)), 320)
        self.assertEqual(sum(shipment_model.objects.values_list('pcs', flat=True)), 230)
        migration = import_module('tracking.migrations.0007_production_material')
        with connection.schema_editor() as editor:
            migration.map_unambiguous_materials(current, editor)
        self.assertEqual((result_model.objects.count(), shipment_model.objects.count()), (4, 4))
        self.assertIsNone(result_model.objects.get(pk=result_ids[cream.pk]).material_id)
