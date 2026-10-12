from collections import defaultdict

from django.db import migrations
from django.db.models import Q, Sum


def preserve_history(apps, schema_editor):
    alias = schema_editor.connection.alias
    Po = apps.get_model('tracking', 'Po')
    Master = apps.get_model('tracking', 'Master')
    Roll = apps.get_model('tracking', 'Roll')
    Hasil = apps.get_model('tracking', 'Hasil')
    Shipment = apps.get_model('tracking', 'KirimGudang')
    Size = apps.get_model('tracking', 'HasilUkuran')
    SizeShipment = apps.get_model('tracking', 'KirimUkuran')
    products = {m.name.casefold(): m for m in Master.objects.using(alias).filter(kind='product')}
    pairs, results, sent = defaultdict(set), defaultdict(dict), defaultdict(dict)
    for allocated_po, legacy_po, material, color in (
        Roll.objects.using(alias)
        .filter(
            Q(alokasi__po__isnull=False) | Q(alokasi__isnull=True, invoice_po__po__isnull=False)
        )
        .values_list('alokasi__po_id', 'invoice_po__po_id', 'material_id', 'color_id')
    ):
        pairs[allocated_po or legacy_po].add((material, color))
    sized = {}
    for pk, po, material, color, pcs, complete in Hasil.objects.using(alias).values_list(
        'id', 'po_id', 'material_id', 'color_id', 'pcs', 'sizes_complete'
    ):
        pair = (material, color)
        pairs[po].add(pair)
        results[po][pair] = pcs
        if complete:
            sized[pk] = (po, pair)
    for item in (
        Shipment.objects.using(alias)
        .values('po_id', 'material_id', 'color_id')
        .annotate(total=Sum('pcs'))
    ):
        pair = (item['material_id'], item['color_id'])
        pairs[item['po_id']].add(pair)
        sent[item['po_id']][pair] = item['total']
    size_sent = dict(
        SizeShipment.objects.using(alias)
        .values('ukuran_id')
        .annotate(total=Sum('pcs'))
        .values_list('ukuran_id', 'total')
    )
    over_pos = set()
    for po, pair in sized.values():
        results[po][pair] = 0
    for pk, hasil_id, pcs in (
        Size.objects.using(alias).filter(hasil_id__in=sized).values_list('id', 'hasil_id', 'pcs')
    ):
        po, pair = sized[hasil_id]
        results[po][pair] += pcs
        if size_sent.get(pk, 0) > pcs:
            over_pos.add(po)
    for po in Po.objects.using(alias).iterator():
        name = ' '.join(po.produk.split())
        if name:
            product = products.get(name.casefold())
            if product is None:
                product = Master.objects.using(alias).create(kind='product', name=name)
                products[name.casefold()] = product
            po.product_id = product.pk
        # Exactly preserve the former resolver's completed state, including size overages.
        completed = bool(
            results[po.pk]
            and sum(results[po.pk].values()) > 0
            and po.pk not in over_pos
            and all(material is not None for material, color in pairs[po.pk])
            and all(
                pair in results[po.pk] and results[po.pk][pair] == sent[po.pk].get(pair, 0)
                for pair in pairs[po.pk]
            )
        )
        po.legacy_completed = po.selesai or completed
        po.save(using=alias, update_fields=['product', 'legacy_completed'])
    # No usage values or snapshots are inferred from historical production.


class Migration(migrations.Migration):
    dependencies = [('tracking', '0010_material_usage_snapshots')]
    operations = [migrations.RunPython(preserve_history, migrations.RunPython.noop)]
