import uuid

from django import forms
from django.core.exceptions import ValidationError
from django.test import TestCase

from . import forms as app_forms, services
from .models import Master, Order, Receipt, Audit
from .tests import Fixture


class ManualInputTests(TestCase):
    def setUp(self):
        from .models import User
        self.p=User.objects.create_user(username='purchasing',role='purchasing')
        self.client.force_login(self.p)

    def order_data(self,**changes):
        return {'token':str(uuid.uuid4()),'number':'PO 600','product':'Kemeja Linen',
                'target':'100','cmts':'CMT Jakarta; CMT Bogor','order_date':'2026-09-14',**changes}

    def test_new_names_create_audited_masters_and_reuse_them(self):
        data=self.order_data()
        response=self.client.post('/orders/new/',data)
        self.assertEqual(response.status_code,302,response.content.decode())
        order=Order.objects.get()
        self.assertEqual(order.product.name,'Kemeja Linen')
        self.assertEqual(order.cmts.count(),2)
        self.assertEqual(Master.objects.count(),3)
        self.assertEqual(Audit.objects.filter(action='create_master').count(),3)
        self.assertEqual(self.client.post('/orders/new/',data).status_code,302)
        self.assertEqual(Order.objects.count(),1)
        self.assertEqual(self.client.post('/orders/new/',self.order_data(number='PO 601',product=' kemeja   linen ',cmts='cmt jakarta')).status_code,302)
        self.assertEqual(Master.objects.count(),3)

    def test_failed_form_and_business_validation_leave_no_new_masters(self):
        self.assertEqual(self.client.post('/orders/new/',self.order_data(target='0')).status_code,400)
        self.assertEqual(Master.objects.count(),0)
        self.assertEqual(Audit.objects.filter(action='create_master').count(),0)
        self.client.post('/orders/new/',self.order_data())
        before=Master.objects.count()
        response=self.client.post('/orders/new/',self.order_data(product='Produk baru gagal',cmts='CMT baru gagal'))
        self.assertEqual(response.status_code,400)
        self.assertEqual(Master.objects.count(),before)

    def test_receipt_works_with_empty_master_tables(self):
        data={'token':str(uuid.uuid4()),'vendor':'PT Kain','invoice':'INV MANUAL',
              'received_date':'2026-09-14','invoice_total':'1000','warehouse':'Gudang Utama',
              'lines-TOTAL_FORMS':'1','lines-INITIAL_FORMS':'0',
              'lines-0-material':'Linen','lines-0-color':'Putih','lines-0-unit':'Yard',
              'lines-0-warehouse':'Gudang Utama','lines-0-rolls':'2','lines-0-yards':'60'}
        response=self.client.post('/receipts/new/',data)
        self.assertEqual(response.status_code,302,response.content.decode())
        receipt=Receipt.objects.get()
        self.assertEqual(receipt.warehouse,receipt.lines.get().warehouse)
        services.post_receipt(self.p,receipt.pk)
        self.assertEqual(services.balance(bucket='physical')[0],2)
        self.assertEqual(Master.objects.count(),5)

    def test_every_operational_choice_is_a_text_input(self):
        for cls in [app_forms.MasterForm,app_forms.ReceiptForm,app_forms.ReceiptLineForm,
                    app_forms.OrderForm,app_forms.AllocationForm,app_forms.AllocationLineForm,
                    app_forms.ShipmentLineForm,app_forms.CMTReceiptForm,app_forms.ProgressForm,
                    app_forms.FinishedForm,app_forms.WarehouseForm,app_forms.ReconciliationForm,
                    app_forms.AdjustmentForm,app_forms.ResolutionForm]:
            for name,field in cls().fields.items():
                self.assertNotIsInstance(field.widget,forms.Select,(cls.__name__,name))
        response=self.client.get('/receipts/new/')
        self.assertNotContains(response,'<select')
        self.assertContains(response,'name="lines-__prefix__-material"')

    def test_inactive_and_ambiguous_names_are_rejected(self):
        for code in ['A','B']:
            Master.objects.create(kind='product',code=code,name='Nama sama',created_by=self.p)
        field=app_forms.OrderForm(actor=self.p).fields['product']
        with self.assertRaises(ValidationError): field.clean('Nama sama')
        self.assertEqual(field.clean('A').code,'A')
        Master.objects.create(kind='product',code='OLD',name='Produk lama',active=False,created_by=self.p)
        with self.assertRaises(ValidationError): field.clean('Produk lama')

    def test_typed_labels_map_to_business_codes(self):
        field=app_forms.ProgressForm().fields['stage']
        self.assertEqual(field.clean('jahit'),'sewing')
        with self.assertRaises(ValidationError): field.clean('tahap asal')
        self.assertEqual(app_forms.AdjustmentForm().fields['direction'].clean('Tambah'),'in')


class ManualReferenceTests(Fixture,TestCase):
    def setUp(self):
        self.build()
        self.client.force_login(self.p)

    def test_typed_allocation_and_shipment_preserve_stock_relationships(self):
        lot=self.posted()
        data={'token':str(uuid.uuid4()),'order':self.order.number,'cmt':self.cmt.name,
              'intent':'allocate','lines-TOTAL_FORMS':'1','lines-INITIAL_FORMS':'0',
              'lines-0-lot':lot.code,'lines-0-warehouse':self.wh.name,
              'lines-0-rolls':'2','lines-0-yards':'60'}
        response=self.client.post('/allocations/new/',data)
        self.assertEqual(response.status_code,302,response.content.decode())
        allocation=self.order.allocations.get()
        line=allocation.lines.get()
        field=app_forms.ShipmentLineForm(allocation=allocation).fields['allocation_line']
        self.assertEqual(field.clean(lot.code),line)
        self.assertEqual(field.clean(f'BARIS-{line.pk}'),line)
        self.assertEqual(services.balance(bucket='reserved')[0],2)
        self.assertContains(self.client.get(response.url),f'BARIS-{line.pk}')

    def test_missing_or_draft_po_is_not_created_or_used(self):
        field=app_forms.AllocationForm().fields['order']
        with self.assertRaises(ValidationError): field.clean('PO TIDAK ADA')
        Order.objects.filter(pk=self.order.pk).update(status='draft')
        with self.assertRaises(ValidationError): field.clean(self.order.number)

    def test_edit_form_shows_readable_names_and_multi_cmt(self):
        form=app_forms.OrderForm(instance=self.order)
        self.assertIn('CMT',form['cmts'].value())
        self.assertIn(self.product.name,form['product'].value())