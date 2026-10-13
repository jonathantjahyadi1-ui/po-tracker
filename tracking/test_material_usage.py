from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import (
    IntegrityError,
    OperationalError,
    close_old_connections,
    connection,
    transaction,
)
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .forms import MaterialUsageFormSet
from .models import (
    Alokasi,
    Hasil,
    Invoice,
    KirimGudang,
    Log,
    Master,
    MaterialUsage,
    Po,
    ProductionUnit,
    Roll,
    User,
)
from .parsers import parse_number
from .production_services import (
    change_draft_product,
    estimate_pcs,
    parse_usage,
    save_delivery_decision,
    save_material_usages,
    save_product_usage,
    set_legacy_snapshots,
)
from .services import (
    ajukan_alokasi,
    kirim_gudang,
    po_status,
    po_status_many,
    putuskan_alokasi,
    simpan_invoice,
    tautkan_po,
    terima_alokasi,
)
from .size_services import simpan_ukuran


class EstimationTests(TestCase):
    def test_acceptance_rounding_boundaries(self):
        cases = [
            ('100', 50),
            ('100.6', 50),
            ('100.98', 50),
            ('101', 51),
            ('101.6', 51),
            ('100.9998', 50),
            ('101.0002', 51),
            ('0.8', 0),
            ('21.2', 11),
        ]
        for yard, expected in cases:
            with self.subTest(yard=yard):
                self.assertEqual(estimate_pcs(yard, '2'), expected)

    def test_comma_preserves_fraction_and_invalid_usage_is_rejected(self):
        self.assertEqual(parse_usage('2,5'), Decimal('2.5'))
        self.assertEqual(parse_usage('1.12345678'), Decimal('1.12345678'))
        self.assertEqual(parse_usage(Decimal('0.00000001')), Decimal('0.00000001'))
        for value in ['', None, '0', '-1', 'NaN', 'Infinity', 'abc', '2,5.0', '1.123456789']:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_usage(value)

    def test_yard_parser_preserves_four_decimal_boundary(self):
        self.assertEqual(parse_number('100.9998'), Decimal('100.9998'))
        self.assertEqual(parse_number('101,0002'), Decimal('101.0002'))
        self.assertEqual(parse_number('1.773'), Decimal('1773'))


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class MaterialUsageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='patent-buyer', role='purchasing')
        self.director = User.objects.create_user(username='patent-director', role='direktur')
        self.product = Master.objects.create(kind='product', name='Fuji')
        self.other_product = Master.objects.create(kind='product', name='Berna')
        self.material = Master.objects.create(kind='material', name='Bahan A')
        self.other_material = Master.objects.create(kind='material', name='Bahan B')
        self.color = Master.objects.create(kind='color', name='Cream')
        self.cmt = Master.objects.create(kind='cmt', name='CMT Paten')
        self.today = timezone.localdate()
        self.save_usage(self.product, self.material, '2')
        self.save_usage(self.product, self.other_material, '2,5')
        self.save_usage(self.other_product, self.material, '1,6')

    def save_usage(self, product, material, amount, *, active=True, edit=False):
        old = MaterialUsage.objects.filter(product=product, material=material).first()
        save_material_usages(
            product,
            [
                {
                    'id': old.pk if edit and old else None,
                    'material': material,
                    'usage': amount,
                    'active': active,
                }
            ],
            self.user,
        )
        return MaterialUsage.objects.get(product=product, material=material)

    def allocation(self, yards=('100',), material=None, color=None):
        invoice = simpan_invoice(
            {
                'vendor': 'Vendor Paten',
                'nomor': uuid4().hex,
                'tanggal': self.today,
                'total_rp': Decimal('1000'),
            },
            [
                {
                    'material': (material or self.material).name,
                    'color': (color or self.color).name,
                    'yards': [Decimal(y) for y in yards],
                }
            ],
            self.user,
        )
        ids = list(invoice.roll_set.values_list('pk', flat=True))
        allocation = ajukan_alokasi(ids, self.cmt, self.user)
        putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today)
        terima_alokasi(allocation, ids, self.today, self.user)
        return allocation

    def ready(self, yards=('100',), material=None, color=None, product=None, nomor=None):
        allocation = self.allocation(yards, material, color)
        po = tautkan_po(
            allocation, nomor or f'PO {uuid4().hex}', self.user, product=product or self.product
        )
        return po, ProductionUnit.objects.get(
            po=po, material=material or self.material, color=color or self.color
        )

    def result(self, po, pcs=50, material=None, color=None):
        return simpan_ukuran(
            po,
            color or self.color,
            material or self.material,
            [{'label': 'All Size', 'pcs': pcs}],
            self.user,
        )

    def ship(self, po, pcs, *, result=None, identity=None, **extra):
        result = result or Hasil.objects.get(po=po, material=self.material, color=self.color)
        return kirim_gudang(
            po,
            result.color,
            {
                'tanggal': self.today,
                'request_id': identity or uuid4().hex,
                'sizes': [{'id': result.sizes.get().pk, 'pcs': pcs}],
                **extra,
            },
            self.user,
            material=result.material,
        )

    def decision(self, po, unit, continuation='final', note=''):
        return save_delivery_decision(po, unit.pk, continuation, note, self.user)

    def test_master_product_material_values_are_independent(self):
        self.assertEqual(
            MaterialUsage.objects.get(
                product=self.product, material=self.material
            ).usage_yard_per_pcs,
            Decimal('2'),
        )
        self.assertEqual(
            MaterialUsage.objects.get(
                product=self.product, material=self.other_material
            ).usage_yard_per_pcs,
            Decimal('2.5'),
        )
        self.save_usage(self.product, self.material, '3', edit=True)
        self.assertEqual(
            MaterialUsage.objects.get(
                product=self.other_product, material=self.material
            ).usage_yard_per_pcs,
            Decimal('1.6'),
        )
        entry = MaterialUsage.objects.get(product=self.product, material=self.material)
        self.assertEqual(entry.updated_by, self.user)
        self.assertTrue(
            Log.objects.filter(aksi='ubah pemakaian paten', detail__contains='before').exists()
        )

    def test_duplicate_and_invalid_batch_are_atomic(self):
        with self.assertRaisesMessage(ValidationError, 'sudah ada'):
            self.save_usage(self.product, self.material, '4')
        old = MaterialUsage.objects.get(product=self.product, material=self.material)
        with self.assertRaises(ValidationError):
            save_material_usages(
                self.product,
                [
                    {'id': old.pk, 'material': self.material, 'usage': '4'},
                    {'material': self.other_material, 'usage': '0'},
                ],
                self.user,
            )
        old.refresh_from_db()
        self.assertEqual(old.usage_yard_per_pcs, Decimal(2))

    def test_duplicate_formset_marks_material_input(self):
        formset = MaterialUsageFormSet(
            {
                'form-TOTAL_FORMS': '2',
                'form-INITIAL_FORMS': '0',
                'form-0-material': str(self.material.pk),
                'form-0-usage': '2',
                'form-0-active': 'on',
                'form-1-material': str(self.material.pk),
                'form-1-usage': '3',
                'form-1-active': 'on',
            }
        )
        self.assertFalse(formset.is_valid())
        self.assertIn('material', formset.forms[1].errors)

    def test_missing_or_inactive_standard_blocks_new_po_without_partial_writes(self):
        self.save_usage(self.product, self.material, '2', active=False, edit=True)
        allocation = self.allocation()
        count = Po.objects.count()
        with self.assertRaisesMessage(ValidationError, 'belum diisi di Master'):
            tautkan_po(allocation, 'PO MISSING', self.user, product=self.product)
        allocation.refresh_from_db()
        self.assertIsNone(allocation.po_id)
        self.assertEqual(Po.objects.count(), count)
        self.assertFalse(ProductionUnit.objects.exists())
        self.assertEqual(Invoice.objects.count(), 1)

    def test_new_allocation_requires_product_id(self):
        allocation = self.allocation()
        with self.assertRaisesMessage(ValidationError, 'Pilih produk'):
            tautkan_po(allocation, 'PO NO PRODUCT', self.user, produk='Fuji')

    def test_snapshot_survives_master_edits_and_deactivation(self):
        po, unit = self.ready()
        self.save_usage(self.product, self.material, '2.5', edit=True)
        other_po, other_unit = self.ready()
        self.assertEqual(
            (unit.usage_snapshot, other_unit.usage_snapshot), (Decimal('2'), Decimal('2.5'))
        )
        self.assertEqual(
            (po_status(po)['rows'][0]['estimate'], po_status(other_po)['rows'][0]['estimate']),
            (50, 40),
        )
        self.save_usage(self.product, self.material, '2.5', active=False, edit=True)
        self.decision(po, unit, note='Produksi gagal')
        self.assertEqual(po_status(po)['code'], 'selesai_selisih')

    def test_rolls_are_summed_before_rounding(self):
        po, _ = self.ready(('10.6', '10.6'))
        row = po_status(po)['rows'][0]
        self.assertEqual((row['allocated_yard'], row['estimate']), (Decimal('21.2'), 11))

    def test_database_keeps_precision_at_half_boundary(self):
        for yard, expected in [('100.9998', 50), ('101.0002', 51)]:
            po, _ = self.ready((yard,))
            self.assertEqual(po_status(po)['rows'][0]['estimate'], expected)
            self.assertEqual(Roll.objects.get(alokasi__po=po).yard, Decimal(yard))

    def test_invoice_yards_are_not_shared_between_products(self):
        invoice = simpan_invoice(
            {
                'vendor': 'Vendor Split',
                'nomor': 'INV SPLIT',
                'tanggal': self.today,
                'total_rp': Decimal(1000),
            },
            [
                {
                    'material': self.material.name,
                    'color': self.color.name,
                    'yards': [Decimal(60), Decimal(40)],
                }
            ],
            self.user,
        )
        pos = []
        for roll, product in zip(
            invoice.roll_set.order_by('pk'), [self.product, self.other_product]
        ):
            allocation = ajukan_alokasi([roll.pk], self.cmt, self.user)
            putuskan_alokasi(allocation, 'acc', self.user, tgl_kirim=self.today)
            terima_alokasi(allocation, [roll.pk], self.today, self.user)
            pos.append(tautkan_po(allocation, 'PO ' + product.name, self.user, product=product))
        self.assertEqual([po_status(p)['estimate'] for p in pos], [30, 25])

    def test_cumulative_shortage_requires_explicit_decision(self):
        po, unit = self.ready()
        self.result(po)
        self.ship(po, 30)
        self.ship(po, 19)
        row = po_status(po)['rows'][0]
        self.assertEqual(
            (row['shipped'], row['estimate'], row['difference'], row['status_key']),
            (49, 50, -1, 'menunggu_keterangan'),
        )
        self.decision(po, unit, 'undecided', 'Sudah ada catatan')
        self.assertEqual(po_status(po)['code'], 'menunggu_keterangan')
        self.decision(po, unit, 'follow_up', 'Masih dikirim')
        self.assertEqual(po_status(po)['code'], 'kurang_kirim')

    def test_final_shortage_rejects_blank_and_whitespace_without_partial_completion(self):
        po, unit = self.ready()
        self.result(po)
        self.ship(po, 49)
        for note in ['', '  \n\t']:
            with self.subTest(note=note), self.assertRaises(ValidationError):
                self.decision(po, unit, note=note)
        unit.refresh_from_db()
        self.assertEqual(unit.continuation, 'undecided')
        self.assertIsNone(unit.finalized_at)
        self.assertFalse(po_status(po)['is_done'])

    def test_final_shortage_preserves_reason_and_totals(self):
        po, unit = self.ready()
        self.result(po)
        self.ship(po, 49)
        self.decision(po, unit, note='  Satu pcs rusak  ')
        unit.refresh_from_db()
        self.assertEqual(unit.discrepancy_note, 'Satu pcs rusak')
        self.assertEqual(unit.finalized_by, self.user)
        self.assertIsNotNone(unit.finalized_at)
        summary = po_status(po)
        self.assertEqual(
            (summary['code'], summary['terkirim'], summary['remaining']), ('selesai_selisih', 49, 1)
        )

    def test_equal_shipments_stay_open_until_final(self):
        po, unit = self.ready()
        self.result(po)
        self.ship(po, 49)
        self.ship(po, 1)
        self.assertFalse(po_status(po)['is_done'])
        self.decision(po, unit)
        self.assertEqual(po_status(po)['code'], 'done')

    def test_overdelivery_is_kept_and_needs_no_note(self):
        po, unit = self.ready()
        self.result(po, 51)
        self.ship(po, 51)
        self.decision(po, unit)
        row = po_status(po)['rows'][0]
        self.assertEqual((row['difference'], row['shipped'], row['status_key']), (1, 51, 'done'))
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('po_detail', args=[po.pk])), '+1 pcs')

    def test_zero_production_can_close_with_explanation(self):
        po, unit = self.ready()
        self.result(po, 0)
        self.decision(po, unit, note='Produksi gagal seluruhnya')
        self.assertEqual(po_status(po)['rows'][0]['difference'], -50)
        self.assertEqual(po_status(po)['code'], 'selesai_selisih')

    def test_zero_estimate_does_not_complete_automatically(self):
        po, unit = self.ready(('0.8',))
        self.assertEqual(po_status(po)['estimate'], 0)
        self.assertFalse(po_status(po)['is_done'])
        self.client.force_login(self.user)
        self.assertContains(
            self.client.get(reverse('po_detail', args=[po.pk])),
            'Alokasi belum mencapai estimasi 1 pcs',
        )
        self.decision(po, unit)
        self.assertTrue(po_status(po)['is_done'])

    def test_other_unit_overage_cannot_cancel_shortage_or_open_unit(self):
        po, unit = self.ready()
        other_color = Master.objects.create(kind='color', name='Navy')
        _, second = self.ready(color=other_color, nomor=po.nomor)
        first_result, second_result = self.result(po, 50), self.result(po, 51, color=other_color)
        self.ship(po, 49, result=first_result)
        self.ship(po, 51, result=second_result)
        self.decision(po, second)
        self.assertEqual(po_status(po)['code'], 'menunggu_keterangan')
        with self.assertRaises(ValidationError):
            self.decision(po, unit)
        self.decision(po, unit, 'follow_up', 'Menyusul')
        self.assertEqual(po_status(po)['code'], 'kurang_kirim')
        self.decision(po, unit, note='Satu rusak')
        self.assertEqual(po_status(po)['code'], 'selesai_selisih')

    def test_final_blocks_shipments_size_edits_and_new_allocated_yards(self):
        po, unit = self.ready()
        result = self.result(po)
        self.decision(po, unit, note='Tidak ada produksi')
        with self.assertRaisesMessage(ValidationError, 'final'):
            self.ship(po, 1)
        with self.assertRaisesMessage(ValidationError, 'final'):
            simpan_ukuran(
                po,
                self.color,
                self.material,
                [{'id': result.sizes.get().pk, 'label': 'All Size', 'pcs': 51}],
                self.user,
            )
        allocation = self.allocation()
        with self.assertRaisesMessage(ValidationError, 'final'):
            tautkan_po(allocation, po.nomor, self.user, product=self.product)

    def test_shipped_unit_blocks_extra_yards_and_product_changes(self):
        po, _ = self.ready()
        self.result(po)
        self.ship(po, 1)
        allocation = self.allocation()
        with self.assertRaisesMessage(ValidationError, 'Sudah ada pengiriman'):
            tautkan_po(allocation, po.nomor, self.user, product=self.product)
        with self.assertRaisesMessage(ValidationError, 'alur koreksi'):
            change_draft_product(po, self.other_product, self.user)

    def test_draft_product_change_explicitly_replaces_snapshot_and_is_logged(self):
        po, unit = self.ready()
        change_draft_product(po, self.other_product, self.user)
        unit.refresh_from_db()
        self.assertEqual(
            (unit.product_id, unit.usage_snapshot), (self.other_product.pk, Decimal('1.6'))
        )
        self.assertTrue(
            Log.objects.filter(aksi='ubah produk draft', detail__contains='before').exists()
        )

    def test_same_unit_cannot_mix_different_master_snapshots(self):
        po, _ = self.ready()
        self.save_usage(self.product, self.material, '2.5', edit=True)
        allocation = self.allocation()
        with self.assertRaisesMessage(ValidationError, 'tidak tercampur'):
            tautkan_po(allocation, po.nomor, self.user, product=self.product)
        self.assertEqual(po_status(po)['estimate'], 50)

    def test_duplicate_shipment_retry_before_and_after_final_is_idempotent(self):
        po, unit = self.ready()
        self.result(po)
        identity = uuid4().hex
        first = self.ship(po, 49, identity=identity)
        self.assertEqual(self.ship(po, 49, identity=identity).pk, first.pk)
        self.decision(po, unit, note='Satu rusak')
        self.assertEqual(self.ship(po, 49, identity=identity).pk, first.pk)
        self.assertEqual(KirimGudang.objects.filter(po=po).count(), 1)
        with self.assertRaises(ValidationError):
            self.ship(po, 48, identity=identity)

    def test_invalid_shipping_pcs_rejected(self):
        po, _ = self.ready()
        self.result(po)
        for pcs in ['1.5', 0, -1, True]:
            with self.subTest(pcs=pcs), self.assertRaises(ValidationError):
                self.ship(po, pcs)
        self.assertFalse(KirimGudang.objects.exists())

    def test_permissions_are_enforced_in_services_and_direct_requests(self):
        po, unit = self.ready()
        for role in ['direktur', 'accounting', 'admin']:
            user = User.objects.create_user(username='patent-' + role, role=role)
            with self.subTest(role=role):
                with self.assertRaises(PermissionDenied):
                    save_material_usages(self.product, [], user)
                with self.assertRaises(PermissionDenied):
                    save_delivery_decision(po, unit.pk, 'final', 'Alasan', user)
                self.client.force_login(user)
                self.assertEqual(
                    self.client.post(
                        reverse('po_detail', args=[po.pk]),
                        {
                            'action': 'delivery_decision',
                            'unit_id': unit.pk,
                            'continuation': 'final',
                            'discrepancy_note': 'Alasan',
                        },
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    self.client.post(
                        reverse('material_usage'), {'product': self.product.pk}
                    ).status_code,
                    403,
                )

    def test_unit_id_from_another_po_is_rejected(self):
        po, _ = self.ready()
        _, other = self.ready()
        with self.assertRaises(ValidationError):
            self.decision(po, other, note='Invalid')

    def test_legacy_active_has_no_fake_estimate_and_requires_explicit_setup(self):
        po, unit = self.ready()
        unit.delete()  # Simulate a pre-migration active PO, without inventing a usage value.
        summary = po_status(po)
        self.assertIsNone(summary['estimate'])
        self.assertIsNone(summary['rows'][0]['estimate'])
        self.assertFalse(summary['is_done'])
        self.client.force_login(self.user)
        self.assertContains(
            self.client.get(reverse('po_detail', args=[po.pk])), 'Patokan belum ditetapkan'
        )
        set_legacy_snapshots(po, self.product, self.user)
        self.assertEqual(po_status(po)['estimate'], 50)

    def test_legacy_completed_stays_closed_without_snapshot(self):
        po, unit = self.ready()
        unit.delete()
        Po.objects.filter(pk=po.pk).update(legacy_completed=True)
        self.assertEqual(po_status(po)['code'], 'done')
        with self.assertRaises(ValidationError):
            set_legacy_snapshots(po, self.product, self.user)

    def test_detail_and_list_share_status_and_finalize_field_error(self):
        po, unit = self.ready()
        self.result(po)
        self.ship(po, 49)
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('po_detail', args=[po.pk]),
            {
                'action': 'delivery_decision',
                'unit_id': unit.pk,
                'continuation': 'final',
                'discrepancy_note': '  ',
            },
        )
        self.assertContains(response, 'Keterangan Selisih wajib diisi')
        self.assertContains(response, 'aria-invalid="true"')
        self.decision(po, unit, note='Satu rusak')
        detail = self.client.get(reverse('po_detail', args=[po.pk]))
        listing = self.client.get(reverse('po_cmt_list', args=[self.cmt.pk]))
        self.assertEqual(
            detail.context['summary']['code'], listing.context['pos'][0].summary['code']
        )
        self.assertContains(detail, 'Satu rusak')
        self.assertContains(listing, 'Selesai dengan Selisih')

    def test_master_page_and_post_keep_comma_value(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('material_usage'), {'product': self.product.pk})
        self.assertContains(response, 'Tambah Bahan')
        self.assertContains(response, self.user.username)
        usage = MaterialUsage.objects.get(product=self.product, material=self.material)
        response = self.client.post(
            reverse('material_usage'),
            {
                'product': self.product.pk,
                'form-TOTAL_FORMS': '1',
                'form-INITIAL_FORMS': '1',
                'form-0-id': usage.pk,
                'form-0-material': self.material.pk,
                'form-0-usage': '2,75',
                'form-0-active': 'on',
            },
        )
        self.assertEqual(response.status_code, 302)
        usage.refresh_from_db()
        self.assertEqual(usage.usage_yard_per_pcs, Decimal('2.75'))

    def test_master_product_requires_positive_usage_and_preserves_invalid_input(self):
        self.client.force_login(self.user)
        for value in ['', '0', '-1', 'NaN', '1.123456789']:
            with self.subTest(usage=value):
                response = self.client.post(
                    reverse('master'), {'kind': 'product', 'name': 'Produk baru', 'usage': value}
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn('usage', response.context['form'].errors)
                self.assertContains(response, 'value="Produk baru"')
                self.assertFalse(Master.objects.filter(name='Produk baru').exists())
        response = self.client.post(
            reverse('master'),
            {'kind': 'product', 'name': '  Produk   baru ', 'usage': '2,50000001'},
        )
        self.assertRedirects(response, reverse('master') + '?tab=product')
        product = Master.objects.get(name='Produk baru')
        usage = MaterialUsage.objects.get(product=product, material__isnull=True)
        self.assertEqual(usage.usage_yard_per_pcs, Decimal('2.50000001'))
        self.assertEqual(usage.created_by, self.user)

    def test_product_default_drives_preview_and_production_estimate(self):
        self.client.force_login(self.user)
        self.client.post(
            reverse('master'), {'kind': 'product', 'name': 'Produk default', 'usage': '2,5'}
        )
        product = Master.objects.get(name='Produk default')
        allocation = self.allocation(('40.6', '60.4'))
        preview = self.client.get(reverse('cmt_detail', args=[self.cmt.pk]))
        default = next(u for u in preview.context['usage_preview'] if u['product_id'] == product.pk)
        self.assertIsNone(default['material_id'])
        self.assertEqual(default['usage_yard_per_pcs'], Decimal('2.5'))
        self.assertTrue(default['active'])
        self.assertTrue(
            preview.context['allocation_preview'][str(allocation.pk)][0]['material_active']
        )
        po = tautkan_po(allocation, 'PO DEFAULT', self.user, product=product)
        unit = ProductionUnit.objects.get(po=po)
        self.assertIsNone(unit.usage_source.material_id)
        self.assertEqual(unit.usage_snapshot, Decimal('2.5'))
        row = po_status(po)['rows'][0]
        self.assertEqual((row['allocated_yard'], row['estimate']), (Decimal('101'), 40))
        self.assertContains(self.client.get(reverse('po_detail', args=[po.pk])), '40 pcs')

    def test_product_default_edit_keeps_old_snapshot_and_updates_new_po(self):
        product = Master.objects.create(kind='product', name='Produk snapshot')
        save_product_usage(product, '2,5', self.user)
        po, unit = self.ready(product=product)
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('master'),
            {'kind': 'product', 'id': product.pk, 'name': 'Produk snapshot baru', 'usage': '2'},
        )
        self.assertEqual(response.status_code, 302)
        unit.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(product.name, 'Produk snapshot baru')
        self.assertEqual(unit.usage_snapshot, Decimal('2.5'))
        new_po, new_unit = self.ready(product=product)
        self.assertEqual(new_unit.usage_snapshot, Decimal('2'))
        self.assertEqual((po_status(po)['estimate'], po_status(new_po)['estimate']), (40, 50))
        audit = Log.objects.filter(aksi='ubah pemakaian paten').latest('pk')
        self.assertIn('"material_id": null', audit.detail)
        self.assertIn('"before": {', audit.detail)

    def test_invalid_product_edit_and_duplicate_name_are_atomic(self):
        save_product_usage(self.product, '2.5', self.user)
        self.client.force_login(self.user)
        for name, value in [('Nama berubah', '0'), (self.other_product.name, '3')]:
            with self.subTest(name=name):
                response = self.client.post(
                    reverse('master'),
                    {'kind': 'product', 'id': self.product.pk, 'name': name, 'usage': value},
                )
                self.assertEqual(response.status_code, 200)
                item = next(i for i in response.context['items'] if i.pk == self.product.pk)
                self.assertTrue(item.edit_form.errors)
                self.product.refresh_from_db()
                self.assertEqual(self.product.name, 'Fuji')
                self.assertEqual(
                    MaterialUsage.objects.get(
                        product=self.product, material__isnull=True
                    ).usage_yard_per_pcs,
                    Decimal('2.5'),
                )

    def test_material_specific_standard_retains_priority_and_deactivation(self):
        save_product_usage(self.product, '3', self.user)
        po, unit = self.ready()
        self.assertEqual(unit.usage_snapshot, Decimal('2'))
        self.assertEqual(po_status(po)['estimate'], 50)
        self.save_usage(self.product, self.material, '2', active=False, edit=True)
        with self.assertRaisesMessage(ValidationError, 'belum diisi di Master'):
            self.ready()

    def test_draft_product_and_legacy_setup_accept_product_default(self):
        product = Master.objects.create(kind='product', name='Produk pengganti')
        save_product_usage(product, '4', self.user)
        po, unit = self.ready()
        change_draft_product(po, product, self.user)
        unit.refresh_from_db()
        self.assertEqual(unit.usage_snapshot, Decimal('4'))
        self.assertEqual(po_status(po)['estimate'], 25)
        unit.delete()
        set_legacy_snapshots(po, product, self.user)
        self.assertEqual(po_status(po)['estimate'], 25)

    def test_material_form_excludes_default_and_rejects_default_identity(self):
        default = save_product_usage(self.product, '3', self.user)
        self.client.force_login(self.user)
        response = self.client.get(reverse('material_usage'), {'product': self.product.pk})
        self.assertEqual(len(response.context['formset'].initial), 2)
        with self.assertRaisesMessage(ValidationError, 'Identitas patokan'):
            save_material_usages(
                self.product,
                [{'id': default.pk, 'material': self.material.pk, 'usage': '4'}],
                self.user,
            )
        default.refresh_from_db()
        self.assertEqual(default.usage_yard_per_pcs, Decimal('3'))

    def test_default_is_unique_and_restricted_to_purchasing(self):
        default = save_product_usage(self.product, '2.5', self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MaterialUsage.objects.create(
                product=self.product,
                usage_yard_per_pcs='3',
                created_by=self.user,
                updated_by=self.user,
            )
        with self.assertRaises(PermissionDenied):
            save_product_usage(self.product, '3', self.director)
        self.client.force_login(self.director)
        response = self.client.get(reverse('master'), {'tab': 'product'})
        self.assertContains(response, '2,5')
        self.assertNotContains(response, 'name="usage"')
        response = self.client.post(
            reverse('master'),
            {'kind': 'product', 'id': self.product.pk, 'name': 'Fuji', 'usage': '3'},
        )
        self.assertEqual(response.status_code, 403)
        default.refresh_from_db()
        self.assertEqual(default.usage_yard_per_pcs, Decimal('2.5'))

    def test_other_master_tabs_and_product_toggle_do_not_require_usage(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('master'), {'kind': 'material', 'name': 'Bahan baru'})
        self.assertRedirects(response, reverse('master') + '?tab=material')
        self.assertNotContains(
            self.client.get(reverse('master'), {'tab': 'material'}), 'name="usage"'
        )
        response = self.client.post(
            reverse('master'), {'kind': 'product', 'id': self.product.pk, 'action': 'toggle'}
        )
        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertFalse(self.product.active)
        self.assertEqual(self.product.name, 'Fuji')

    def test_server_ignores_spoofed_estimate_status_and_snapshot(self):
        po, unit = self.ready()
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('po_detail', args=[po.pk]),
            {
                'action': 'delivery_decision',
                'unit_id': unit.pk,
                'continuation': 'final',
                'discrepancy_note': '',
                'estimate': 0,
                'usage_snapshot': 999,
                'shipped': 1000,
                'status': 'done',
            },
        )
        unit.refresh_from_db()
        self.assertEqual(unit.usage_snapshot, Decimal(2))
        self.assertIsNone(unit.finalized_at)
        self.assertContains(response, 'Keterangan Selisih wajib diisi')

    def test_bounded_list_queries(self):
        po, _ = self.ready()
        with CaptureQueriesContext(connection) as queries:
            result = po_status_many([po])
        self.assertEqual(result[po.pk]['estimate'], 50)
        self.assertLessEqual(len(queries), 10)


class MaterialUsageMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_migration_preserves_completed_and_unresolved_history_without_guessing_usage(self):
        executor = MigrationExecutor(connection)
        executor.migrate([('tracking', '0009_invoice_payments')])
        apps = executor.loader.project_state([('tracking', '0009_invoice_payments')]).apps
        OldMaster = apps.get_model('tracking', 'Master')
        user = apps.get_model('tracking', 'User').objects.create(
            username='old-buyer', role='purchasing'
        )
        material = OldMaster.objects.create(kind='material', name='Bahan lama')
        color = OldMaster.objects.create(kind='color', name='Cream')
        vendor = OldMaster.objects.create(kind='vendor', name='Vendor lama')
        cmt = OldMaster.objects.create(kind='cmt', name='CMT lama')
        invoice = apps.get_model('tracking', 'Invoice').objects.create(
            vendor=vendor,
            nomor='INV OLD',
            tanggal=timezone.localdate(),
            total_rp=1000,
            dibuat_oleh=user,
        )
        pos = []
        for i, delivered in enumerate([50, 49]):
            po = apps.get_model('tracking', 'Po').objects.create(
                nomor=f'PO MIG {i}', produk='Fuji', cmt=cmt, pemakaian_std='9.99'
            )
            allocation = apps.get_model('tracking', 'Alokasi').objects.create(
                po=po, cmt=cmt, dibuat_oleh=user, status='disetujui'
            )
            apps.get_model('tracking', 'Roll').objects.create(
                invoice=invoice,
                alokasi=allocation,
                material=material,
                color=color,
                urut=i + 1,
                yard=100,
                status='diterima',
            )
            apps.get_model('tracking', 'Hasil').objects.create(
                po=po, material=material, color=color, pcs=50
            )
            apps.get_model('tracking', 'KirimGudang').objects.create(
                po=po, material=material, color=color, tanggal=timezone.localdate(), pcs=delivered
            )
            pos.append(po.pk)
        legacy_flagged = apps.get_model('tracking', 'Po').objects.create(
            nomor='PO MANUAL DONE', selesai=True
        )
        apps.get_model('tracking', 'Log').objects.create(
            user=user, aksi='lama', objek='PO MIG', detail='Riwayat asli'
        )
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        completed, active = Po.objects.get(pk=pos[0]), Po.objects.get(pk=pos[1])
        self.assertTrue(completed.legacy_completed)
        self.assertTrue(po_status(completed)['is_done'])
        self.assertFalse(active.legacy_completed)
        self.assertIsNone(po_status(active)['estimate'])
        self.assertEqual(po_status(active)['code'], 'patokan_belum')
        self.assertEqual(completed.product_id, active.product_id)
        self.assertEqual(completed.pemakaian_std, Decimal('9.99'))
        self.assertTrue(Po.objects.get(pk=legacy_flagged.pk).legacy_completed)
        self.assertFalse(MaterialUsage.objects.exists())
        self.assertFalse(ProductionUnit.objects.exists())
        self.assertEqual(KirimGudang.objects.count(), 2)
        self.assertEqual(Log.objects.get().detail, 'Riwayat asli')
        self.assertEqual(Invoice.objects.get().total_rp, Decimal(1000))


class MaterialUsageConcurrencyTests(TransactionTestCase):
    def test_sqlite_conflict_cannot_finalize_using_a_stale_shipment_total(self):
        if connection.vendor != 'sqlite':
            self.skipTest('Specific to the SQLite write lock fallback.')
        user = User.objects.create_user(username='concurrent-patent', role='purchasing')
        product = Master.objects.create(kind='product', name='Fuji Lock')
        material = Master.objects.create(kind='material', name='Bahan Lock')
        color = Master.objects.create(kind='color', name='Cream Lock')
        cmt = Master.objects.create(kind='cmt', name='CMT Lock')
        vendor = Master.objects.create(kind='vendor', name='Vendor Lock')
        today = timezone.localdate()
        save_material_usages(product, [{'material': material, 'usage': '2'}], user)
        po = Po.objects.create(nomor='PO CONCURRENT', cmt=cmt, product=product)
        allocation = Alokasi.objects.create(po=po, cmt=cmt, dibuat_oleh=user)
        invoice = Invoice.objects.create(
            vendor=vendor, nomor='INV LOCK', tanggal=today, dibuat_oleh=user
        )
        Roll.objects.create(
            invoice=invoice, alokasi=allocation, material=material, color=color, yard=100, urut=1
        )
        set_legacy_snapshots(po, product, user)
        unit = po.production_units.get()
        result = simpan_ukuran(po, color, material, [{'label': 'All Size', 'pcs': 50}], user)
        size = result.sizes.get()
        kirim_gudang(
            po,
            color,
            {'tanggal': today, 'request_id': 'lock-first', 'sizes': [{'id': size.pk, 'pcs': 49}]},
            user,
            material=material,
        )

        def concurrent_finalize():
            close_old_connections()
            try:
                save_delivery_decision(po, unit.pk, 'final', '', user)
                return 'final'
            except OperationalError as error:
                self.assertIn('locked', str(error).lower())
                return 'conflict'
            finally:
                close_old_connections()

        with transaction.atomic():
            kirim_gudang(
                po,
                color,
                {'tanggal': today, 'request_id': 'lock-last', 'sizes': [{'id': size.pk, 'pcs': 1}]},
                user,
                material=material,
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                self.assertEqual(
                    executor.submit(concurrent_finalize).result(timeout=10), 'conflict'
                )
            unit.refresh_from_db()
            self.assertEqual(unit.continuation, 'undecided')
        save_delivery_decision(po, unit.pk, 'final', '', user)
        self.assertEqual((po_status(po)['terkirim'], po_status(po)['code']), (50, 'done'))
