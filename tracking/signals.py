from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from .middleware import request_id
from .models import Audit
from django.db.models.signals import pre_migrate
from django.db import connections

@receiver(pre_migrate)
def private_test_schema(sender,using,**kwargs):
    # Test databases are created by Django; match the production schema layout.
    from django.conf import settings
    connection=connections[using]
    if settings.TESTING and connection.vendor=='postgresql':
        with connection.cursor() as cur:
            cur.execute('CREATE SCHEMA IF NOT EXISTS po_tracking')
            cur.execute('REVOKE ALL ON SCHEMA po_tracking FROM PUBLIC')

@receiver(user_logged_in)
def login_audit(sender, request, user, **kwargs):
    Audit.objects.create(actor=user, role=user.role, action='login', entity='User', object_id=str(user.pk), request_id=request_id.get())

@receiver(user_logged_out)
def logout_audit(sender, request, user, **kwargs):
    if user:
        Audit.objects.create(actor=user, role=user.role, action='logout', entity='User', object_id=str(user.pk), request_id=request_id.get())
