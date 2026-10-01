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
