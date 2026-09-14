from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from . import services
from .models import Order
from .reports import dataset
from .tests import Fixture


class ListingQueryTests(Fixture,TestCase):
    def setUp(self):
        self.build()

    def test_order_page_has_constant_query_count_and_correct_totals(self):
        self.allocate()
        first,receipt=self.finished(12,good=10,reject=2)
        second,unused=self.finished(3,good=2,reject=1)
        # Include several receipts per PO: aggregation must not multiply values.
        expected=services.totals(self.order)
        self.assertEqual(services.totals_many([self.order])[self.order.pk],expected)
        with CaptureQueriesContext(connection) as small:
            dataset('orders',{},self.p,{'_page':1})
        Order.objects.bulk_create([Order(created_by=self.p,number=f'PERF-{i}',product=self.product,target=100) for i in range(30)])
        options={'_page':1}
        with CaptureQueriesContext(connection) as large:
            headers,rows=dataset('orders',{},self.p,options)
        self.assertEqual(len(rows),25)
        self.assertEqual(options['_total'],31)
        self.assertEqual(len(small),3)
        self.assertEqual(len(large),3)
        orders=list(Order.objects.all())
        with self.assertNumQueries(1):
            values=services.totals_many(orders)
        self.assertEqual(values[self.order.pk],expected)
        self.assertTrue(all(v['good']==0 for pk,v in values.items() if pk!=self.order.pk))

    def test_quantity_pages_use_one_grouped_query(self):
        shipment=self.shipment()
        for kind in ['receipts','allocations','shipments']:
            with self.subTest(kind=kind):
                with self.assertNumQueries(3):
                    headers,rows=dataset(kind,{},self.p,{'_page':1})
                self.assertEqual(len(rows),1)

    def test_empty_page_and_filtered_receipts(self):
        with self.assertNumQueries(1):
            headers,rows=dataset('orders',{'q':'missing-number'},self.p,{'_page':1})
        self.assertEqual(rows,[])
        # Filtering by a material must still show the full invoice totals.
        from .models import Master
        material=services.save_master(self.p,{'kind':'material','code':'OTHER','name':'Other'})
        lines=[self.receipt_line(2,'60'),{**self.receipt_line(3,'90'),'material':material}]
        obj=services.save_receipt(self.p,{'vendor':self.vendor,'invoice':'PERF-MULTI','warehouse':self.wh,'received_date':self.today},lines)
        headers,rows=dataset('receipts',{'q':'PERF-MULTI','material':material},self.p,{'_page':1})
        self.assertEqual(len(rows),1)
        self.assertIn('150',str(rows))
