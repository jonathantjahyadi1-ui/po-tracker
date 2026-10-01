from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


class Command(BaseCommand):
    help = 'Salin akun dari schema lama tanpa mengubah akun atau data lama.'

    def handle(self, *args, **options):
        source = settings.LEGACY_USERS_SCHEMA
        if not source:
            return
        target = settings.DATABASE_SCHEMA
        if connection.vendor != 'postgresql' or not target or source == target:
            raise CommandError('Schema sumber dan tujuan PostgreSQL harus berbeda.')

        quote = connection.ops.quote_name
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute('SELECT to_regclass(%s)', [f'{source}.tracking_user'])
            if cursor.fetchone()[0] is None:
                raise CommandError(f'Tabel akun lama tidak ditemukan di schema {source}.')
            cursor.execute('SELECT to_regclass(%s)', [f'{target}.tracking_user'])
            if cursor.fetchone()[0] is None:
                raise CommandError(f'Migrasi schema {target} belum dijalankan.')

            cursor.execute(
                f"""INSERT INTO {quote(target)}.tracking_user
                    (password, last_login, is_superuser, username, first_name,
                     last_name, email, is_staff, is_active, date_joined, role, created_at)
                    SELECT password, last_login, is_superuser, username, first_name,
                           last_name, email, is_staff, is_active, date_joined,
                           CASE WHEN is_superuser OR role = 'admin' THEN 'admin'
                                WHEN role = 'purchasing' THEN 'purchasing'
                                ELSE 'direktur' END,
                           date_joined
                    FROM {quote(source)}.tracking_user
                    WHERE true
                    ON CONFLICT (username) DO NOTHING"""
            )
            imported = cursor.rowcount
        self.stdout.write(f'{imported} akun lama disalin ke schema {target}.')
