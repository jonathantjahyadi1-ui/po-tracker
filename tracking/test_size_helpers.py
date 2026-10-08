"""Explicit All Size fixtures for pre-size workflow regression scenarios.

Only tests choose this size: migration and application code never infer a size.
"""

from uuid import uuid4

from .models import Hasil
from .services import production_material, require_purchasing, simpan_hasil
from .size_services import kirim_ukuran, simpan_ukuran


def record_single_size(po, color, pcs, user, material=None):
    require_purchasing(user)
    material = production_material(po, color, material)
    result = Hasil.objects.filter(po=po, material=material, color=color).first()
    if result and not result.sizes_complete:
        # Hand-created historical fixtures retain their unresolved size state.
        return simpan_hasil(po, color, pcs, user, material=material)
    size = result.sizes.get() if result else None
    return simpan_ukuran(
        po,
        color,
        material,
        [
            {
                'id': size.pk if size else None,
                'label': 'All Size',
                'pcs': pcs,
            }
        ],
        user,
        result.pk if result else None,
    )


def ship_single_size(po, color, data, user, material=None):
    require_purchasing(user)
    material = production_material(po, color, material or data.get('material'))
    result = Hasil.objects.filter(po=po, material=material, color=color).first()
    size = result.sizes.first() if result else None
    payload = dict(data)
    payload.setdefault('request_id', uuid4().hex)
    return kirim_ukuran(
        po,
        color,
        material,
        payload,
        [
            {
                'id': size.pk if size else None,
                'pcs': payload.get('pcs'),
            }
        ],
        user,
    )
