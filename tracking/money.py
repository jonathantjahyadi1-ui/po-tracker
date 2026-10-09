import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

CENT = Decimal('0.01')
MAX_MONEY = Decimal('99999999999999.99')


def parse_money(value):
    """Accept Indonesian grouped amounts and ungrouped machine decimal amounts."""
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        amount = Decimal(value)
    elif isinstance(value, str):
        raw = value.strip()
        if re.fullmatch(r'\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?', raw):
            raw = raw.replace('.', '').replace(',', '.')
        elif re.fullmatch(r'\d+(?:[.,]\d{1,2})?', raw):
            raw = raw.replace(',', '.')
        else:
            raise ValidationError('Isi nominal rupiah yang valid, misalnya 500.000.000.')
        amount = Decimal(raw)
    else:
        raise ValidationError('Nominal rupiah tidak valid.')
    if not amount.is_finite() or amount < 0 or amount > MAX_MONEY:
        raise ValidationError('Nominal rupiah di luar batas yang dapat disimpan.')
    try:
        normalized = amount.quantize(CENT)
    except InvalidOperation:
        raise ValidationError('Nominal rupiah tidak valid.')
    if amount != normalized:
        raise ValidationError('Nominal rupiah maksimal dua angka desimal.')
    return normalized


def format_money(value):
    return f'{value:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')
