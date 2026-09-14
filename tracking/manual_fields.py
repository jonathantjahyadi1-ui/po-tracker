import hashlib
import re

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from .models import Master, Order, Lot, Receipt, FinishedShipment, AllocationLine, normalized, normalize_po


def reference_text(obj):
    if isinstance(obj, Master):
        # Include code so identically named master records remain distinguishable.
        return str(obj)
    if isinstance(obj, Order):
        return obj.number
    if isinstance(obj, Lot):
        return obj.code
    if isinstance(obj, Receipt):
        return f'INV-{obj.pk} · {obj.invoice}'
    if isinstance(obj, FinishedShipment):
        return f'HSL-{obj.pk:05d}'
    if isinstance(obj, AllocationLine):
        return f'BARIS-{obj.pk} · {obj.lot.code} · {obj.warehouse.name}'
    return str(obj)


class TypedReference(forms.ModelChoiceField):
    master_kind = None
    actor = None

    def prepare_value(self, value):
        if hasattr(value, 'pk'):
            return reference_text(value)
        if value not in self.empty_values and str(value).isdigit():
            obj=self.queryset.filter(pk=value).first()
            if obj:
                return reference_text(obj)
        return value

    def to_python(self, value):
        if value in self.empty_values:
            return None
        if hasattr(value, 'pk'):
            return super().to_python(value.pk)
        text=' '.join(str(value).strip().split())
        model=self.queryset.model
        prefix=text.split(' · ',1)[0]
        if model is Master:
            query=Q(code__iexact=prefix)|Q(name__iexact=text)
        elif model is Order:
            query=Q(number__iexact=normalize_po(text))
        elif model is Lot:
            query=Q(code__iexact=prefix)
        elif model is Receipt:
            match=re.fullmatch(r'INV-(\d+)',prefix,re.I)
            query=Q(pk=int(match[1])) if match else Q(invoice__iexact=text)
        elif model is FinishedShipment:
            match=re.fullmatch(r'HSL-(\d+)',prefix,re.I)
            query=Q(pk=int(match[1])) if match else Q(delivery_note__iexact=text)
        elif model is AllocationLine:
            match=re.fullmatch(r'BARIS-(\d+)',prefix,re.I)
            query=Q(pk=int(match[1])) if match else Q(lot__code__iexact=prefix)
        else:
            query=Q(pk=None)
        matches=list(self.queryset.filter(query)[:2])
        if len(matches)>1:
            raise ValidationError('Nama atau nomor ini memiliki beberapa data. Ketik kode lengkap yang tercantum pada detail transaksi.')
        if matches:
            return matches[0]
        # Preserve requests from previously opened forms that posted database IDs.
        if text.isdigit():
            obj=self.queryset.filter(pk=text).first()
            if obj:
                return obj
        if model is Master and self.master_kind and self.actor:
            from . import services
            services.require(self.actor,['purchasing'])
            if Master.objects.filter(kind=self.master_kind).filter(query).exists():
                raise ValidationError('Data ini tidak aktif. Aktifkan kembali melalui Data master.')
            if ' · ' in text:
                raise ValidationError('Kode master tidak ditemukan. Untuk data baru, ketik namanya saja.')
            code='AUTO-'+hashlib.sha256(normalized(text).encode()).hexdigest()[:32].upper()
            candidate=Master(kind=self.master_kind,code=code,name=text,created_by=self.actor)
            try:
                candidate.full_clean(validate_unique=False,validate_constraints=False)
            except ValidationError as error:
                raise ValidationError(error.messages)
            # Unique (kind, code) also prevents duplicates on simultaneous submissions.
            with transaction.atomic():
                obj,created=Master.objects.get_or_create(kind=self.master_kind,code=code,
                    defaults={'name':text,'created_by':self.actor})
                if not obj.active:
                    raise ValidationError('Data ini tidak aktif. Aktifkan kembali melalui Data master.')
                if created:
                    services.audit(self.actor,'create_master',obj,after={'kind':obj.kind,'code':obj.code,'name':obj.name,'source':'manual_input'})
                return obj
        raise ValidationError('Data tidak ditemukan atau belum dapat digunakan. Ketik kode/nomor dari transaksi yang sudah tercatat.')


class TypedMultipleReference(TypedReference):
    def prepare_value(self,value):
        if isinstance(value,str):
            return value
        return '; '.join(str(super(TypedMultipleReference,self).prepare_value(v)) for v in (value or []))

    def clean(self,value):
        values=re.split(r'[;\n]',value) if isinstance(value,str) else (value or [])
        values=[v for v in values if str(v).strip()]
        if self.required and not values:
            raise ValidationError(self.error_messages['required'],code='required')
        objects=[self.to_python(v) for v in values]
        return list({obj.pk:obj for obj in objects}.values())


class TypedChoice(forms.ChoiceField):
    def to_python(self,value):
        text=str(value or '').strip()
        for key,label in self.choices:
            if text.casefold() in [str(key).casefold(),str(label).casefold()]:
                return key
        return text


def manualize(form,actor=None):
    for name,old in list(form.fields.items()):
        common=dict(required=old.required,label=old.label,initial=old.initial,
                    help_text=old.help_text,disabled=old.disabled)
        if isinstance(old,forms.ModelChoiceField):
            multiple=isinstance(old,forms.ModelMultipleChoiceField)
            cls=TypedMultipleReference if multiple else TypedReference
            field=cls(queryset=old.queryset,**common)
            field.actor=actor
            field.widget=forms.TextInput(attrs={'placeholder':'Ketik nama atau kode' if old.queryset.model is Master else 'Ketik nomor / kode transaksi','autocomplete':'off'})
            field.help_text=('Pisahkan beberapa nama dengan titik koma (;). ' if multiple else '')+'Ketik nama atau kode.'
            form.fields[name]=field
        elif isinstance(old,forms.ChoiceField):
            choices=list(old.choices)
            field=TypedChoice(choices=choices,**common)
            field.widget=forms.TextInput(attrs={'placeholder':'Ketik '+str(old.label).lower()})
            field.help_text='Ketik: '+', '.join(str(label) for key,label in choices if key)+'.'
            form.fields[name]=field