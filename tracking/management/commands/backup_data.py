import gzip
import hashlib
import json
from pathlib import Path
from django.core.management import call_command
from django.core.management.base import BaseCommand,CommandError
from django.db import connection,transaction
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

class Command(BaseCommand):
    help='Backup data aplikasi konsisten dalam JSON gzip; migration disimpan bersama kode.'
    def add_arguments(self,parser):
        parser.add_argument('--output',required=True)
    def handle(self,*args,**opts):
        target=Path(opts['output']).resolve()
        if not target.name.endswith('.json.gz'):
            raise CommandError('Nama backup harus berakhiran .json.gz.')
        if target.exists() or target.with_suffix('.manifest.json').exists():
            raise CommandError('File backup sudah ada. Gunakan nama baru.')
        target.parent.mkdir(parents=True,exist_ok=True)
        with transaction.atomic():
            if connection.vendor=='postgresql':
                with connection.cursor() as cursor:
                    cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            migrations=list(MigrationRecorder.Migration.objects.order_by('app','name').values_list('app','name'))
            with gzip.open(target,'xt',encoding='utf-8') as output:
                call_command('dumpdata',all=True,format='json',stdout=output,verbosity=0)
        with target.open('rb') as source:
            digest=hashlib.file_digest(source,'sha256').hexdigest()
        manifest={'created_at':timezone.now().isoformat(),'database_engine':connection.vendor,'filename':target.name,'sha256':digest,'migrations':migrations,'complete':True}
        target.with_suffix('.manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        self.stdout.write(f'Backup selesai: {target.name}. Manifest dan SHA256 tersimpan.')
