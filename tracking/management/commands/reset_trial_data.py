import hashlib
import json
import os
from collections import Counter
from itertools import chain
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from tracking import models

# Children first. PROTECT and database foreign keys remain enabled throughout.
TRANSACTION_MODELS = (
    models.VendorRefund,
    models.FabricDefectItem,
    models.FabricDefect,
    models.InvoicePayment,
    models.InvoiceAttachment,
    models.InvoiceWrite,
    models.KirimUkuran,
    models.KirimGudang,
    models.HasilUkuran,
    models.Hasil,
    models.ProductionUnit,
    models.AlokasiRoll,
    models.Roll,
    models.InvoiceColor,
    models.InvoiceMaterial,
    models.InvoicePo,
    models.Alokasi,
    models.Po,
    models.Invoice,
    models.Log,
)
PRESERVED_MODELS = (models.User, models.Master, models.MaterialUsage)


def snapshot(model_list):
    return serializers.serialize(
        'json',
        chain.from_iterable(model.objects.order_by('pk').iterator() for model in model_list),
    )


class Command(BaseCommand):
    help = (
        'Reset seluruh transaksi percobaan (termasuk invoice, roll, pembayaran, BS, '
        'dan kiriman). Akun, semua Master, dan patokan pemakaian dipertahankan. '
        'Default hanya menampilkan rencana.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Jalankan reset setelah backup.')
        parser.add_argument(
            '--backup', type=Path, help='File JSON backup baru; wajib untuk --apply.'
        )

    @transaction.atomic
    def handle(self, *args, **options):
        all_models = tuple(apps.get_app_config('tracking').get_models())
        if set(all_models) != set(TRANSACTION_MODELS + PRESERVED_MODELS):
            raise CommandError('Daftar model berubah. Tinjau cakupan reset sebelum melanjutkan.')
        if options['apply'] and not options['backup']:
            raise CommandError('--backup wajib diisi. Reset dibatalkan tanpa mengubah data.')

        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT current_schema()')
                schema = cursor.fetchone()[0]
                if schema != settings.DATABASE_SCHEMA:
                    raise CommandError('Schema aktif berbeda dari DATABASE_SCHEMA.')
                self.stdout.write(f'Target: PostgreSQL / {schema}')
                if options['apply']:
                    cursor.execute("SET LOCAL lock_timeout = '10s'")
                    tables = sorted(
                        model._meta.db_table
                        for model in apps.get_app_config('tracking').get_models(
                            include_auto_created=True
                        )
                    )
                    quoted = ', '.join(connection.ops.quote_name(table) for table in tables)
                    cursor.execute(f'LOCK TABLE {quoted} IN EXCLUSIVE MODE')
        elif connection.vendor == 'sqlite':
            self.stdout.write(f'Target: SQLite / {connection.settings_dict["NAME"]}')
            if options['apply']:
                # Reserve the SQLite writer before reading the backup snapshot.
                table = connection.ops.quote_name(models.Po._meta.db_table)
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE {table} SET id = id WHERE 1 = 0')
        else:
            raise CommandError('Reset hanya mendukung PostgreSQL dan SQLite.')

        counts = {model._meta.label_lower: model.objects.count() for model in all_models}
        for model in TRANSACTION_MODELS:
            self.stdout.write(f'Hapus {model.__name__}: {counts[model._meta.label_lower]}')
        for model in PRESERVED_MODELS:
            self.stdout.write(f'Pertahankan {model.__name__}: {counts[model._meta.label_lower]}')
        if not options['apply']:
            self.stdout.write('Pratinjau saja. Tidak ada data yang diubah.')
            return

        preserved = snapshot(PRESERVED_MODELS)
        backup = snapshot(all_models)
        actual = Counter(row['model'] for row in json.loads(backup))
        if any(actual[label] != count for label, count in counts.items()):
            raise CommandError('Jumlah baris backup berbeda. Reset dibatalkan.')
        backup_path = options['backup'].resolve()
        try:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            with backup_path.open('x', encoding='utf-8') as stream:
                stream.write(backup)
                stream.flush()
                os.fsync(stream.fileno())
            if backup_path.read_text(encoding='utf-8') != backup:
                raise OSError('Isi backup tidak cocok.')
        except OSError as error:
            raise CommandError(f'Backup gagal; tidak ada data dihapus: {error}') from error

        for model in TRANSACTION_MODELS:
            model.objects.all().delete()
        if snapshot(PRESERVED_MODELS) != preserved:
            raise CommandError('Data yang dipertahankan berubah. Seluruh reset dibatalkan.')
        if any(model.objects.exists() for model in TRANSACTION_MODELS):
            raise CommandError('Masih ada transaksi tersisa. Seluruh reset dibatalkan.')
        connection.check_constraints()
        digest = hashlib.sha256(backup.encode('utf-8')).hexdigest()
        self.stdout.write(f'Backup: {backup_path}')
        self.stdout.write(f'SHA256: {digest}')
        self.stdout.write(self.style.SUCCESS('Reset selesai. Akun dan semua Master tetap identik.'))
