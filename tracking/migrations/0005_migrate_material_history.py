from django.db import migrations
from django.db.models import Sum


def lock_source_tables(models, connection):
    if connection.vendor != 'postgresql':
        return
    tables = sorted({model._meta.db_table for model in models.values()})
    quoted_tables = ', '.join(connection.ops.quote_name(table) for table in tables)
    # The migration is atomic. EXCLUSIVE permits ordinary SELECTs, while blocking
    # writes and SELECT FOR UPDATE until conservation checks and backfill commit.
    # Blocking row-locking readers also prevents them holding rows the copy needs.
    # Use the connection's configured schema/search_path and one stable table order.
    with connection.cursor() as cursor:
        cursor.execute(f'LOCK TABLE {quoted_tables} IN EXCLUSIVE MODE')


def migrate_history(apps, schema_editor):
    alias = schema_editor.connection.alias
    models = {
        name: apps.get_model('tracking', name)
        for name in (
            'Invoice',
            'Roll',
            'Po',
            'Alokasi',
            'Hasil',
            'KirimGudang',
            'InvoiceAttachment',
            'Log',
        )
    }
    InvoiceMaterial = apps.get_model('tracking', 'InvoiceMaterial')
    InvoiceColor = apps.get_model('tracking', 'InvoiceColor')
    AlokasiRoll = apps.get_model('tracking', 'AlokasiRoll')
    lock_source_tables(models, schema_editor.connection)

    def totals():
        result = {name: model.objects.using(alias).count() for name, model in models.items()}
        for name, field in (('Roll', 'yard'), ('Hasil', 'pcs'), ('KirimGudang', 'pcs')):
            result[f'{name}_{field}'] = (
                models[name].objects.using(alias).aggregate(value=Sum(field))['value']
            )
        result['attachments_linked'] = (
            models['InvoiceAttachment'].objects.using(alias).exclude(invoice_id=None).count()
        )
        return result

    before = totals()
    # Group actual roll details, not the old PO label: legacy overrides may mix materials.
    panel_cache, row_cache, next_panel, next_row = {}, {}, {}, {}
    pending = []
    rolls = (
        models['Roll']
        .objects.using(alias)
        .filter(invoice_color_id=None)
        .order_by('invoice_id', 'id')
    )
    for roll in rolls.iterator(chunk_size=1000):
        panel_key = (roll.invoice_id, roll.material_id)
        if panel_key not in panel_cache:
            existing = (
                InvoiceMaterial.objects.using(alias)
                .filter(invoice_id=roll.invoice_id, material_id=roll.material_id)
                .first()
            )
            if existing is None:
                if roll.invoice_id not in next_panel:
                    previous = (
                        InvoiceMaterial.objects.using(alias)
                        .filter(invoice_id=roll.invoice_id)
                        .order_by('-urut')
                        .first()
                    )
                    next_panel[roll.invoice_id] = previous.urut if previous else 0
                next_panel[roll.invoice_id] += 1
                existing = InvoiceMaterial.objects.using(alias).create(
                    invoice_id=roll.invoice_id,
                    material_id=roll.material_id,
                    urut=next_panel[roll.invoice_id],
                    created_at=roll.created_at,
                )
            panel_cache[panel_key] = existing
        panel = panel_cache[panel_key]
        row_key = (panel.pk, roll.color_id, roll.lokasi_id)
        if row_key not in row_cache:
            existing = (
                InvoiceColor.objects.using(alias)
                .filter(
                    group_id=panel.pk,
                    color_id=roll.color_id,
                    lokasi_id=roll.lokasi_id,
                )
                .first()
            )
            if existing is None:
                if panel.pk not in next_row:
                    previous = (
                        InvoiceColor.objects.using(alias)
                        .filter(group_id=panel.pk)
                        .order_by('-urut')
                        .first()
                    )
                    next_row[panel.pk] = previous.urut if previous else 0
                next_row[panel.pk] += 1
                existing = InvoiceColor.objects.using(alias).create(
                    group_id=panel.pk,
                    color_id=roll.color_id,
                    lokasi_id=roll.lokasi_id,
                    urut=next_row[panel.pk],
                    created_at=roll.created_at,
                )
            row_cache[row_key] = existing
        roll.invoice_color_id = row_cache[row_key].pk
        pending.append(roll)
        if len(pending) >= 1000:
            models['Roll'].objects.using(alias).bulk_update(pending, ['invoice_color'])
            pending = []
    if pending:
        models['Roll'].objects.using(alias).bulk_update(pending, ['invoice_color'])

    # Persist all membership still known in the old schema without inventing released rolls.
    memberships = []
    allocation_dates = dict(models['Alokasi'].objects.using(alias).values_list('pk', 'created_at'))
    for roll in (
        models['Roll']
        .objects.using(alias)
        .exclude(alokasi_id=None)
        .order_by('id')
        .iterator(chunk_size=1000)
    ):
        memberships.append(
            AlokasiRoll(
                alokasi_id=roll.alokasi_id,
                roll_id=roll.pk,
                created_at=allocation_dates[roll.alokasi_id],
            )
        )
        if len(memberships) >= 1000:
            AlokasiRoll.objects.using(alias).bulk_create(memberships, ignore_conflicts=True)
            memberships = []
    if memberships:
        AlokasiRoll.objects.using(alias).bulk_create(memberships, ignore_conflicts=True)

    # Only a single known CMT is deterministic. Empty/multiple-CMT cases stay null.
    for po in models['Po'].objects.using(alias).filter(cmt_id=None).order_by('pk').iterator():
        cmt_ids = list(
            models['Alokasi']
            .objects.using(alias)
            .filter(po_id=po.pk)
            .values_list('cmt_id', flat=True)
            .distinct()
        )
        if len(cmt_ids) == 1:
            models['Po'].objects.using(alias).filter(pk=po.pk).update(cmt_id=cmt_ids[0])

    # Copy only dates and delivery notes already consistently present on every member roll.
    for allocation in models['Alokasi'].objects.using(alias).order_by('pk').iterator():
        details = list(
            models['Roll']
            .objects.using(alias)
            .filter(alokasi_id=allocation.pk)
            .values_list('tgl_kirim', 'sj_kirim')
        )
        if details and len(set(details)) == 1 and details[0][0] is not None:
            models['Alokasi'].objects.using(alias).filter(pk=allocation.pk).update(
                tgl_kirim=details[0][0],
                sj_kirim=details[0][1],
            )
    after = totals()
    if before != after:
        raise RuntimeError(
            f'Migrasi histori mengubah jumlah atau total transaksi: {before!r} != {after!r}'
        )


class Migration(migrations.Migration):
    dependencies = [('tracking', '0004_material_cmt_workflow')]
    operations = [migrations.RunPython(migrate_history, migrations.RunPython.noop)]
