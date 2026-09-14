from django.db import migrations, models
from django.utils import timezone


def migrate_workflow(apps, schema_editor):
    User = apps.get_model('tracking', 'User')
    Allocation = apps.get_model('tracking', 'Allocation')
    Audit = apps.get_model('tracking', 'Audit')
    alias = schema_editor.connection.alias
    role_map = {'approver': 'purchasing', 'management': 'director'}
    status_map = {'approved': 'allocated', 'pending': 'draft', 'revision': 'draft'}

    for user in User.objects.using(alias).select_for_update().all().iterator():
        role = role_map.get(user.role, user.role)
        before = {'role': user.role, 'can_adjust': user.can_adjust, 'can_reopen': user.can_reopen}
        after = {'role': role, 'can_adjust': role == 'purchasing', 'can_reopen': role == 'purchasing'}
        if before == after:
            continue
        User.objects.using(alias).filter(pk=user.pk).update(**after)
        Audit.objects.using(alias).create(
            actor_id=None, role='system', action='workflow_migration', entity='User',
            object_id=str(user.pk), before=before, after=after, request_id='',
        )

    # Existing reservations remain unchanged. Pending allocations never reserved
    # stock, so they return to draft and must pass the new stock check when posted.
    for allocation in Allocation.objects.using(alias).select_for_update().filter(status__in=status_map).iterator():
        status = status_map[allocation.status]
        Allocation.objects.using(alias).filter(pk=allocation.pk).update(
            status=status, version=allocation.version + 1, updated_at=timezone.now(),
        )
        Audit.objects.using(alias).create(
            actor_id=None, role='system', action='workflow_migration', entity='Allocation',
            object_id=str(allocation.pk),
            before={'status': allocation.status, 'version': allocation.version},
            after={'status': status, 'version': allocation.version + 1}, request_id='',
        )


class Migration(migrations.Migration):
    dependencies = [
        ('tracking', '0004_remove_warehousereceipt_warehouse_received_positive_and_more'),
    ]
    operations = [
        migrations.RunPython(migrate_workflow),
        migrations.AlterField(
            model_name='user', name='role',
            field=models.CharField(
                choices=[('purchasing', 'Purchasing'), ('director', 'Direktur'), ('admin', 'Super Admin')],
                default='director', max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name='user',
            constraint=models.CheckConstraint(
                condition=models.Q(role__in=['purchasing', 'director', 'admin']),
                name='valid_user_role',
            ),
        ),
        migrations.AddConstraint(
            model_name='allocation',
            constraint=models.CheckConstraint(
                condition=models.Q(status__in=['draft', 'allocated', 'partially_shipped', 'fully_shipped', 'cancelled', 'rejected']),
                name='valid_allocation_status',
            ),
        ),
    ]
