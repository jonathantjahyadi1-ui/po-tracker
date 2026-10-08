import json
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import OperationalError, close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .models import (
    Alokasi,
    Hasil,
    HasilUkuran,
    Invoice,
    KirimGudang,
    KirimUkuran,
    Log,
    Master,
    Po,
    Roll,
    User,
)
from .services import kirim_gudang, po_status, po_status_many, simpan_hasil
from .size_services import kirim_ukuran, production_size_data, rekonsiliasi_ukuran, simpan_ukuran


def size_fixture(owner):
    owner.user = User.objects.create_user(
        username='size-buyer', password='password123', role='purchasing'
    )
    owner.director = User.objects.create_user(
        username='size-director', password='password123', role='direktur'
    )
    owner.material = Master.objects.create(kind='material', name='finewool')
    owner.color = Master.objects.create(kind='color', name='cap')
    owner.cmt = Master.objects.create(kind='cmt', name='CMT Ukuran')
    owner.vendor = Master.objects.create(kind='vendor', name='Vendor Ukuran')
    owner.po = Po.objects.create(nomor='PO UKURAN', cmt=owner.cmt)
    owner.today = timezone.localdate()
    owner.invoice = Invoice.objects.create(
        vendor=owner.vendor, nomor='INV UKURAN', tanggal=owner.today, dibuat_oleh=owner.user
    )
    owner.allocation = Alokasi.objects.create(
        po=owner.po, cmt=owner.cmt, status='disetujui', dibuat_oleh=owner.user
    )
    Roll.objects.create(
        invoice=owner.invoice,
        material=owner.material,
        color=owner.color,
        urut=1,
        yard=100,
        status='diterima',
        alokasi=owner.allocation,
        tgl_terima=owner.today,
    )


class ProductionSizeTests(TestCase):
    def setUp(self):
        size_fixture(self)
        self.rows = [
            {'label': 'S', 'pcs': 20},
            {'label': 'M', 'pcs': 20},
            {'label': 'L', 'pcs': 10},
        ]

    def save(self, rows=None, **kwargs):
        return simpan_ukuran(
            self.po,
            self.color,
            self.material,
            rows if rows is not None else self.rows,
            self.user,
            **kwargs,
        )

    def saved_rows(self, hasil):
        return list(hasil.sizes.values('id', 'label', 'pcs'))

    def amounts(self, hasil, **quantities):
        return [
            {'id': size.pk, 'pcs': quantities.get(size.label, '')} for size in hasil.sizes.all()
        ]

    def ship(self, hasil, request_id='size-request', **quantities):
        return kirim_ukuran(
            self.po,
            self.color,
            self.material,
            {'tanggal': self.today, 'request_id': request_id},
            self.amounts(hasil, **quantities),
            self.user,
            hasil.pk,
        )

    def legacy(self, shipped=0):
        hasil = Hasil.objects.create(po=self.po, material=self.material, color=self.color, pcs=50)
        shipment = None
        if shipped:
            shipment = KirimGudang.objects.create(
                po=self.po,
                material=self.material,
                color=self.color,
                tanggal=self.today,
                pcs=shipped,
                surat_jalan='SJ historis',
            )
        return hasil, shipment

    def test_total_edit_replaces_size_and_keeps_one_item(self):
        hasil = self.save()
        self.assertEqual(hasil.pcs, 50)
        self.assertEqual(list(hasil.sizes.values_list('label', flat=True)), ['S', 'M', 'L'])
        rows = self.saved_rows(hasil)
        rows[0]['pcs'] = 25
        self.save(rows, hasil_id=hasil.pk)
        hasil.refresh_from_db()
        self.assertEqual(hasil.pcs, 55)
        self.assertEqual(po_status(self.po)['hasil'], 55)
        self.assertEqual(Hasil.objects.count(), 1)
        self.assertEqual(len(po_status(self.po)['rows']), 1)

    def test_duplicate_normalization_is_atomic(self):
        for duplicate in ('s', ' S ', 'S'):
            with (
                self.subTest(duplicate=duplicate),
                self.assertRaisesMessage(ValidationError, 'sudah digunakan'),
            ):
                self.save([{'label': 'S', 'pcs': 20}, {'label': duplicate, 'pcs': 10}])
            self.assertFalse(Hasil.objects.exists())
            self.assertFalse(Log.objects.exists())

    def test_blank_negative_fraction_and_invalid_label_are_rejected(self):
        for value in ('', None, -1, 1.5, '1.0', True, 'x', 2147483648):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.save([{'label': 'S', 'pcs': value}])
        for label in ('', '  ', None, 'x' * 81):
            with self.subTest(label=label), self.assertRaises(ValidationError):
                self.save([{'label': label, 'pcs': 20}])
        self.assertFalse(Hasil.objects.exists())

    def test_custom_numeric_sizes_and_aliases_stay_distinct(self):
        hasil = self.save(
            [
                {'label': label, 'pcs': 1}
                for label in ['38', '40', 'OS', 'All Size', ' Custom   Fit ']
            ]
        )
        self.assertEqual(
            list(hasil.sizes.values_list('label', flat=True)),
            ['38', '40', 'OS', 'All Size', 'Custom Fit'],
        )

    def test_only_sizes_with_real_history_are_locked_in_the_ui_payload(self):
        hasil = self.save()
        key = f'{self.material.pk}-{self.color.pk}'
        self.assertEqual(
            [size['history'] for size in production_size_data(self.po)[key]['sizes']],
            [False, False, False],
        )
        self.ship(hasil, S=10)
        self.assertEqual(
            [size['history'] for size in production_size_data(self.po)[key]['sizes']],
            [True, False, False],
        )

    def test_successive_shipments_and_zero_rows(self):
        hasil = self.save()
        first = self.ship(hasil, S=10, M=5)
        self.ship(hasil, request_id='next', S=10)
        self.assertEqual((first.pcs, first.sizes.count()), (15, 2))
        data = production_size_data(self.po)[f'{self.material.pk}-{self.color.pk}']
        self.assertEqual(
            [(s['shipped'], s['remaining']) for s in data['sizes']], [(20, 0), (5, 15), (0, 10)]
        )
        self.assertEqual(po_status(self.po)['terkirim'], 25)

    def test_below_shipped_size_is_rejected_even_if_total_is_enough(self):
        hasil = self.save()
        self.ship(hasil, S=20)
        rows = self.saved_rows(hasil)
        rows[0]['pcs'], rows[1]['pcs'] = 19, 100
        with self.assertRaisesMessage(ValidationError, '20 pcs yang sudah terkirim'):
            self.save(rows, hasil_id=hasil.pk)
        hasil.refresh_from_db()
        self.assertEqual(hasil.pcs, 50)

    def test_rename_and_delete_history_are_rejected(self):
        hasil = self.save()
        self.ship(hasil, S=10)
        rows = self.saved_rows(hasil)
        rows[0]['label'] = 'XS'
        with self.assertRaisesMessage(ValidationError, 'label tidak boleh'):
            self.save(rows)
        with self.assertRaisesMessage(ValidationError, 'tidak boleh dihapus'):
            self.save(rows[1:])
        self.assertEqual(KirimUkuran.objects.count(), 1)

    def test_unshipped_size_can_be_removed_and_renamed(self):
        hasil = self.save()
        rows = self.saved_rows(hasil)
        rows[0]['label'] = 'XS'
        self.save(rows[:2])
        hasil.refresh_from_db()
        self.assertEqual((hasil.pcs, hasil.sizes.count()), (40, 2))

    def test_shipment_exceeding_one_size_rejects_whole_transaction(self):
        hasil = self.save()
        logs = Log.objects.count()
        with self.assertRaisesMessage(ValidationError, 'melebihi sisa'):
            self.ship(hasil, S=21, M=1)
        self.assertFalse(KirimGudang.objects.exists())
        self.assertFalse(KirimUkuran.objects.exists())
        self.assertEqual(Log.objects.count(), logs)
        with self.assertRaises(ValidationError):
            self.ship(hasil)

    def test_retry_is_single_transaction_and_changed_payload_conflicts(self):
        hasil = self.save()
        first = self.ship(hasil, S=20, M=20, L=10)
        logs = Log.objects.count()
        repeated = self.ship(hasil, S=20, M=20, L=10)
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(
            (KirimGudang.objects.count(), KirimUkuran.objects.count(), Log.objects.count()),
            (1, 3, logs),
        )
        with self.assertRaisesMessage(ValidationError, 'kiriman berbeda'):
            self.ship(hasil, S=1)

    def test_request_identity_required_and_aggregate_bypass_blocked(self):
        hasil = self.save()
        with self.assertRaises(ValidationError):
            self.ship(hasil, request_id=None, S=1)
        with self.assertRaises(ValidationError):
            kirim_gudang(
                self.po,
                self.color,
                {'tanggal': self.today, 'pcs': 1, 'request_id': 'old'},
                self.user,
            )
        with self.assertRaisesMessage(ValidationError, 'rincian ukuran'):
            simpan_hasil(self.po, self.color, 60, self.user)

    def test_done_only_with_positive_complete_production(self):
        self.assertFalse(po_status(self.po)['is_done'])
        hasil = self.save([{'label': 'S', 'pcs': 0}])
        self.assertFalse(po_status(self.po)['is_done'])
        self.save([{'id': hasil.sizes.get().pk, 'label': 'S', 'pcs': 200}])
        self.ship(hasil, S=150)
        self.assertEqual(po_status(self.po)['remaining'], 50)
        self.ship(hasil, request_id='last', S=50)
        self.assertTrue(po_status(self.po)['is_done'])
        other = Master.objects.create(kind='color', name='Other color')
        Roll.objects.create(
            invoice=self.invoice,
            material=self.material,
            color=other,
            urut=1,
            yard=10,
            status='diterima',
            alokasi=self.allocation,
        )
        self.assertFalse(po_status(self.po)['is_done'])

    def test_first_legacy_split_matches_total_and_creates_no_shipment(self):
        hasil, _ = self.legacy()
        with self.assertRaisesMessage(ValidationError, '50 pcs'):
            self.save([{'label': 'S', 'pcs': 40}])
        self.save()
        hasil.refresh_from_db()
        self.assertEqual((hasil.pcs, hasil.sizes_complete), (50, True))
        self.assertFalse(KirimGudang.objects.exists())

    def test_legacy_shipments_unknown_balance_and_blocked_writes(self):
        hasil, _ = self.legacy(30)
        before = po_status(self.po)
        self.save()
        after = po_status(self.po)
        self.assertEqual(
            (before['hasil'], before['terkirim'], before['code']),
            (after['hasil'], after['terkirim'], after['code']),
        )
        data = production_size_data(self.po)[f'{self.material.pk}-{self.color.pk}']
        self.assertTrue(all(s['shipped'] is None and s['remaining'] is None for s in data['sizes']))
        with self.assertRaisesMessage(ValidationError, 'Kiriman lama'):
            self.ship(hasil, S=1)
        rows = self.saved_rows(hasil)
        rows[0]['pcs'] += 1
        with self.assertRaisesMessage(ValidationError, 'perubahan total'):
            self.save(rows)

    def test_reconciliation_is_atomic_audited_and_retry_safe(self):
        hasil, shipment = self.legacy(30)
        self.save()
        payload = [{'shipment_id': shipment.pk, 'sizes': self.amounts(hasil, S=20, M=10)}]
        rekonsiliasi_ukuran(self.po, hasil.pk, payload, self.user)
        logs = Log.objects.count()
        rekonsiliasi_ukuran(self.po, hasil.pk, payload, self.user)
        shipment.refresh_from_db()
        self.assertEqual(
            (shipment.pcs, shipment.surat_jalan, shipment.tanggal), (30, 'SJ historis', self.today)
        )
        self.assertEqual(
            (KirimGudang.objects.count(), KirimUkuran.objects.count(), Log.objects.count()),
            (1, 2, logs),
        )
        self.ship(hasil, M=5)
        self.assertEqual(po_status(self.po)['terkirim'], 35)

    def test_reconciliation_requires_all_original_transactions_and_valid_allocations(self):
        hasil, first = self.legacy(15)
        second = KirimGudang.objects.create(
            po=self.po, material=self.material, color=self.color, tanggal=self.today, pcs=15
        )
        self.save()
        for payload in (
            [{'shipment_id': first.pk, 'sizes': self.amounts(hasil, S=15)}],
            [
                {'shipment_id': first.pk, 'sizes': self.amounts(hasil, S=15)},
                {'shipment_id': second.pk, 'sizes': self.amounts(hasil, S=15)},
            ],
            [
                {'shipment_id': first.pk, 'sizes': self.amounts(hasil, S=14)},
                {'shipment_id': second.pk, 'sizes': self.amounts(hasil, M=15)},
            ],
        ):
            with self.assertRaises(ValidationError):
                rekonsiliasi_ukuran(self.po, hasil.pk, payload, self.user)
            self.assertFalse(KirimUkuran.objects.exists())
        self.assertEqual(po_status(self.po)['terkirim'], 30)

    def test_old_completed_po_keeps_status_before_and_after_split(self):
        hasil, shipment = self.legacy(50)
        self.assertTrue(po_status(self.po)['is_done'])
        self.save()
        self.assertTrue(po_status(self.po)['is_done'])
        rekonsiliasi_ukuran(
            self.po,
            hasil.pk,
            [{'shipment_id': shipment.pk, 'sizes': self.amounts(hasil, S=20, M=20, L=10)}],
            self.user,
        )
        self.assertTrue(po_status(self.po)['is_done'])

    def test_other_po_size_item_and_transaction_ids_cannot_be_used(self):
        hasil = self.save()
        other = Po.objects.create(nomor='PO OTHER SIZE', cmt=self.cmt)
        foreign = Hasil.objects.create(po=other, material=self.material, color=self.color, pcs=1)
        foreign_size = HasilUkuran.objects.create(hasil=foreign, label='S', pcs=1)
        with self.assertRaises(ValidationError):
            self.save(self.rows, hasil_id=foreign.pk)
        rows = self.saved_rows(hasil)
        rows[0]['id'] = foreign_size.pk
        with self.assertRaises(ValidationError):
            self.save(rows)
        with self.assertRaises(ValidationError):
            kirim_ukuran(
                self.po,
                self.color,
                self.material,
                {'tanggal': self.today, 'request_id': 'foreign'},
                [{'id': foreign_size.pk, 'pcs': 1}],
                self.user,
            )
        with self.assertRaises(ValidationError):
            rekonsiliasi_ukuran(self.po, foreign.pk, [], self.user)
        self.assertFalse(KirimGudang.objects.exists())

    def test_readonly_roles_and_inactive_users_cannot_mutate(self):
        hasil = self.save()
        self.user.is_active = False
        for actor in (self.director, self.user):
            for operation in (
                lambda: simpan_ukuran(self.po, self.color, self.material, self.rows, actor),
                lambda: kirim_ukuran(self.po, self.color, self.material, {}, [], actor),
                lambda: rekonsiliasi_ukuran(self.po, hasil.pk, [], actor),
            ):
                with self.assertRaises(PermissionDenied):
                    operation()

    def test_batch_totals_are_derived_and_query_count_is_bounded(self):
        hasil = self.save()
        Hasil.objects.filter(pk=hasil.pk).update(pcs=999)
        with CaptureQueriesContext(connection) as queries:
            summary = po_status_many([self.po])[self.po.pk]
        self.assertLessEqual(len(queries), 6)
        self.assertEqual(summary['hasil'], 50)

    def test_ajax_save_returns_server_totals_and_preserves_errors(self):
        self.client.force_login(self.user)
        data = {
            'action': 'sizes',
            'material': self.material.pk,
            'color': self.color.pk,
            'sizes': json.dumps(self.rows),
        }
        response = self.client.post(
            f'/po/{self.po.pk}/', data, HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['summary']['hasil'], 50)
        data['sizes'] = json.dumps([{'label': 'S', 'pcs': ''}])
        response = self.client.post(
            f'/po/{self.po.pk}/', data, HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()['errors'])
        self.assertEqual(po_status(self.po)['hasil'], 50)

    def test_ajax_shipping_retries_and_rejects_missing_details(self):
        hasil = self.save()
        self.client.force_login(self.user)
        data = {
            'action': 'kirim',
            'material': self.material.pk,
            'color': self.color.pk,
            'hasil_id': hasil.pk,
            'tanggal': str(self.today),
            'request_id': 'ajax-shipment',
            'sizes': json.dumps(self.amounts(hasil, S=10)),
        }
        for _ in range(2):
            response = self.client.post(
                f'/po/{self.po.pk}/', data, HTTP_X_REQUESTED_WITH='XMLHttpRequest'
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['summary']['terkirim'], 10)
        self.assertEqual(KirimGudang.objects.count(), 1)
        del data['sizes']
        self.assertEqual(
            self.client.post(
                f'/po/{self.po.pk}/', data, HTTP_X_REQUESTED_WITH='XMLHttpRequest'
            ).status_code,
            400,
        )

    def test_readonly_detail_has_disclosure_but_no_shipment_form(self):
        self.save()
        self.client.force_login(self.director)
        page = self.client.get(f'/po/{self.po.pk}/')
        self.assertContains(page, 'Rincian ukuran')
        self.assertContains(page, 'data-can-edit="false"')
        self.assertContains(page, 'aria-expanded="false"')
        self.assertNotContains(page, 'data-size-shipment')
        self.assertEqual(
            self.client.post(f'/po/{self.po.pk}/', {'action': 'sizes'}).status_code, 403
        )

    def test_missing_result_does_not_allow_production_below_legacy_shipments(self):
        KirimGudang.objects.create(
            po=self.po, material=self.material, color=self.color, tanggal=self.today, pcs=60
        )
        with self.assertRaisesMessage(ValidationError, 'kiriman lama 60 pcs'):
            self.save()
        self.assertFalse(Hasil.objects.exists())

    def test_sqlite_write_conflict_returns_retryable_error_without_mutation(self):
        if connection.vendor != 'sqlite':
            self.skipTest('Konflik SQLite hanya berlaku pada SQLite.')
        self.client.force_login(self.user)
        payload = {
            'action': 'sizes',
            'material': self.material.pk,
            'color': self.color.pk,
            'sizes': json.dumps(self.rows),
        }
        with patch(
            'tracking.views.simpan_ukuran', side_effect=OperationalError('database is locked')
        ):
            response = self.client.post(
                f'/po/{self.po.pk}/', payload, HTTP_X_REQUESTED_WITH='XMLHttpRequest'
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn('draft tetap tersedia', response.json()['errors'][0])
        self.assertFalse(Hasil.objects.exists())

    @override_settings(DEBUG=True)
    def test_existing_sample_command_uses_explicit_sizes_and_can_be_retried(self):
        output = StringIO()
        call_command('seed_contoh', stdout=output)
        po = Po.objects.get(nomor='PO 109')
        self.assertEqual(po_status(po)['terkirim'], 1072)
        self.assertTrue(po_status(po)['is_done'])
        self.assertEqual(Hasil.objects.get(po=po).sizes.get().label, 'All Size')
        counts = (Hasil.objects.count(), KirimGudang.objects.count(), Log.objects.count())
        call_command('seed_contoh', stdout=output)
        self.assertEqual(
            counts, (Hasil.objects.count(), KirimGudang.objects.count(), Log.objects.count())
        )

    def test_size_overship_cannot_be_hidden_by_balanced_aggregate(self):
        hasil = self.save()
        shipment = KirimGudang.objects.create(
            po=self.po, material=self.material, color=self.color, tanggal=self.today, pcs=50
        )
        amounts = {'S': 30, 'M': 10, 'L': 10}
        KirimUkuran.objects.bulk_create(
            [
                KirimUkuran(kiriman=shipment, ukuran=s, pcs=amounts[s.label])
                for s in hasil.sizes.all()
            ]
        )
        summary = po_status(self.po)
        self.assertEqual((summary['hasil'], summary['terkirim'], summary['over']), (50, 50, 10))
        self.assertFalse(summary['is_done'])
        self.assertEqual(summary['rows'][0]['status_key'], 'lebih_kirim')


class ProductionSizeConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Penguncian ukuran bersamaan harus diverifikasi pada PostgreSQL.')
        size_fixture(self)
        self.hasil = simpan_ukuran(
            self.po, self.color, self.material, [{'label': 'S', 'pcs': 20}], self.user
        )
        self.size = self.hasil.sizes.get()

    def concurrent(self, callbacks):
        barrier = Barrier(2)

        def submit(callback):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '10s'")
                barrier.wait(timeout=10)
                return callback()
            except ValidationError:
                return 'conflict'
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(submit, callbacks))

    def send(self, identity):
        kirim_ukuran(
            self.po,
            self.color,
            self.material,
            {'tanggal': self.today, 'request_id': identity},
            [{'id': self.size.pk, 'pcs': 20}],
            self.user,
        )
        return 'ok'

    def test_same_remaining_size_can_only_be_sent_once(self):
        self.assertCountEqual(
            self.concurrent([lambda: self.send('a'), lambda: self.send('b')]), ['ok', 'conflict']
        )
        self.assertEqual((KirimGudang.objects.count(), po_status(self.po)['terkirim']), (1, 20))

    def test_concurrent_retry_creates_one_transaction(self):
        self.assertEqual(
            self.concurrent([lambda: self.send('same'), lambda: self.send('same')]), ['ok', 'ok']
        )
        self.assertEqual(KirimGudang.objects.count(), 1)


class ProductionSizeMigrationTests(TransactionTestCase):
    def test_migration_keeps_unknown_sizes_totals_and_original_history(self):
        executor = MigrationExecutor(connection)
        old_target = [('tracking', '0007_production_material')]
        executor.migrate(old_target)
        self.addCleanup(self.restore_latest)
        old = executor.loader.project_state(old_target).apps
        UserOld, MasterOld = old.get_model('tracking', 'User'), old.get_model('tracking', 'Master')
        user = UserOld.objects.create(username='size-migration-user', role='purchasing')
        material = MasterOld.objects.create(kind='material', name='finewool')
        color = MasterOld.objects.create(kind='color', name='cap')
        po = old.get_model('tracking', 'Po').objects.create(nomor='PO OLD SIZE', selesai=True)
        hasil = old.get_model('tracking', 'Hasil').objects.create(
            po=po, material=material, color=color, pcs=50
        )
        shipment = old.get_model('tracking', 'KirimGudang').objects.create(
            po=po,
            material=material,
            color=color,
            tanggal=timezone.localdate(),
            pcs=50,
            surat_jalan='SJ ORIGINAL',
            request_id='original-request',
        )
        old.get_model('tracking', 'Log').objects.create(
            user=user, aksi='original', objek=po.nomor, detail='original history'
        )
        self.restore_latest()
        current = Hasil.objects.get(pk=hasil.pk)
        self.assertEqual(
            (current.pcs, current.sizes_complete, current.sizes.count()), (50, False, 0)
        )
        shipped = KirimGudang.objects.get(pk=shipment.pk)
        self.assertEqual(
            (shipped.pcs, shipped.surat_jalan, shipped.request_id, shipped.sizes.count()),
            (50, 'SJ ORIGINAL', 'original-request', 0),
        )
        self.assertEqual(Log.objects.get().detail, 'original history')
        self.restore_latest()
        self.assertEqual((Hasil.objects.count(), KirimGudang.objects.count()), (1, 1))
        self.assertTrue(po_status(Po.objects.get(pk=po.pk))['is_done'])

    def restore_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
