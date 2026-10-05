from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Event
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .models import Alokasi, Hasil, Invoice, KirimGudang, Log, Master, Po, Roll, User
from .services import (
    _ids,
    ajukan_alokasi,
    batalkan_invoice,
    kirim_gudang,
    po_status,
    po_status_many,
    putuskan_alokasi,
    simpan_hasil,
    simpan_invoice,
    tautkan_po,
    terima_alokasi,
    ubah_roll_invoice,
)


class ServiceSafetyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='buyer-safety', role='purchasing')
        self.cmt = Master.objects.create(kind='cmt', name='CMT Safety')
        self.today = timezone.localdate()
        self.data = {
            'vendor': 'Vendor Safety',
            'nomor': 'INV-SAFETY',
            'tanggal': self.today - timedelta(days=2),
            'total_rp': Decimal('0'),
            'surat_jalan': '',
        }
        self.groups = [
            {'material': 'Cotton', 'rows': [{'color': 'Cream', 'yards': [Decimal(100)]}]}
        ]
        self.invoice = simpan_invoice(self.data, self.groups, self.user)
        self.roll = self.invoice.roll_set.select_related('color').get()

    def ready_po(self, nomor='PO SAFETY'):
        allocation = ajukan_alokasi([self.roll.pk], self.cmt, self.user)
        putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today)
        terima_alokasi(allocation, [self.roll.pk], self.today, self.user)
        return allocation, tautkan_po(allocation, nomor, self.user)

    def test_mutation_services_reject_director_admin_and_inactive_buyer(self):
        users = [
            User.objects.create_user(username='director-safety', role='direktur'),
            User.objects.create_user(username='admin-safety', role='admin'),
            User.objects.create_user(
                username='inactive-safety', role='purchasing', is_active=False
            ),
        ]
        counts = (Invoice.objects.count(), Roll.objects.count(), Log.objects.count())
        for user in users:
            for callback in (
                lambda: ajukan_alokasi([self.roll.pk], self.cmt, user),
                lambda: batalkan_invoice(self.invoice, user),
                lambda: simpan_invoice(self.data, self.groups, user),
                lambda: ubah_roll_invoice(self.invoice, {f'existing-{self.roll.pk}': '50'}, user),
            ):
                with self.subTest(role=user.role, active=user.is_active):
                    with self.assertRaises(PermissionDenied):
                        callback()
        self.assertEqual(
            counts, (Invoice.objects.count(), Roll.objects.count(), Log.objects.count())
        )
        self.assertFalse(Alokasi.objects.exists())

    def test_stale_cmt_activation_is_revalidated_on_submit_and_acc(self):
        Master.objects.filter(pk=self.cmt.pk).update(active=False)
        with self.assertRaisesMessage(ValidationError, 'CMT aktif'):
            ajukan_alokasi([self.roll.pk], self.cmt, self.user)
        Master.objects.filter(pk=self.cmt.pk).update(active=True)
        allocation = ajukan_alokasi([self.roll.pk], self.cmt, self.user)
        Master.objects.filter(pk=self.cmt.pk).update(active=False)
        logs = Log.objects.count()
        with self.assertRaisesMessage(ValidationError, 'nonaktif'):
            putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today)
        allocation.refresh_from_db()
        self.roll.refresh_from_db()
        self.assertEqual((allocation.status, self.roll.status), ('menunggu', 'menunggu'))
        self.assertEqual(Log.objects.count(), logs)

    def test_invalid_dates_and_long_delivery_notes_fail_atomically(self):
        with self.assertRaisesMessage(ValidationError, '80 karakter'):
            simpan_invoice(
                dict(self.data, nomor='INV-LONG', surat_jalan='x' * 81), self.groups, self.user
            )
        self.assertFalse(Invoice.objects.filter(nomor='INV-LONG').exists())
        allocation = ajukan_alokasi([self.roll.pk], self.cmt, self.user)
        for arguments in (
            {'tgl_kirim': self.today, 'sj_kirim': 'x' * 81},
            {'tgl_kirim': self.today.isoformat()},
            {'tgl_kirim': ''},
        ):
            with self.assertRaises(ValidationError):
                putuskan_alokasi(allocation, 'acc', self.user, **arguments)
        allocation.refresh_from_db()
        self.roll.refresh_from_db()
        self.assertEqual((allocation.status, self.roll.status), ('menunggu', 'menunggu'))
        putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today)
        terima_alokasi(allocation, [self.roll.pk], self.today, self.user)
        po = tautkan_po(allocation, 'PO LIMITS', self.user)
        simpan_hasil(po, self.roll.color, 100, self.user)
        for data in (
            {'tanggal': self.today, 'pcs': 10, 'surat_jalan': 'x' * 81},
            {'tanggal': self.today.isoformat(), 'pcs': 10},
            {'tanggal': self.today, 'pcs': 10, 'request_id': 123},
        ):
            with self.assertRaises(ValidationError):
                kirim_gudang(po, self.roll.color, data, self.user)
        self.assertFalse(KirimGudang.objects.exists())

    def test_cancelled_invoice_and_allocated_roll_are_locked_for_stale_callers(self):
        batalkan_invoice(self.invoice, self.user)
        with self.assertRaisesMessage(ValidationError, 'dibatalkan'):
            ajukan_alokasi([self.roll.pk], self.cmt, self.user)
        with self.assertRaisesMessage(ValidationError, 'dibatalkan'):
            ubah_roll_invoice(self.invoice, {f'existing-{self.roll.pk}': '10'}, self.user)
        self.assertFalse(Alokasi.objects.exists())
        self.roll.refresh_from_db()
        self.assertEqual((self.roll.yard, self.roll.status), (Decimal('100'), 'tersedia'))

    def test_receipt_retry_keeps_original_date_and_does_not_add_logs(self):
        allocation = ajukan_alokasi([self.roll.pk], self.cmt, self.user)
        putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today)
        terima_alokasi(allocation, [self.roll.pk], self.today, self.user)
        logs = Log.objects.count()
        terima_alokasi(allocation, [self.roll.pk], self.today, self.user)
        self.assertEqual(Log.objects.count(), logs)
        with self.assertRaises(ValidationError):
            terima_alokasi(allocation, [self.roll.pk], self.today + timedelta(days=1), self.user)
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.tgl_terima, self.today)

    def test_rejected_membership_is_retained_after_reallocation(self):
        first = ajukan_alokasi([self.roll.pk], self.cmt, self.user)
        putuskan_alokasi(first, 'tolak', self.user, alasan='Tujuan perlu diperiksa')
        second = ajukan_alokasi([self.roll.pk], self.cmt, self.user)
        self.assertEqual(first.items.get().roll_id, self.roll.pk)
        self.assertEqual(second.items.get().roll_id, self.roll.pk)
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.alokasi_id, second.pk)

    def test_invoice_metadata_omitted_by_ui_keeps_history(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(catatan='Catatan historis')
        changed = dict(self.data, catatan='', total_rp=Decimal(25))
        simpan_invoice(changed, [], self.user, invoice=self.invoice)
        self.invoice.refresh_from_db()
        self.assertEqual(
            (self.invoice.catatan, self.invoice.total_rp), ('Catatan historis', Decimal(25))
        )

    def test_invoice_creation_retry_keeps_single_invoice_and_single_audit(self):
        payload = dict(self.data, nomor='INV-REPLAY', request_id='new-invoice-token')
        first = simpan_invoice(payload, self.groups, self.user)
        counts = (Invoice.objects.count(), Roll.objects.count(), Log.objects.count())
        repeated = simpan_invoice(payload, self.groups, self.user)
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(
            counts, (Invoice.objects.count(), Roll.objects.count(), Log.objects.count())
        )
        with self.assertRaisesMessage(ValidationError, 'data berbeda'):
            simpan_invoice(dict(payload, total_rp=Decimal(50)), self.groups, self.user)

    def test_append_retry_does_not_duplicate_rolls_or_undo_later_edits(self):
        payload = dict(
            self.data,
            request_id='edit-invoice-token',
            existing_roll_input={f'existing-{self.roll.pk}': '80'},
        )
        simpan_invoice(payload, self.groups, self.user, self.invoice)
        self.assertEqual(self.invoice.roll_set.count(), 2)
        later = dict(
            self.data,
            request_id='later-invoice-token',
            existing_roll_input={f'existing-{self.roll.pk}': '90'},
        )
        simpan_invoice(later, [], self.user, self.invoice)
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.yard, Decimal(90))
        logs = Log.objects.count()
        simpan_invoice(payload, self.groups, self.user, self.invoice)
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.yard, Decimal(90))
        self.assertEqual(self.invoice.roll_set.count(), 2)
        self.assertEqual(Log.objects.count(), logs)
        modified = dict(payload, existing_roll_input={f'existing-{self.roll.pk}': '60'})
        with self.assertRaisesMessage(ValidationError, 'data berbeda'):
            simpan_invoice(modified, self.groups, self.user, self.invoice)
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.yard, Decimal(90))

    def test_invoice_request_identity_is_bound_to_actor_and_target(self):
        payload = dict(self.data, request_id='actor-target-token')
        simpan_invoice(payload, [], self.user, self.invoice)
        other_buyer = User.objects.create_user(username='other-buyer', role='purchasing')
        with self.assertRaisesMessage(ValidationError, 'data berbeda'):
            simpan_invoice(payload, [], other_buyer, self.invoice)
        other = simpan_invoice(dict(self.data, nomor='INV-OTHER'), self.groups, self.user)
        with self.assertRaisesMessage(ValidationError, 'data berbeda'):
            simpan_invoice(payload, [], self.user, other)

    def test_global_shipment_identity_cannot_be_reused_by_another_po(self):
        _, po = self.ready_po()
        simpan_hasil(po, self.roll.color, 100, self.user)
        kirim_gudang(
            po,
            self.roll.color,
            {'tanggal': self.today, 'pcs': 10, 'request_id': 'shared-id'},
            self.user,
        )
        other_invoice = simpan_invoice(dict(self.data, nomor='INV-OTHER'), self.groups, self.user)
        other_roll = other_invoice.roll_set.select_related('color').get()
        allocation = ajukan_alokasi([other_roll.pk], self.cmt, self.user)
        putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today)
        terima_alokasi(allocation, [other_roll.pk], self.today, self.user)
        other_po = tautkan_po(allocation, 'PO OTHER', self.user)
        simpan_hasil(other_po, other_roll.color, 100, self.user)
        logs = Log.objects.count()
        with self.assertRaisesMessage(ValidationError, 'kiriman berbeda'):
            kirim_gudang(
                other_po,
                other_roll.color,
                {'tanggal': self.today, 'pcs': 10, 'request_id': 'shared-id'},
                self.user,
            )
        self.assertEqual(KirimGudang.objects.count(), 1)
        self.assertEqual(Log.objects.count(), logs)

    def test_legitimate_identical_shipments_have_distinct_request_identity(self):
        _, po = self.ready_po()
        simpan_hasil(po, self.roll.color, 100, self.user)
        for identity in ('first-shipment', 'second-shipment'):
            payload = {'tanggal': self.today, 'pcs': 40, 'request_id': identity}
            first = kirim_gudang(po, self.roll.color, payload, self.user)
            repeated = kirim_gudang(po, self.roll.color, payload, self.user)
            self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(KirimGudang.objects.count(), 2)
        self.assertEqual(po_status(po)['remaining'], 20)

    def test_empty_po_and_batch_resolver_do_not_complete_missing_production(self):
        pos = [Po.objects.create(nomor=f'PO EMPTY {index}', cmt=self.cmt) for index in range(20)]
        with CaptureQueriesContext(connection) as captured:
            summaries = po_status_many(pos)
        self.assertLessEqual(len(captured), 4)
        for po in pos:
            self.assertEqual(summaries[po.pk]['code'], 'belum_ada_hasil')
            self.assertFalse(summaries[po.pk]['is_done'])
        self.assertEqual(po_status_many([]), {})

    def test_batch_resolver_keeps_null_zero_and_legacy_overship_distinct(self):
        _, po = self.ready_po()
        other_color = Master.objects.create(kind='color', name='Navy')
        Hasil.objects.create(po=po, color=other_color, pcs=0)
        KirimGudang.objects.create(po=po, color=other_color, tanggal=self.today, pcs=10)
        summary = po_status(po)
        by_color = {row['color_id']: row for row in summary['rows']}
        self.assertEqual(summary['code'], 'lebih_kirim')
        self.assertEqual(summary['over'], 10)
        self.assertFalse(by_color[self.roll.color_id]['has_result'])
        self.assertIsNone(by_color[self.roll.color_id]['remaining'])
        self.assertEqual(by_color[other_color.pk]['hasil'], 0)


class InvoiceLockConcurrencyTests(TransactionTestCase):
    """These cases require the real row locks implemented by PostgreSQL."""

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Penguncian invoice/alokasi harus diuji pada PostgreSQL.')
        self.user = User.objects.create_user(username='buyer-locking', role='purchasing')
        self.cmt = Master.objects.create(kind='cmt', name='CMT Locking')
        self.data = {
            'vendor': 'Vendor Locking',
            'nomor': 'INV-LOCKING',
            'tanggal': timezone.localdate(),
            'total_rp': Decimal(0),
        }
        self.invoice = simpan_invoice(
            self.data,
            [{'material': 'Cotton', 'color': 'Cream', 'yards': [Decimal(100)]}],
            self.user,
        )
        self.roll = self.invoice.roll_set.get()

    def concurrent(self, callbacks):
        barrier = Barrier(2)

        def submit(callback):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '10s'")
                barrier.wait(timeout=10)
                callback()
                return 'ok'
            except ValidationError:
                return 'conflict'
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(submit, callbacks))

    def test_cancel_and_allocate_cannot_both_succeed(self):
        outcomes = self.concurrent(
            [
                lambda: batalkan_invoice(self.invoice, self.user),
                lambda: ajukan_alokasi([self.roll.pk], self.cmt, self.user),
            ]
        )
        self.assertCountEqual(outcomes, ['ok', 'conflict'])
        self.invoice.refresh_from_db()
        self.assertFalse(self.invoice.dibatalkan and Alokasi.objects.exists())

    def test_invoice_edit_and_allocate_finish_without_lock_inversion(self):
        outcomes = self.concurrent(
            [
                lambda: simpan_invoice(
                    dict(self.data, nomor='INV-CHANGED'), [], self.user, self.invoice
                ),
                lambda: ajukan_alokasi([self.roll.pk], self.cmt, self.user),
            ]
        )
        self.assertIn(outcomes, (['ok', 'ok'], ['conflict', 'ok']))
        self.assertEqual(Alokasi.objects.count(), 1)

    def test_po_fk_creation_does_not_deadlock_with_colliding_allocation(self):
        allocation = ajukan_alokasi([self.roll.pk], self.cmt, self.user)
        today = timezone.localdate()
        putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=today)
        terima_alokasi(allocation, [self.roll.pk], today, self.user)
        cmt_locked, roll_locked = Event(), Event()

        def pause_after_cmt_lock(values):
            cmt_locked.set()
            self.assertTrue(roll_locked.wait(timeout=10))
            return _ids(values)

        def link_po_while_roll_locked():
            self.assertTrue(cmt_locked.wait(timeout=10))
            with transaction.atomic():
                Alokasi.objects.select_for_update().get(pk=allocation.pk)
                Roll.objects.select_for_update().get(pk=self.roll.pk)
                roll_locked.set()
                tautkan_po(allocation, 'PO FIRST', self.user)

        # Hold the actual service lock at its next step to reproduce the FK lock race.
        # FOR UPDATE on CMT would block the new PO while each transaction waits on the other.
        with patch('tracking.services._ids', side_effect=pause_after_cmt_lock):
            outcomes = self.concurrent(
                [
                    lambda: ajukan_alokasi([self.roll.pk], self.cmt, self.user),
                    link_po_while_roll_locked,
                ]
            )
        self.assertEqual(outcomes, ['conflict', 'ok'])
        self.assertEqual(Po.objects.get().cmt_id, self.cmt.pk)
