from datetime import date, timedelta
from decimal import Decimal
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from django.db import transaction
from django.utils import timezone
from tracking.models import *
from tracking import services as s

class Command(BaseCommand):
    help='Data contoh lokal. Tidak dijalankan pada database produksi.'
    @transaction.atomic
    def handle(self,*args,**options):
        if not settings.DEBUG or settings.DATABASE_URL:
            raise CommandError('Seed demo hanya untuk SQLite lokal dengan DEBUG=true.')
        if User.objects.filter(username='demo.purchasing').exists():
            self.stdout.write('Data demo sudah tersedia; tidak ditambahkan ulang.'); return
        if Receipt.objects.exists() or Order.objects.exists():
            raise CommandError('Gunakan database lokal kosong untuk data demo.')
        users={}
        for role,label in [('purchasing','Nadia Putri'),('approver','Rizky Pratama'),('management','Management'),('admin','Administrator')]:
            user=User(username='demo.'+role,first_name=label,role=role,can_adjust=role=='purchasing',can_reopen=role=='purchasing')
            user.set_unusable_password(); user.save(); users[role]=user
        p=users['purchasing']; a=users['approver']
        def master(kind,code,name,**extra):
            return s.save_master(p,{'kind':kind,'code':code,'name':name,**extra})
        yard=master('unit','YRD','Yard')
        material=master('material','INESA','Inesa',unit=yard)
        cream=master('color','CRM','Cream'); black=master('color','BLK','Black'); bw=master('color','BW','Broken White')
        vendor=master('vendor','VND-001','Sumber Tekstil',pic='Ibu Rina')
        vendor2=master('vendor','VND-002','Prima Sandang',pic='Bapak Arif')
        cmt1=master('cmt','CMT-001','Ci Herlina',pic='Ci Herlina')
        cmt2=master('cmt','CMT-002','Ci Ling Ling',pic='Ci Ling Ling')
        warehouse=master('warehouse','GDG-01','Gudang Utama',pic='Budi')
        master('warehouse','GDG-02','Gudang Hasil',pic='Sari')
        master('target','TGT-1072','Target Fuji Midi',target_qty=1072)
        products=[master('product','PRD-'+str(i+1).zfill(3),name,standard_usage=Decimal('1.65')) for i,name in enumerate(['Fuji Midi','Kirana Blouse','Ayla Dress','Nara Tunik','Luna Top','Fuji Maxi'])]
        today=timezone.localdate()
        lots=[]
        for i,(color,yards) in enumerate([(cream,'1665.50'),(black,'5910.00'),(bw,'2800.00')]):
            r=s.save_receipt(p,{'vendor':vendor if i<2 else vendor2,'invoice':f'INV/IX/2026/{41+i:04d}','delivery_note':f'SJ-VND-{41+i}','received_date':today-timedelta(days=9-i),'invoice_date':today-timedelta(days=10-i),'invoice_total':Decimal('25000000')+i*1000000,'warehouse':warehouse},[{'material':material,'color':color,'unit':yard,'warehouse':warehouse,'rolls':50,'yards':Decimal(yards),'notes':''}])
            s.post_receipt(p,r.pk)
            lots.append(r.lines.first().lot)
        for i,target in enumerate([1072,840,600,1200,480,750]):
            order=s.save_order(p,{'number':f'PO {111+i:04d}','product':products[i],'target':target,'cmts':[cmt1,cmt2],'order_date':date(2026,7,24) if i==0 else today-timedelta(days=8-i),'due_date':today+timedelta(days=i*3-2),'standard_usage':Decimal('1.65')})
            s.order_action(p,order.pk,'activate'); order.refresh_from_db()
            lot=lots[1] if i==0 else lots[i%3]
            q=(15,Decimal('1773')) if i==0 else (7,Decimal('233.17')) if i%3==0 else (5,Decimal('450')) if i%3==1 else (5,Decimal('280'))
            cmt=cmt1 if i%2==0 else cmt2
            al=s.save_allocation(p,{'order':order,'cmt':cmt,'planned_date':today+timedelta(days=1)},[{'lot':lot,'warehouse':warehouse,'rolls':q[0],'yards':q[1]}])
            s.submit_allocation(p,al.pk)
            if i>=4: continue
            s.decide_allocation(a,al.pk,'approved','Bahan tersedia.'); al.refresh_from_db()
            sj=s.save_shipment(p,al,{'delivery_note':f'SJ-CMT-{210+i}','sent_date':today-timedelta(days=3)},[{'allocation_line':al.lines.first(),'rolls':q[0],'yards':q[1]}])
            s.dispatch(p,sj.pk)
            if i==3: continue
            s.receive_cmt(p,sj.lines.first(),{'rolls':q[0],'yards':q[1],'condition':'good','report_date':today-timedelta(days=2),'pic':cmt.name,'medium':'whatsapp','notes':'Laporan penerimaan bahan sesuai surat jalan.'})
            s.save_progress(p,{'order':order,'cmt':cmt,'stage':'sewing' if i==2 else 'finishing','quantity':800 if i==0 else 400,'reject':0,'report_date':today,'pic':cmt.name,'medium':'whatsapp','notes':'Produksi berjalan.','eta':order.due_date})
            if i<2:
                shipments=[430,247] if i==0 else [280]
                for j,count in enumerate(shipments):
                    fs=s.send_finished(p,{'order':order,'cmt':cmt,'warehouse':warehouse,'delivery_note':f'SJ-HSL-{i+1}-{j+1}','quantity':count,'report_date':today-timedelta(days=1),'pic':cmt.name,'medium':'document','notes':''})
                    s.receive_warehouse(p,{'shipment':fs,'received':count,'good':count,'reject':0,'discrepancy':False,'report_date':today,'pic':'Budi','medium':'whatsapp','notes':'Hasil sudah diperiksa.'})
        self.stdout.write(self.style.SUCCESS('Data contoh siap: 4 role, 3 invoice, 6 PO, alokasi, pengiriman, dan hasil gudang.'))
