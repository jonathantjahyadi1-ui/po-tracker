from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.utils import timezone

from .models import InvoicePayment, Master, Po, User
from .money import format_money, parse_money
from .uploads import validate_document


class MoneyField(forms.Field):
    widget = forms.TextInput(attrs={'inputmode': 'decimal', 'placeholder': '500.000.000'})

    def to_python(self, value):
        if value in self.empty_values:
            return None
        return parse_money(value)

    def prepare_value(self, value):
        if value is not None and not isinstance(value, str):
            return format_money(value)
        return value


class LoginForm(AuthenticationForm):
    username = forms.CharField(label='Nama pengguna')
    password = forms.CharField(label='Kata sandi', widget=forms.PasswordInput)


class InvoiceForm(forms.Form):
    vendor = forms.CharField(label='Vendor', max_length=160)
    nomor = forms.CharField(label='No. invoice', max_length=80)
    surat_jalan = forms.CharField(label='Surat jalan', max_length=80, required=False)
    tanggal = forms.DateField(label='Tanggal', widget=forms.DateInput(attrs={'type': 'date'}))
    total_rp = MoneyField(label='Total tagihan (Rp)')
    invoice_file = forms.FileField(
        label='File invoice',
        required=False,
        widget=forms.FileInput(attrs={'accept': '.pdf,.jpg,.jpeg,.png'}),
    )

    def __init__(self, *args, legacy_zero=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.legacy_zero = legacy_zero

    def clean_total_rp(self):
        total = self.cleaned_data['total_rp']
        if total <= 0 and not self.legacy_zero:
            raise forms.ValidationError('Total tagihan wajib lebih besar dari nol.')
        return total

    def clean_invoice_file(self):
        upload = self.cleaned_data['invoice_file']
        return validate_document(upload, 'File invoice', 10) if upload else None


class PaymentForm(forms.Form):
    kind = forms.ChoiceField(label='Pilihan pembayaran', choices=InvoicePayment.Kind.choices)
    amount = MoneyField(label='Nominal dibayar (Rp)')
    payment_date = forms.DateField(
        label='Tanggal pembayaran',
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
    )
    proof = forms.FileField(
        label='Bukti pembayaran',
        widget=forms.FileInput(attrs={'accept': '.pdf,.jpg,.jpeg,.png'}),
    )
    remaining_seen = MoneyField(widget=forms.HiddenInput)
    request_id = forms.RegexField(regex=r'^[a-zA-Z0-9_-]{16,64}$', widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        kind = self.data.get('kind') if self.is_bound else self.initial.get('kind', 'lunas')
        if kind != 'cicil':
            self.fields['amount'].widget.attrs['readonly'] = True

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount <= 0:
            raise forms.ValidationError('Nominal pembayaran wajib lebih besar dari nol.')
        return amount

    def clean_proof(self):
        return validate_document(self.cleaned_data['proof'], 'Bukti pembayaran', 5)


class PoForm(forms.ModelForm):
    class Meta:
        model = Po
        fields = ['tgl_order']
        labels = {'tgl_order': 'Tanggal order'}
        widgets = {
            'tgl_order': forms.DateInput(attrs={'type': 'date'}),
        }


class ReceiveForm(forms.Form):
    tanggal = forms.DateField(
        label='Tanggal terima bahan', widget=forms.DateInput(attrs={'type': 'date'})
    )


class AssignPoForm(forms.Form):
    nomor_po = forms.CharField(label='Nomor PO', max_length=80)
    product = forms.ModelChoiceField(
        label='Produk', queryset=Master.objects.filter(kind='product', active=True)
    )


class UsageField(forms.CharField):
    widget = forms.TextInput(attrs={'inputmode': 'decimal', 'placeholder': 'Contoh: 2,5'})

    def clean(self, value):
        from .production_services import parse_usage

        return parse_usage(super().clean(value))


class MasterForm(forms.Form):
    name = forms.CharField(label='Nama', max_length=160)
    usage = UsageField(label='Pemakaian Paten (yard/pcs)')

    def __init__(self, *args, kind, **kwargs):
        super().__init__(*args, **kwargs)
        if kind == Master.Kind.PRODUCT:
            self.fields['name'].label = 'Nama produk'
        else:
            self.fields.pop('usage')

    def clean_name(self):
        return ' '.join(self.cleaned_data['name'].split())


class MaterialUsageForm(forms.Form):
    id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    material = forms.ModelChoiceField(
        label='Bahan', queryset=Master.objects.filter(kind='material')
    )
    usage = UsageField(label='Pemakaian Paten (yard/pcs)')
    active = forms.BooleanField(label='Aktif', required=False, initial=True)


class UsageFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        seen = set()
        for form in self.forms:
            material = form.cleaned_data.get('material')
            if material is None:
                continue
            if material.pk in seen:
                form.add_error('material', 'Bahan duplikat; edit satu entri untuk kombinasi ini.')
            seen.add(material.pk)


MaterialUsageFormSet = forms.formset_factory(
    MaterialUsageForm, formset=UsageFormSet, extra=1, max_num=500, validate_max=True
)


class AccountForm(forms.Form):
    username = forms.CharField(label='Nama pengguna', max_length=150)
    role = forms.ChoiceField(label='Peran', choices=User.Role.choices)
    password = forms.CharField(label='Kata sandi', min_length=6, widget=forms.PasswordInput)

    def clean_username(self):
        username = self.cleaned_data['username']
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError('Nama pengguna sudah dipakai.')
        return username
