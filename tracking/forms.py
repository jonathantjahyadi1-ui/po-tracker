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
    catatan = forms.CharField(
        label='Catatan', required=False, widget=forms.Textarea(attrs={'rows': 3})
    )
    invoice_file = forms.FileField(
        label='File invoice', required=False,
        widget=forms.FileInput(attrs={'accept': '.pdf,.jpg,.jpeg,.png'}),
    )

    def clean_invoice_file(self):
        upload = self.cleaned_data['invoice_file']
        if not upload:
            return None
        if upload.size > 10 * 1024 * 1024:
            raise forms.ValidationError('File invoice maksimal 10 MB.')
        header = upload.read(12)
        upload.seek(0)
        name = upload.name.lower()
        valid = (
            (name.endswith('.pdf') and header.startswith(b'%PDF-'), 'application/pdf'),
            (name.endswith(('.jpg', '.jpeg')) and header.startswith(b'\xff\xd8\xff'), 'image/jpeg'),
            (name.endswith('.png') and header.startswith(b'\x89PNG\r\n\x1a\n'), 'image/png'),
        )
        upload.verified_content_type = next((mime for accepted, mime in valid if accepted), None)
        if not upload.verified_content_type:
            raise forms.ValidationError('Unggah invoice PDF, JPG, atau PNG yang valid.')
        return upload


class PoForm(forms.ModelForm):
    class Meta:
        model = Po
        fields = ['tgl_order', 'produk', 'pemakaian_std', 'catatan']
        widgets = {
            'tgl_order': forms.DateInput(attrs={'type': 'date'}),
            'catatan': forms.Textarea(attrs={'rows': 3}),
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
