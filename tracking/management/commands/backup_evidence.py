import hashlib
import json
from pathlib import Path
from django.core.management.base import BaseCommand,CommandError
from tracking.models import Evidence
from tracking import storage

class Command(BaseCommand):
    help='Salin bukti ke direktori backup, verifikasi SHA256, simpan manifest.'
    def add_arguments(self,parser):
        parser.add_argument('--output',required=True)
    def handle(self,*args,**opts):
        root=Path(opts['output']).resolve(); root.mkdir(parents=True,exist_ok=True)
        manifest=[]
        for item in Evidence.objects.order_by('pk').iterator():
            target=root/item.storage_key
            if target.parent!=root: raise CommandError('Storage key tidak valid.')
            with storage.read(item) as source: content=source.read()
            if hashlib.sha256(content).hexdigest()!=item.sha256: raise CommandError(f'Checksum bukti #{item.pk} tidak cocok.')
            target.write_bytes(content)
            manifest.append({'id':item.pk,'key':item.storage_key,'sha256':item.sha256,'name':item.name,'size':item.size,'backend':item.backend})
        (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        self.stdout.write(f'{len(manifest)} bukti disalin dan diverifikasi.')
