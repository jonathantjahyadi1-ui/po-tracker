import requests
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from django.db import connection

class Command(BaseCommand):
    help='Periksa PostgreSQL dan bucket Supabase tanpa menulis data bisnis.'
    def handle(self,*args,**kwargs):
        with connection.cursor() as cur:
            if connection.vendor=='postgresql':
                cur.execute('SELECT current_schema()')
                if cur.fetchone()[0]!='po_tracking': raise CommandError('Schema database bukan po_tracking.')
                cur.execute("SELECT COUNT(*) FROM pg_trigger WHERE tgname='immutable_history' AND NOT tgisinternal")
                if cur.fetchone()[0]<3: raise CommandError('Trigger ledger/audit belum terpasang.')
                self.stdout.write('PostgreSQL: schema dan proteksi riwayat valid.')
            else:
                self.stdout.write('SQLite lokal. Koneksi Supabase belum diuji.')
        if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
            raise CommandError('Konfigurasi Supabase Storage belum lengkap.')
        try:
            response=requests.get(f'{settings.SUPABASE_URL}/storage/v1/bucket/{settings.SUPABASE_STORAGE_BUCKET}',headers={'Authorization':'Bearer '+settings.SUPABASE_SERVICE_ROLE_KEY,'apikey':settings.SUPABASE_SERVICE_ROLE_KEY},timeout=20)
            response.raise_for_status(); bucket=response.json()
        except (requests.RequestException,ValueError):
            raise CommandError('Bucket belum dapat diperiksa. Periksa URL, key, dan nama bucket.')
        if bucket.get('public'): raise CommandError('Bucket lampiran harus privat.')
        self.stdout.write('Supabase Storage: bucket ditemukan dan privat.')
