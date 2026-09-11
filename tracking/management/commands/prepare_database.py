from django.core.management.base import BaseCommand
from django.db import connection

class Command(BaseCommand):
    help='Buat schema PostgreSQL privat sebelum migrate.'
    def handle(self,*args,**kwargs):
        if connection.vendor!='postgresql':
            self.stdout.write('SQLite lokal: tidak memerlukan schema.'); return
        with connection.cursor() as cur:
            cur.execute('CREATE SCHEMA IF NOT EXISTS po_tracking')
            cur.execute('REVOKE ALL ON SCHEMA po_tracking FROM PUBLIC')
            for role in ['anon','authenticated']:
                cur.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',[role])
                if cur.fetchone():
                    cur.execute(f'REVOKE ALL ON SCHEMA po_tracking FROM {role}')
            cur.execute('SELECT current_schema()')
            if cur.fetchone()[0]!='po_tracking':
                raise RuntimeError('Schema aktif bukan po_tracking. Periksa search_path dan izin role.')
        self.stdout.write('Schema po_tracking siap dan tidak dapat diakses role API publik.')
