import hashlib
import io
import uuid
from pathlib import Path
from urllib.parse import quote
import requests
from PIL import Image, UnidentifiedImageError
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from .models import Evidence

def upload(actor, file):
    if not file:
        return None
    if file.size > 10 * 1024 * 1024:
        raise ValidationError('Lampiran maksimal 10 MB.')
    content = file.read()
    extension = Path(file.name).suffix.lower()
    if extension == '.pdf' and content.startswith(b'%PDF-'):
        mime = 'application/pdf'
    elif extension in ['.jpg','.jpeg','.png']:
        try:
            with Image.open(io.BytesIO(content)) as img:
                if img.format not in ['JPEG','PNG'] or img.width * img.height > 25000000:
                    raise ValueError()
                expected = 'PNG' if extension == '.png' else 'JPEG'
                if img.format != expected:
                    raise ValueError()
                img.verify()
            mime = 'image/png' if extension == '.png' else 'image/jpeg'
        except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError):
            raise ValidationError('Lampiran gambar tidak valid atau terlalu besar dimensinya.')
    else:
        raise ValidationError('Gunakan PDF, JPG, atau PNG dengan isi file yang sesuai.')
    key = f'{uuid.uuid4().hex}{extension}'
    if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY:
        endpoint = f'{settings.SUPABASE_URL}/storage/v1/object/{quote(settings.SUPABASE_STORAGE_BUCKET)}/{key}'
        try:
            response = requests.post(endpoint, headers={'Authorization':'Bearer '+settings.SUPABASE_SERVICE_ROLE_KEY,'apikey':settings.SUPABASE_SERVICE_ROLE_KEY,'Content-Type':mime,'x-upsert':'false'}, data=content, timeout=30)
            response.raise_for_status()
        except requests.RequestException:
            raise ValidationError('Bukti gagal diunggah. Data form tetap tersedia; coba simpan kembali.')
        backend = 'supabase'
    elif settings.DEBUG or settings.TESTING:
        from django.core.files.base import ContentFile
        FileSystemStorage(location=settings.MEDIA_ROOT).save(key,ContentFile(content))
        backend = 'local'
    else:
        raise ValidationError('Penyimpanan lampiran Supabase belum dikonfigurasi.')
    return Evidence.objects.create(created_by=actor,name=Path(file.name).name[:240],mime_type=mime,size=len(content),storage_key=key,sha256=hashlib.sha256(content).hexdigest(),backend=backend)

def read(evidence):
    if evidence.backend == 'local':
        return FileSystemStorage(location=settings.MEDIA_ROOT).open(evidence.storage_key,'rb')
    try:
        response = requests.get(f'{settings.SUPABASE_URL}/storage/v1/object/authenticated/{quote(settings.SUPABASE_STORAGE_BUCKET)}/{quote(evidence.storage_key)}', headers={'Authorization':'Bearer '+settings.SUPABASE_SERVICE_ROLE_KEY,'apikey':settings.SUPABASE_SERVICE_ROLE_KEY},timeout=30)
        response.raise_for_status()
        return io.BytesIO(response.content)
    except requests.RequestException:
        raise ValidationError('Bukti belum dapat diunduh. Coba kembali.')
