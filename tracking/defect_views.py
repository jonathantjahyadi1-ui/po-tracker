from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, OperationalError
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .defect_services import (
    can_receive_refund,
    cancel_defect,
    cancel_refund,
    record_defect,
    record_refund,
    return_defect_item,
)
from .forms import DefectForm, RefundForm
from .models import FabricDefect, FabricDefectItem, Invoice, Master, Roll, VendorRefund
from .payment_services import payment_queryset, payment_summary
from .payment_views import payment_access
from .services import require_purchasing
from .views import input_date


def invoice_defect_context(invoice, user, summary=None):
    summary = summary or payment_summary(invoice)
    items = list(
        invoice.defect_items.select_related(
            'defect__recorded_by', 'roll__material', 'roll__color', 'po', 'vendor'
        ).order_by('defect__date', 'pk')
    )
    return {
        'summary': summary,
        'bs_items': items,
        'refunds': list(invoice.refunds.select_related('recorded_by', 'cancelled_by')),
        'can_receive_refund': can_receive_refund(user) and summary.refund_remaining > 0,
        'can_correct_bs': user.is_active and (user.is_superuser or user.role == 'admin'),
    }


@payment_access
@require_http_methods(['GET', 'POST'])
def defect_create(request, pk):
    require_purchasing(request.user)
    cmt = get_object_or_404(Master, pk=pk, kind='cmt')
    selected = (
        request.POST.getlist('roll') if request.method == 'POST' else request.GET.getlist('roll')
    )
    form = DefectForm(
        request.POST if request.method == 'POST' else None, initial={'request_id': uuid4().hex}
    )
    status = 200
    if request.method == 'POST':
        status = 400
        if form.is_valid():
            try:
                defect = record_defect(
                    cmt,
                    [
                        {'roll': key, 'yard': request.POST.get(f'yard-{key}', '')}
                        for key in selected
                    ],
                    form.cleaned_data['date'],
                    form.cleaned_data['note'],
                    form.cleaned_data['request_id'],
                    request.user,
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            except OperationalError:
                form.add_error(
                    None, 'Roll/tagihan sedang diperbarui. Tinjau sisa terbaru dan coba lagi.'
                )
                status = 409
            except DatabaseError:
                form.add_error(
                    None, 'BS belum tersimpan. Isian tetap tersedia; coba simpan kembali.'
                )
                status = 503
            else:
                messages.success(
                    request, 'BS tersimpan. Yard layak pakai dan invoice asal diperbarui.'
                )
                return redirect('defect_detail', pk=defect.pk)
    candidates = Roll.objects.filter(
        alokasi__cmt=cmt,
        status='diterima',
        invoice__dibatalkan=False,
    ).select_related('material', 'color', 'invoice__vendor', 'alokasi__po')
    if selected:
        valid_ids = [int(key) for key in selected if key.isdigit() and len(key) < 19]
        candidates = candidates.filter(pk__in=valid_ids)
    else:
        candidates = candidates.filter(yard__gt=F('bs_yard'))
    page = Paginator(
        candidates.order_by('invoice_id', 'material_id', 'color_id', 'urut'), 50
    ).get_page(request.GET.get('page'))
    rolls = list(page.object_list)
    invoices = {
        invoice.pk: invoice
        for invoice in payment_queryset(
            {
                'q': '',
                'status': '',
                'date_from': None,
                'date_to': None,
            }
        ).filter(pk__in={r.invoice_id for r in rolls})
    }
    impacts = {}
    for key, invoice in invoices.items():
        summary = payment_summary(invoice)
        impacts[str(key)] = {
            'number': invoice.nomor,
            'vendor': invoice.vendor.name,
            'net': str(summary.net_total),
            'paid': str(summary.paid or 0),
            'remaining': str(summary.remaining or 0),
            'reconciliation': summary.status_key == 'reconciliation',
        }
    for roll in rolls:
        roll.selected_bs = str(roll.pk) in selected
        roll.input_bs = request.POST.get(f'yard-{roll.pk}', '')
    return render(
        request,
        'defect_form.html',
        {
            'title': 'Catat BS',
            'active': 'cmt',
            'cmt': cmt,
            'rolls': rolls,
            'form': form,
            'impacts': impacts,
            'page_obj': page,
        },
        status=status,
    )


@payment_access
@require_GET
def defect_detail(request, pk):
    defect = get_object_or_404(
        FabricDefect.objects.select_related('cmt', 'recorded_by', 'cancelled_by'), pk=pk
    )
    items = list(
        defect.items.select_related(
            'roll__material', 'roll__color', 'invoice', 'vendor', 'po', 'returned_by'
        )
    )
    invoices = {
        invoice.pk: invoice
        for invoice in payment_queryset(
            {
                'q': '',
                'status': '',
                'date_from': None,
                'date_to': None,
            }
        ).filter(pk__in={item.invoice_id for item in items})
    }
    for invoice in invoices.values():
        invoice.summary = payment_summary(invoice)
    return render(
        request,
        'defect_detail.html',
        {
            'title': defect.number,
            'active': 'cmt',
            'defect': defect,
            'items': items,
            'invoices': invoices.values(),
            'today': timezone.localdate(),
            'can_correct_bs': request.user.is_superuser or request.user.role == 'admin',
        },
    )


@payment_access
@require_POST
def defect_return(request, pk):
    require_purchasing(request.user)
    item = get_object_or_404(FabricDefectItem, pk=pk)
    try:
        return_defect_item(
            item, input_date(request.POST.get('date')), request.POST.get('note', ''), request.user
        )
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    except DatabaseError:
        messages.error(request, 'Retur belum tersimpan. Data sedang diproses; coba lagi.')
    else:
        messages.success(
            request, 'Retur fisik tercatat. Potongan dan penerimaan uang tidak berubah.'
        )
    return redirect('defect_detail', pk=item.defect_id)


@payment_access
@require_POST
def defect_cancel(request, pk):
    defect = get_object_or_404(FabricDefect, pk=pk)
    try:
        cancel_defect(defect, request.POST.get('reason', ''), request.user)
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    except DatabaseError:
        messages.error(request, 'Pembatalan belum tersimpan. Coba lagi.')
    else:
        messages.success(
            request, 'BS dibatalkan. Yard dan tagihan dihitung kembali; histori disimpan.'
        )
    return redirect('defect_detail', pk=pk)


@payment_access
@require_http_methods(['GET', 'POST'])
def refund_create(request, pk):
    if not can_receive_refund(request.user):
        raise PermissionDenied
    invoice = get_object_or_404(Invoice.objects.select_related('vendor'), pk=pk)
    summary = payment_summary(invoice)
    form = RefundForm(
        request.POST if request.method == 'POST' else None,
        initial={
            'request_id': uuid4().hex,
            'amount': summary.refund_remaining,
        },
    )
    status = 200
    if request.method == 'POST':
        status = 400
        if form.is_valid():
            try:
                record_refund(invoice, user=request.user, **form.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, exc)
            except OperationalError:
                form.add_error(None, 'Penerimaan lain sedang diproses. Periksa sisa terbaru.')
                status = 409
            except DatabaseError:
                form.add_error(None, 'Refund belum tersimpan. Isian tetap tersedia; coba lagi.')
                status = 503
            else:
                messages.success(request, 'Penerimaan refund tersimpan pada invoice asal.')
                return redirect('payment_detail', pk=pk)
        summary = payment_summary(invoice)
    return render(
        request,
        'refund_form.html',
        {
            'title': 'Catat Refund Diterima',
            'active': 'payment',
            'invoice': invoice,
            'form': form,
            'summary': summary,
        },
        status=status,
    )


@payment_access
@require_POST
def refund_cancel(request, pk):
    refund = get_object_or_404(VendorRefund, pk=pk)
    try:
        cancel_refund(refund, request.POST.get('reason', ''), request.user)
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    except DatabaseError:
        messages.error(request, 'Koreksi penerimaan belum tersimpan. Coba lagi.')
    else:
        messages.success(request, 'Penerimaan refund dibatalkan dengan jejak koreksi.')
    return redirect('payment_detail', pk=refund.invoice_id)
