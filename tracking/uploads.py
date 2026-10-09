from django.core.exceptions import ValidationError


def validate_document(upload, label, max_mb):
    """Apply the existing invoice signature checks to every document entry point."""
    if not upload:
        raise ValidationError(f'{label} wajib diunggah.')
    if not upload.size or upload.size > max_mb * 1024 * 1024:
        raise ValidationError(f'{label} harus berisi data, maksimal {max_mb} MB.')
    try:
        upload.seek(0)
        header = upload.read(12)
        upload.seek(0)
    except (OSError, ValueError):
        raise ValidationError(f'{label} tidak berhasil dibaca. Unggah ulang berkas.')
    name = upload.name.lower()
    valid = (
        (name.endswith('.pdf') and header.startswith(b'%PDF-'), 'application/pdf'),
        (name.endswith(('.jpg', '.jpeg')) and header.startswith(b'\xff\xd8\xff'), 'image/jpeg'),
        (name.endswith('.png') and header.startswith(b'\x89PNG\r\n\x1a\n'), 'image/png'),
    )
    mime = next((mime for accepted, mime in valid if accepted), None)
    if not mime:
        raise ValidationError(f'{label}: unggah PDF, JPG, atau PNG yang valid.')
    upload.verified_content_type = mime
    return upload
