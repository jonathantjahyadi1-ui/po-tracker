from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def indo(value):
    if value is None:
        return '0,00'
    text = f'{Decimal(value):,.2f}'
    return text.replace(',', '_').replace('.', ',').replace('_', '.')


@register.filter
def get(mapping, key):
    return mapping.get(key, '') if mapping else ''


@register.filter
def precise(value):
    if value is None:
        return '—'
    return (
        format(Decimal(value), 'f').rstrip('0').rstrip('.').replace('.', ',')
        if Decimal(value) % 1
        else str(int(Decimal(value)))
    )
