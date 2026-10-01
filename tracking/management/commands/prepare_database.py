from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = 'Siapkan schema PostgreSQL untuk versi Severli saat ini.'

    def handle(self, *args, **options):
        schema = settings.DATABASE_SCHEMA
        if connection.vendor != 'postgresql' or not schema:
            return
        if schema == settings.LEGACY_USERS_SCHEMA:
            raise CommandError('Schema baru harus berbeda dari schema akun lama.')

        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT 1 FROM information_schema.schemata WHERE schema_name = %s',
                [schema],
            )
            if cursor.fetchone() is None:
                cursor.execute(f'CREATE SCHEMA {connection.ops.quote_name(schema)}')

            cursor.execute(
                """SELECT 1 FROM information_schema.tables
                   WHERE table_schema = %s AND table_name = 'tracking_user'""",
                [schema],
            )
            if cursor.fetchone() is not None:
                cursor.execute(
                    """SELECT 1 FROM information_schema.columns
                       WHERE table_schema = %s AND table_name = 'tracking_user'
                         AND column_name = 'created_at'""",
                    [schema],
                )
                if cursor.fetchone() is None:
                    raise CommandError(
                        f'Schema {schema} masih berisi tabel User versi lama. '
                        'Pilih schema baru tanpa menghapus data lama.'
                    )
        self.stdout.write(f'Schema {schema} siap.')
