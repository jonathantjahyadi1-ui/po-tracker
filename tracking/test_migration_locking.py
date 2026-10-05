from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from importlib import import_module

from django.db import OperationalError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from .models import (
    Alokasi,
    Hasil,
    Invoice,
    InvoiceAttachment,
    KirimGudang,
    Log,
    Master,
    Po,
    Roll,
    User,
)


class MigrationSourceLockTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Kunci tabel migrasi harus diuji pada PostgreSQL.')
        self.user = User.objects.create_user(username='migration-lock-buyer', role='purchasing')
        vendor = Master.objects.create(kind='vendor', name='Migration lock vendor')
        material = Master.objects.create(kind='material', name='Migration lock material')
        color = Master.objects.create(kind='color', name='Migration lock color')
        invoice = Invoice.objects.create(
            vendor=vendor,
            nomor='INV-MIGRATION-LOCK',
            tanggal=timezone.localdate(),
            dibuat_oleh=self.user,
        )
        self.roll = Roll.objects.create(
            invoice=invoice, material=material, color=color, urut=1, yard=Decimal(100)
        )
        self.sources = {
            model.__name__: model
            for model in (Invoice, Roll, Po, Alokasi, Hasil, KirimGudang, InvoiceAttachment, Log)
        }

    def worker(self, callback):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '500ms'")
                cursor.execute("SET statement_timeout = '2s'")
            with transaction.atomic():
                return callback()
        except OperationalError as error:
            return getattr(error.__cause__, 'sqlstate', None)
        finally:
            connection.close()

    def test_snapshot_table_lock_allows_reads_and_blocks_mutation_until_commit(self):
        migration = import_module('tracking.migrations.0005_migrate_material_history')
        before = {name: model.objects.count() for name, model in self.sources.items()}
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                migration.lock_source_tables(self.sources, connection)
                read = pool.submit(
                    self.worker,
                    lambda: {name: model.objects.count() for name, model in self.sources.items()},
                )
                self.assertEqual(read.result(timeout=3), before)
                for write in (
                    lambda: Log.objects.create(
                        user=self.user, aksi='probe', objek='migration lock'
                    ),
                    lambda: Roll.objects.filter(pk=self.roll.pk).update(yard=Decimal(50)),
                    lambda: Roll.objects.filter(pk=self.roll.pk).delete(),
                    lambda: Roll.objects.select_for_update().get(pk=self.roll.pk),
                ):
                    result = pool.submit(self.worker, write).result(timeout=3)
                    self.assertEqual(result, '55P03', 'Mutation must wait for the migration lock.')
            # Releasing the migration transaction makes ordinary writes available again.
            result = pool.submit(
                self.worker,
                lambda: Roll.objects.filter(pk=self.roll.pk).update(yard=Decimal(75)),
            ).result(timeout=3)
        self.assertEqual(result, 1)
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.yard, Decimal(75))
        self.assertEqual(
            before, {name: model.objects.count() for name, model in self.sources.items()}
        )
