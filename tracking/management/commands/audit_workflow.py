import json

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Sum

from tracking.models import (
    Alokasi,
    AlokasiRoll,
    Hasil,
    Invoice,
    InvoiceAttachment,
    InvoiceColor,
    InvoiceMaterial,
    InvoiceWrite,
    KirimGudang,
    Log,
    Po,
    Roll,
)
from tracking.services import po_status_many


class Command(BaseCommand):
    help = 'Audit baca saja: total transaksi, pemetaan historis CMT, dan anomali hasil/kiriman.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', help='Keluarkan laporan sebagai JSON.')

    @transaction.atomic
    def handle(self, *args, **options):
        counts = {
            model.__name__: model.objects.count()
            for model in (
                Invoice,
                Roll,
                Alokasi,
                Po,
                Hasil,
                KirimGudang,
                InvoiceAttachment,
                Log,
                InvoiceMaterial,
                InvoiceColor,
                InvoiceWrite,
                AlokasiRoll,
            )
        }
        totals = {
            'yard': str(Roll.objects.aggregate(total=Sum('yard'))['total'] or 0),
            'hasil_pcs': Hasil.objects.aggregate(total=Sum('pcs'))['total'] or 0,
            'kiriman_pcs': KirimGudang.objects.aggregate(total=Sum('pcs'))['total'] or 0,
            'attachments_linked': InvoiceAttachment.objects.exclude(invoice_id=None).count(),
        }
        unmapped = []
        for po in (
            Po.objects.filter(cmt=None)
            .annotate(cmt_count=Count('alokasi__cmt', distinct=True))
            .order_by('pk')
        ):
            unmapped.append(
                {
                    'po_id': po.pk,
                    'nomor': po.nomor,
                    'reason': 'multiple_cmt' if po.cmt_count > 1 else 'no_cmt',
                    'cmt_ids': list(
                        po.alokasi_set.order_by('cmt_id')
                        .values_list('cmt_id', flat=True)
                        .distinct()
                    ),
                }
            )
        statuses = po_status_many(Po.objects.all())
        report = {
            'counts': counts,
            'totals': totals,
            'unmapped_pos': unmapped,
            'rolls_without_material_row': Roll.objects.filter(invoice_color=None).count(),
            'legacy_ready_to_ship': list(
                Roll.objects.filter(status='siap_kirim').values_list('pk', flat=True)
            ),
            'legacy_invoice_po_links': Roll.objects.filter(invoice_po__po__isnull=False).count(),
            'allocations_without_po': Alokasi.objects.filter(po=None).count(),
            'allocations_without_membership': list(
                Alokasi.objects.filter(items=None).values_list('pk', flat=True)
            ),
            'over_shipped_pos': [po_id for po_id, summary in statuses.items() if summary['over']],
            'results_incomplete_pos': [
                po_id for po_id, summary in statuses.items() if summary['missing_results']
            ],
            'status_counts': {
                key: sum(item['code'] == key for item in statuses.values())
                for key in (
                    'belum_ada_hasil',
                    'belum_lengkap',
                    'belum_ada_produksi',
                    'kurang_kirim',
                    'lebih_kirim',
                    'done',
                )
            },
        }
        if options['json']:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            self.stdout.write('Audit workflow (baca saja; tidak mengubah transaksi)')
            for section, content in report.items():
                self.stdout.write(f'{section}: {json.dumps(content, ensure_ascii=False)}')
