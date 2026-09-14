import uuid
from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from .models import *
from .manual_fields import manualize

LABELS = {'kind':'Jenis','code':'Kode','name':'Nama','pic':'PIC pelapor','contact':'Kontak','notes':'Catatan / alasan','active':'Aktif','unit':'Satuan','standard_usage':'Standar pemakaian (yard/pcs)','target_qty':'Target (pcs)','vendor':'Vendor','invoice':'Nomor invoice','delivery_note':'Nomor surat jalan','invoice_date':'Tanggal invoice','received_date':'Tanggal diterima','invoice_total':'Total invoice (Rp)','warehouse':'Lokasi / gudang','revision_of':'Revisi dari invoice','material':'Bahan','color':'Warna','rolls':'Roll','yards':'Yard','number':'Nomor PO','product':'Produk','target':'Target hasil (pcs)','cmts':'CMT pelaksana','order_date':'Tanggal order','due_date':'Target selesai','order':'PO produksi','source_order':'PO sumber transfer (opsional)','cmt':'CMT','planned_date':'Rencana kirim','lot':'Lot / invoice','allocation_line':'Baris alokasi','sent_date':'Tanggal kirim','report_date':'Tanggal laporan','medium':'Media laporan','condition':'Kondisi','stage':'Tahap','quantity':'Jumlah (pcs)','reject':'Reject (pcs)','eta':'Estimasi selesai','shipment':'Pengiriman hasil','received':'Diterima (pcs)','good':'Good (pcs)','discrepancy':'Ada selisih / kerusakan','action':'Tindakan','target_allocation':'Alokasi transfer aktif','username':'Nama pengguna','first_name':'Nama lengkap','email':'Email','role':'Role','is_active':'Akun aktif','password1':'Kata sandi','password2':'Ulangi kata sandi'}

class BaseForm(forms.ModelForm):
    token = forms.UUIDField(widget=forms.HiddenInput,initial=uuid.uuid4)
    version = forms.IntegerField(widget=forms.HiddenInput,required=False,initial=1)
    attachment = forms.FileField(label='Lampiran bukti',required=False,help_text='PDF, JPG, PNG · maksimal 10 MB')
    def __init__(self,*args,**kwargs):
        actor=kwargs.pop('actor',None)
        super().__init__(*args,**kwargs)
        if self.is_bound and self.data.get(self.add_prefix('DELETE')):
            actor=None
        manualize(self,actor)
        for name,field in self.fields.items():
            field.label = LABELS.get(name,field.label)
            if isinstance(field,forms.DateField):
                field.widget = forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d')
                field.input_formats = ['%Y-%m-%d']
            elif isinstance(field.widget,forms.Textarea):
                field.widget.attrs['rows'] = 3
            if isinstance(field,forms.DecimalField):
                field.widget.attrs.update({'step':'0.01','min':'0'})
            elif isinstance(field,forms.IntegerField) and name not in ['version']:
                field.widget.attrs['min'] = 0
        if self.instance.pk and 'version' in self.fields:
            self.initial['version'] = self.instance.version
    def masters(self,**mapping):
        for field,kind in mapping.items():
            self.fields[field].queryset = Master.objects.filter(kind=kind,active=True)
            self.fields[field].master_kind=kind
            self.fields[field].help_text='Ketik nama atau kode. Nama baru disimpan otomatis.'
    def clean(self):
        data = super().clean()
        attachment = data.get('attachment')
        if attachment and attachment.size > 10*1024*1024:
            self.add_error('attachment','Lampiran maksimal 10 MB.')
        return data

class MasterForm(BaseForm):
    class Meta:
        model=Master
        fields=['kind','code','name','pic','contact','notes','active','unit','standard_usage','target_qty']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields.pop('attachment')
        self.masters(unit='unit')
        if self.instance.pk:
            self.fields['kind'].disabled = True
    def clean_code(self):
        return normalized(self.cleaned_data['code'])

class ReceiptForm(BaseForm):
    class Meta:
        model=Receipt
        fields=['vendor','invoice','delivery_note','invoice_date','received_date','invoice_total','warehouse','revision_of','notes']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.masters(vendor='vendor',warehouse='warehouse')
        self.fields['revision_of'].queryset=Receipt.objects.filter(status='reversed')
    def clean_invoice(self):
        return normalized(self.cleaned_data['invoice'])

class ReceiptLineForm(BaseForm):
    class Meta:
        model=ReceiptLine
        fields=['material','color','rolls','yards','unit','warehouse','notes']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields.pop('attachment'); self.fields.pop('token'); self.fields.pop('version')
        self.masters(material='material',color='color',unit='unit',warehouse='warehouse')

class OrderForm(BaseForm):
    class Meta:
        model=Order
        fields=['number','product','target','cmts','order_date','due_date','standard_usage','notes']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields.pop('attachment')
        self.masters(product='product',cmts='cmt')
        self.fields['cmts'].help_text='Ketik nama CMT, pisahkan dengan titik koma (;). Nama baru disimpan otomatis. Kosong berarti semua CMT aktif.'
    def clean_number(self):
        return normalize_po(self.cleaned_data['number'])

class AllocationForm(BaseForm):
    class Meta:
        model=Allocation
        fields=['order','cmt','source_order','planned_date','notes']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields.pop('attachment')
        self.masters(cmt='cmt')
        for field in ['order','source_order']:
            self.fields[field].queryset=Order.objects.exclude(status__in=['draft','closed','cancelled'])
        self.fields['source_order'].help_text='Isi hanya untuk transfer sisa bahan antar-PO di CMT yang sama.'

class AllocationLineForm(BaseForm):
    class Meta:
        model=AllocationLine
        fields=['lot','warehouse','rolls','yards']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields.pop('attachment'); self.fields.pop('token'); self.fields.pop('version')
        self.fields['warehouse'].queryset=Master.objects.filter(kind__in=['warehouse','cmt'],active=True)
        self.fields['lot'].queryset=Lot.objects.filter(receipt_line__receipt__status='posted').select_related('receipt_line__material','receipt_line__color','receipt_line__receipt')

class ShipmentForm(BaseForm):
    class Meta:
        model=Shipment
        fields=['delivery_note','sent_date']

class ShipmentLineForm(BaseForm):
    class Meta:
        model=ShipmentLine
        fields=['allocation_line','rolls','yards']
    def __init__(self,*args,allocation=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields.pop('attachment'); self.fields.pop('token'); self.fields.pop('version')
        self.fields['allocation_line'].queryset=AllocationLine.objects.filter(allocation=allocation).select_related('lot__receipt_line__material','lot__receipt_line__color','warehouse')
        self.fields['allocation_line'].label_from_instance=lambda x:f'{x.lot.code} · {x.warehouse.name} · {x.rolls} roll / {x.yards} yard'

REPORT_FIELDS=['report_date','pic','medium','notes']

class CMTReceiptForm(BaseForm):
    class Meta:
        model=CMTReceipt
        fields=['rolls','yards','condition']+REPORT_FIELDS

class ProgressForm(BaseForm):
    class Meta:
        model=Progress
        fields=['order','cmt','stage','quantity','reject','eta']+REPORT_FIELDS
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.masters(cmt='cmt')
        self.fields['order'].queryset=Order.objects.exclude(status__in=['draft','closed','cancelled'])

class FinishedForm(BaseForm):
    class Meta:
        model=FinishedShipment
        fields=['order','cmt','warehouse','delivery_note','quantity']+REPORT_FIELDS
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.masters(cmt='cmt',warehouse='warehouse')
        self.fields['order'].queryset=Order.objects.exclude(status__in=['draft','closed','cancelled'])

class WarehouseForm(BaseForm):
    class Meta:
        model=WarehouseReceipt
        fields=['shipment','received','good','reject','discrepancy']+REPORT_FIELDS
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['shipment'].queryset=FinishedShipment.objects.exclude(status='received').exclude(order__status__in=['closed','cancelled'])

class ReconciliationForm(BaseForm):
    class Meta:
        model=Reconciliation
        fields=['order','cmt','lot','action','rolls','yards','warehouse','target_allocation']+REPORT_FIELDS
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.masters(cmt='cmt',warehouse='warehouse')
        self.fields['order'].queryset=Order.objects.exclude(status__in=['draft','closed','cancelled'])
        self.fields['target_allocation'].queryset=AllocationLine.objects.filter(allocation__source_order__isnull=False,allocation__status__in=['allocated','partially_shipped'])
        self.fields['target_allocation'].label_from_instance=lambda x:f'{x.allocation} → {x.allocation.order} · {x.lot.code}'

class ActionForm(forms.Form):
    token=forms.UUIDField(initial=uuid.uuid4,widget=forms.HiddenInput)
    reason=forms.CharField(label='Alasan / catatan',required=False,widget=forms.Textarea(attrs={'rows':3}))
    attachment=forms.FileField(label='Lampiran bukti',required=False,help_text='PDF, JPG, PNG · maksimal 10 MB')
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        manualize(self)

class ResolutionForm(ActionForm):
    resolution=forms.ChoiceField(label='Resolusi',choices=[('confirmed','Penerimaan telah sesuai / kerusakan direkonsiliasi'),('loss','Catat kehilangan dari sisa pengiriman')])
    rolls=forms.IntegerField(label='Roll hilang / pcs hasil hilang',min_value=0,initial=0)
    yards=forms.DecimalField(label='Yard hilang',min_value=0,max_digits=14,decimal_places=2,initial=0)

class AdjustmentForm(ActionForm):
    lot=forms.ModelChoiceField(queryset=Lot.objects.all(),label='Lot')
    warehouse=forms.ModelChoiceField(queryset=Master.objects.filter(kind='warehouse',active=True),label='Gudang')
    direction=forms.ChoiceField(label='Arah',choices=[('in','Tambah'),('out','Kurangi')])
    rolls=forms.IntegerField(label='Roll',min_value=0)
    yards=forms.DecimalField(label='Yard',max_digits=14,decimal_places=2,min_value=0)

class LoginForm(AuthenticationForm):
    username=forms.CharField(label='Nama pengguna',widget=forms.TextInput(attrs={'autofocus':True,'autocomplete':'username'}))
    password=forms.CharField(label='Kata sandi',strip=False,widget=forms.PasswordInput(attrs={'autocomplete':'current-password'}))

class AccountForm(forms.ModelForm):
    new_password=forms.CharField(label='Kata sandi baru',required=False,min_length=6,strip=False,widget=forms.PasswordInput(attrs={'autocomplete':'new-password'}),help_text='Minimal 6 karakter. Kosongkan jika tidak ingin mengganti kata sandi.')
    token=forms.UUIDField(initial=uuid.uuid4,widget=forms.HiddenInput)
    class Meta:
        model=User
        fields=['username','first_name','email','role','is_active']
        labels=LABELS
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['first_name'].label='Nama lengkap (opsional)'
        self.fields['email'].label='Email (opsional)'
        if not self.instance.pk:
            self.fields.pop('is_active')
            self.fields['new_password'].required=True
            self.fields['new_password'].label='Kata sandi'
            self.fields['new_password'].help_text='Minimal 6 karakter.'
    def clean(self):
        data=super().clean()
        from django.contrib.auth.password_validation import validate_password
        if data.get('new_password'):
            validate_password(data['new_password'],self.instance)
        elif not self.instance.pk:
            self.add_error('new_password','Kata sandi wajib untuk akun baru.')
        return data

class FilterForm(forms.Form):
    q=forms.CharField(label='Cari',required=False,widget=forms.TextInput(attrs={'placeholder':'Nomor PO, invoice, bahan…'}))
    status=forms.CharField(label='Status',required=False)
    start=forms.DateField(label='Dari tanggal',required=False,widget=forms.DateInput(attrs={'type':'date'}))
    end=forms.DateField(label='Sampai tanggal',required=False,widget=forms.DateInput(attrs={'type':'date'}))
    scope=forms.ChoiceField(label='Jenis lokasi',required=False,choices=[('','Semua lokasi'),('warehouse','Gudang'),('cmt','CMT')])
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        for kind in ['vendor','cmt','material','color','product','warehouse']:
            self.fields[kind]=forms.ModelChoiceField(queryset=Master.objects.filter(kind=kind),required=False,label=LABELS[kind])
        self.fields['order']=forms.ModelChoiceField(queryset=Order.objects.all(),required=False,label='PO')
    def clean(self):
        data=super().clean()
        if data.get('start') and data.get('end') and data['start']>data['end']:
            raise ValidationError('Tanggal awal harus sebelum tanggal akhir.')
        return data
