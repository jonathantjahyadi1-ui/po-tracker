import hashlib
import json
from pathlib import Path
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import BaseCommand,CommandError
from django.db import transaction
from django.db.migrations.recorder import MigrationRecorder

class Command(BaseCommand):
    help='Pulihkan backup JSON gzip hanya ke database aplikasi kosong dengan migration yang sama.'
    def add_arguments(self,parser):
        parser.add_argument('--input',required=True)
        parser.add_argument('--confirm-empty-database',action='store_true')
    def handle(self,*args,**opts):
        if not opts['confirm_empty_database']:
            raise CommandError('Gunakan --confirm-empty-database setelah memeriksa database tujuan.')
        source=Path(opts['input']).resolve()
        try:
            manifest=json.loads(source.with_suffix('.manifest.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):
            raise CommandError('Manifest backup tidak dapat dibaca.')
        with source.open('rb') as backup:
            digest=hashlib.file_digest(backup,'sha256').hexdigest()
        if not manifest.get('complete') or digest!=manifest.get('sha256'):
            raise CommandError('Backup belum lengkap atau checksum berbeda.')
        current=[list(x) for x in MigrationRecorder.Migration.objects.order_by('app','name').values_list('app','name')]
        if current!=manifest.get('migrations'):
            raise CommandError('Versi migration berbeda. Gunakan versi kode yang sama dengan backup sebelum restore.')
        with transaction.atomic():
            for model in apps.get_app_config('tracking').get_models():
                if model.objects.exists():
                    raise CommandError('Database aplikasi tujuan tidak kosong. Restore dibatalkan.')
            call_command('loaddata',str(source),verbosity=0)
            call_command('verify_ledger')
        self.stdout.write('Data dipulihkan. Pulihkan lampiran dan verifikasi akun sebelum layanan dibuka.')
