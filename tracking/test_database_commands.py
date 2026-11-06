import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.query import QuerySet
from django.test import TestCase, override_settings
from django.utils import timezone

from .management.commands.reset_trial_data import PRESERVED_MODELS, TRANSACTION_MODELS, snapshot
from .models import (
    FabricDefect,
    FabricDefectItem,
    Invoice,
    InvoiceAttachment,
    InvoicePayment,
    Master,
    Po,
    User,
    VendorRefund,
)


@override_settings(DEBUG=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ResetTrialDataTests(TestCase):
    def setUp(self):
        call_command('seed_contoh', stdout=StringIO())
        self.user = User.objects.get(username='contoh_purchasing')
        self.user.groups.add(Group.objects.create(name='Purchasing test'))
        self.user.user_permissions.add(Permission.objects.first())
        self.invoice = Invoice.objects.get(nomor='INV-CONTOH')
        InvoiceAttachment.objects.create(
            invoice=self.invoice,
            filename='invoice.pdf',
            content_type='application/pdf',
            size=5,
            content=b'%PDF-',
        )
        InvoicePayment.objects.create(
            invoice=self.invoice,
            amount=100,
            payment_date=timezone.localdate(),
            kind='cicil',
            recorded_by=self.user,
            request_id='payment-reset-test',
            fingerprint='payment-fingerprint',
            proof_filename='proof.png',
            proof_content_type='image/png',
            proof_size=3,
            proof_content=b'png',
        )
        defect = FabricDefect.objects.create(
            number='BS-RESET-TEST',
            date=timezone.localdate(),
            cmt=Master.objects.get(kind='cmt'),
            note='Trial',
            recorded_by=self.user,
            request_id='defect-reset-test',
            fingerprint='defect-fingerprint',
        )
        FabricDefectItem.objects.create(
            defect=defect,
            roll=self.invoice.roll_set.first(),
            invoice=self.invoice,
            vendor=self.invoice.vendor,
            po=Po.objects.first(),
            yard=1,
            price=100,
            amount=100,
        )
        VendorRefund.objects.create(
            invoice=self.invoice,
            amount=100,
            date=timezone.localdate(),
            recorded_by=self.user,
            request_id='refund-reset-test',
            fingerprint='refund-fingerprint',
        )
        self.all_models = tuple(apps.get_app_config('tracking').get_models())

    def test_preview_does_not_change_any_data(self):
        before = snapshot(self.all_models)
        output = StringIO()
        call_command('reset_trial_data', stdout=output)
        self.assertIn('Pratinjau saja', output.getvalue())
        self.assertEqual(snapshot(self.all_models), before)

    def test_apply_requires_backup(self):
        before = snapshot(self.all_models)
        with self.assertRaisesMessage(CommandError, '--backup wajib diisi'):
            call_command('reset_trial_data', apply=True, stdout=StringIO())
        self.assertEqual(snapshot(self.all_models), before)

    def test_backup_failure_aborts_before_any_deletion(self):
        before = snapshot(self.all_models)
        with TemporaryDirectory() as folder:
            backup = Path(folder) / 'existing.json'
            backup.write_text('existing backup', encoding='utf-8')
            with self.assertRaisesMessage(CommandError, 'Backup gagal'):
                call_command('reset_trial_data', apply=True, backup=backup, stdout=StringIO())
            self.assertEqual(backup.read_text(encoding='utf-8'), 'existing backup')
        self.assertEqual(snapshot(self.all_models), before)

    def test_reset_preserves_accounts_masters_and_standards_and_backup_restores_all_data(self):
        before = snapshot(self.all_models)
        preserved = snapshot(PRESERVED_MODELS)
        with TemporaryDirectory() as folder:
            backup = Path(folder) / 'before-reset.json'
            call_command('reset_trial_data', apply=True, backup=backup, stdout=StringIO())
            self.assertEqual(backup.read_text(encoding='utf-8'), before)
            self.assertTrue(all(not model.objects.exists() for model in TRANSACTION_MODELS))
            self.assertEqual(snapshot(PRESERVED_MODELS), preserved)
            self.user.refresh_from_db()
            self.assertTrue(self.user.check_password('contoh123'))
            self.assertEqual(self.user.groups.count(), 1)
            self.assertEqual(self.user.user_permissions.count(), 1)
            call_command('loaddata', str(backup), stdout=StringIO())
            self.assertEqual(json.loads(snapshot(self.all_models)), json.loads(before))

    def test_failure_midway_rolls_back_all_deletions(self):
        before = snapshot(self.all_models)
        original_delete = QuerySet.delete

        def fail_on_po(queryset):
            if queryset.model is Po:
                raise RuntimeError('Reset failure test')
            return original_delete(queryset)

        with TemporaryDirectory() as folder:
            with patch.object(QuerySet, 'delete', fail_on_po):
                with self.assertRaisesMessage(RuntimeError, 'Reset failure test'):
                    call_command(
                        'reset_trial_data',
                        apply=True,
                        backup=Path(folder) / 'before-reset.json',
                        stdout=StringIO(),
                    )
        self.assertEqual(snapshot(self.all_models), before)

    def test_status_reports_active_storage_without_credentials(self):
        output = StringIO()
        call_command('database_status', stdout=output)
        self.assertIn('User: 1', output.getvalue())
        self.assertIn('Po: 1', output.getvalue())
        self.assertNotIn('postgresql://', output.getvalue())
