from django import template
from ..presentation import STATUS,number,url,ACTION_LABELS,ENTITY_LABELS
register=template.Library()
register.filter('status_label',lambda v:STATUS.get(v,v))
register.filter('quantity',lambda v:number(v,2))
register.filter('integer',lambda v:number(v))
register.filter('object_url',url)
register.filter('action_label',lambda v:ACTION_LABELS.get(v,v))
register.filter('entity_label',lambda v:ENTITY_LABELS.get(v,v))
