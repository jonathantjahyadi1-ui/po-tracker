from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from tracking.models import Alokasi, Invoice, Master, Po, Roll, User


class Command(BaseCommand):
    help = 'Periksa database aktif dan jumlah data tanpa menampilkan password atau URL koneksi.'

    def handle(self, *args, **options):
        self.stdout.write(f'Backend: {connection.vendor}')
        self.stdout.write(f'Sumber konfigurasi: {settings.DATABASE_URL_SOURCE}')
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT current_database(), current_schema()')
                name, schema = cursor.fetchone()
            self.stdout.write(f'Database: {name}; schema: {schema}')
            if schema != settings.DATABASE_SCHEMA:
                raise CommandError('Schema aktif berbeda dari DATABASE_SCHEMA atau belum dibuat.')
        else:
            self.stdout.write(f'File: {connection.settings_dict["NAME"]}')
            self.stdout.write('Data di SQLite lokal tidak disinkronkan ke Supabase.')
        for model in (User, Master, Po, Invoice, Roll, Alokasi):
            self.stdout.write(f'{model.__name__}: {model.objects.count()}')
