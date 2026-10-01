from django import forms
from django.contrib.auth.forms import AuthenticationForm

from .models import Po, User


class LoginForm(AuthenticationForm):
    username = forms.CharField(label='Nama pengguna')
    password = forms.CharField(label='Kata sandi', widget=forms.PasswordInput)


class InvoiceForm(forms.Form):
    vendor = forms.CharField(label='Vendor', max_length=160)
    nomor = forms.CharField(label='No. invoice', max_length=80)
    surat_jalan = forms.CharField(label='Surat jalan', max_length=80, required=False)
    tanggal = forms.DateField(label='Tanggal', widget=forms.DateInput(attrs={'type': 'date'}))
    total_rp = forms.DecimalField(
        label='Total Rp', max_digits=16, decimal_places=2, min_value=0, initial=0
    )
    catatan = forms.CharField(label='Catatan', required=False, widget=forms.Textarea)


class PoForm(forms.ModelForm):
    class Meta:
        model = Po
        fields = ['tgl_order', 'produk', 'pemakaian_std', 'catatan']
        widgets = {
            'tgl_order': forms.DateInput(attrs={'type': 'date'}),
            'catatan': forms.Textarea,
        }


class AccountForm(forms.Form):
    username = forms.CharField(label='Nama pengguna', max_length=150)
    role = forms.ChoiceField(label='Peran', choices=User.Role.choices)
    password = forms.CharField(label='Kata sandi', min_length=6, widget=forms.PasswordInput)

    def clean_username(self):
        username = self.cleaned_data['username']
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError('Nama pengguna sudah dipakai.')
        return username
