from collections import defaultdict
from django.core.management.base import BaseCommand,CommandError
from django.db.models import Sum
from tracking.models import Movement,ZERO

class Command(BaseCommand):
    help='Audit saldo negatif dan stok bebas per lot/lokasi/PO dari ledger.'
    def handle(self,*args,**opts):
        buckets=defaultdict(lambda:defaultdict(lambda:[0,ZERO]))
        for m in Movement.objects.order_by('created_at','pk'):
            group=(m.lot_id,m.location_id,m.order_id if m.bucket in ['cmt','transit'] or (m.bucket=='reserved' and m.location.kind=='cmt') else None)
            b=buckets[group][m.bucket]
            b[0]+=m.rolls; b[1]+=m.yards
            # Multi-entry postings are atomik, audit saldo akhir (bukan urutan entry internal).
        errors=[]
        for group,b in buckets.items():
            for bucket,pair in b.items():
                if pair[0]<0 or pair[1]<0: errors.append(f'{group} {bucket}: {pair}')
            physical=b['cmt'] if 'cmt' in b else b['physical']
            reserved=b['reserved']
            if reserved[0]>physical[0] or reserved[1]>physical[1]:
                errors.append(f'{group}: reservasi melebihi stok')
        if errors: raise CommandError('\n'.join(errors))
        self.stdout.write(self.style.SUCCESS(f'Ledger valid: {Movement.objects.count()} pergerakan, {len(buckets)} kelompok saldo.'))
