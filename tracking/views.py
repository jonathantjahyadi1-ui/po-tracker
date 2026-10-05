import json
import re
from collections import defaultdict
from datetime import date
from functools import wraps
from hashlib import sha256
from io import BytesIO
from uuid import uuid4
from zipfile import BadZipFile

from django.contrib import messages
from django.contrib.auth import login, logout
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q, Sum
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import content_disposition_header, url_has_allowed_host_and_scheme
from openpyxl import Workbook
from openpyxl.utils.exceptions import InvalidFileException

from .forms import AccountForm, AssignPoForm, InvoiceForm, LoginForm, PoForm, ReceiveForm
from .models import (
    Alokasi,
    Invoice,
    InvoiceAttachment,
    KirimGudang,
    Log,
    Master,
    Po,
    Roll,
    User,
)
from .parsers import import_yards, parse_yards
from .services import (
    ajukan_alokasi,
    batalkan_invoice,
    hitung_pemakaian,
    kirim_alokasi_legacy,
    kirim_gudang,
    log,
    map_legacy_production,
    pindah_status,
    po_rolls,
    po_status,
    po_status_many,
    putuskan_alokasi,
    roll_count,
    simpan_hasil,
    simpan_invoice,
    tautkan_po,
    terima_alokasi,
    ubah_produk_po,
    yard_total,
)


def access(*roles):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect(f'/login/?next={request.path}')
            if request.user.role not in roles:
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorate


def write_only(request):
    if request.user.role != 'purchasing':
        raise PermissionDenied


def flash_error(request, error):
    messages.error(request, '; '.join(error.messages))


def input_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError('Tanggal wajib diisi dengan benar.')


def input_pcs(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError('Jumlah pcs harus berupa angka bulat.')


def safe_back(request, fallback):
    value = request.POST.get('back') or request.GET.get('back') or fallback
    if (
        value.startswith('/')
        and not value.startswith('//')
        and url_has_allowed_host_and_scheme(
            value, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
    ):
        return value
    return fallback


def allocation_display(allocation, rolls):
    """Make unsent legacy approval explicit rather than infer a fabric shipment."""
    if allocation.status == 'disetujui' and any(roll.status == 'siap_kirim' for roll in rolls):
        partly_sent = any(roll.tgl_kirim for roll in rolls)
        allocation.status_key = 'dikirim' if partly_sent else 'siap_kirim'
        allocation.status_label = (
            'Dikirim sebagian (data lama)' if partly_sent else 'Siap kirim (data lama)'
        )


def health(request):
    return HttpResponse('ok', content_type='text/plain')


def sign_in(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    username = request.POST.get('username', '').casefold()
    identity = f'{request.META.get("REMOTE_ADDR", "")}:{username}'
    key = f'login:{sha256(identity.encode()).hexdigest()}'
    if request.method == 'POST' and cache.get(key, 0) >= 5:
        messages.error(request, 'Terlalu banyak percobaan. Coba lagi dalam 15 menit.')
        return render(request, 'login.html', {'form': LoginForm(), 'title': 'Masuk'}, status=429)
    form = LoginForm(request, data=request.POST or None)
    if request.method == 'POST':
        if form.is_valid():
            cache.delete(key)
            login(request, form.get_user())
            return redirect('accounts' if form.get_user().role == 'admin' else 'dashboard')
        cache.set(key, cache.get(key, 0) + 1, 900)
    return render(request, 'login.html', {'form': form, 'title': 'Masuk'})


@access('purchasing', 'direktur', 'admin')
def sign_out(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    logout(request)
    return redirect('login')


def balance_flags(pos):
    summaries = po_status_many(pos)
    for po in pos:
        po.summary = summaries[po.pk]
        po.balance = po.summary['is_done']
    return pos


@access('purchasing', 'direktur')
def dashboard(request):
    stock = Roll.objects.filter(status='tersedia', invoice__dibatalkan=False).aggregate(
        rolls=Count('id'), yards=Sum('yard')
    )
    waiting = (
        Alokasi.objects.filter(status='menunggu')
        .select_related('po', 'cmt')
        .annotate(rolls=Count('roll'))
    )
    pos = balance_flags(list(Po.objects.select_related('cmt').order_by('-id')))
    unbalanced = [po for po in pos if po.summary['code'] == 'kurang_kirim']
    return render(
        request,
        'dashboard.html',
        {
            'title': 'Ringkasan',
            'active': 'dashboard',
            'stock': stock,
            'waiting_count': len(waiting),
            'waiting_rolls': sum(a.rolls for a in waiting),
            'shipped_count': Roll.objects.filter(status='dikirim').count(),
            'without_po_count': Alokasi.objects.filter(status='disetujui', po__isnull=True).count(),
            'legacy_ready_count': Roll.objects.filter(status='siap_kirim').count(),
            'unbalanced_count': len(unbalanced),
            'waiting': waiting[:5],
            'unbalanced': unbalanced[:5],
            'remaining_pcs': sum(po.summary['remaining'] for po in pos),
            'incomplete_count': sum(
                bool(po.summary['missing_results']) or po.summary['unmapped_material'] for po in pos
            ),
        },
    )


@access('purchasing', 'direktur')
def master_page(request):
    if request.method == 'POST':
        write_only(request)
        kind = request.POST.get('kind', '')
        name = ' '.join(request.POST.get('name', '').split())
        if kind not in Master.Kind.values or not name:
            messages.error(request, 'Jenis dan nama wajib diisi.')
        else:
            try:
                with transaction.atomic():
                    pk = request.POST.get('id')
                    item = get_object_or_404(Master, pk=pk, kind=kind) if pk else Master(kind=kind)
                    if request.POST.get('action') == 'toggle':
                        item.active = not item.active
                    else:
                        if (
                            Master.objects.filter(kind=kind, name__iexact=name)
                            .exclude(pk=item.pk)
                            .exists()
                        ):
                            raise ValidationError('Nama sudah ada dalam daftar ini.')
                        item.name = name
                    item.save()
                    log(request.user, 'ubah master', f'{item.get_kind_display()} {item.name}')
                    messages.success(request, 'Master tersimpan.')
            except ValidationError as error:
                flash_error(request, error)
        return redirect(f'/master/?tab={kind}')
    tab = request.GET.get('tab', 'vendor')
    if tab not in Master.Kind.values:
        tab = 'vendor'
    return render(
        request,
        'master.html',
        {
            'title': 'Master',
            'active': 'master',
            'tab': tab,
            'kinds': Master.Kind.choices,
            'items': Master.objects.filter(kind=tab),
        },
    )


@access('purchasing', 'direktur')
def vendor_list(request):
    vendors = list(Master.objects.filter(kind='vendor', active=True))
    totals = (
        Roll.objects.filter(invoice__dibatalkan=False)
        .values('invoice__vendor_id')
        .annotate(
            rolls=Count('id'),
            available=Count('id', filter=Q(status='tersedia')),
            allocated=Count('id', filter=~Q(status='tersedia')),
            last_send=Max('tgl_kirim'),
        )
    )
    by_vendor = {row['invoice__vendor_id']: row for row in totals}
    material_rows = (
        Roll.objects.filter(invoice__dibatalkan=False)
        .values('invoice__vendor_id', 'material__name')
        .distinct()
    )
    materials = defaultdict(list)
    for row in material_rows:
        materials[row['invoice__vendor_id']].append(row['material__name'])
    for vendor in vendors:
        vendor.summary = by_vendor.get(vendor.pk, {})
        vendor.materials = ', '.join(materials[vendor.pk])
    return render(
        request,
        'vendor_list.html',
        {
            'title': 'Vendor',
            'active': 'vendor',
            'vendors': vendors,
        },
    )


@access('purchasing', 'direktur')
def vendor_detail(request, pk):
    vendor = get_object_or_404(Master, pk=pk, kind='vendor')
    q = request.GET.get('q', '').strip()
    queryset = Invoice.objects.filter(vendor=vendor, dibatalkan=False)
    if q:
        queryset = queryset.filter(nomor__icontains=q)
    page_obj = Paginator(
        queryset.order_by('-tanggal', '-id').prefetch_related(
            Prefetch(
                'roll_set',
                queryset=Roll.objects.select_related('material', 'color', 'alokasi__cmt'),
            )
        ),
        20,
    ).get_page(request.GET.get('page'))
    invoices = list(page_obj.object_list)
    grouped_yards = {
        (row['invoice_id'], row['material_id'], row['color_id']): row['yard']
        for row in Roll.objects.filter(invoice_id__in=[item.pk for item in invoices])
        .values('invoice_id', 'material_id', 'color_id')
        .annotate(yard=Sum('yard'))
    }
    for invoice in invoices:
        groups = {}
        for roll in invoice.roll_set.all():
            key = (roll.material_id, roll.color_id)
            row = groups.setdefault(
                key,
                {
                    'material': roll.material,
                    'color': roll.color,
                    'rolls': 0,
                    'yard': 0,
                    'available': 0,
                    'pending': defaultdict(int),
                    'sent': defaultdict(lambda: defaultdict(int)),
                },
            )
            row['rolls'] += 1
            if roll.status == 'tersedia':
                row['available'] += 1
            elif roll.status in ('menunggu', 'siap_kirim') and roll.alokasi_id:
                row['pending'][roll.alokasi.cmt.name] += 1
            elif roll.tgl_kirim and roll.alokasi_id:
                row['sent'][roll.tgl_kirim][roll.alokasi.cmt.name] += 1
        invoice.rows = list(groups.values())
        for row in invoice.rows:
            row['yard'] = grouped_yards[(invoice.pk, row['material'].pk, row['color'].pk)]
            row['pending'] = dict(row['pending'])
            row['sent'] = {date: dict(counts) for date, counts in row['sent'].items()}
        invoice.dates = sorted({date for row in invoice.rows for date in row['sent']})
        invoice.yards = sum((row['yard'] for row in invoice.rows), 0)
        invoice.roll_count = sum(row['rolls'] for row in invoice.rows)
    return render(
        request,
        'vendor_detail.html',
        {
            'title': vendor.name,
            'active': 'vendor',
            'vendor': vendor,
            'invoices': invoices,
            'q': q,
            'invoice_count': page_obj.paginator.count,
            'page_obj': page_obj,
            'back': request.get_full_path(),
        },
    )


def draft_groups(request, process_details=True):
    """Retain every entered value, including invalid rows, for server validation."""
    if 'group_count' in request.POST or any(
        re.fullmatch(r'groups-\d+-material', key) for key in request.POST
    ):
        indices = sorted(
            {
                int(match.group(1))
                for key in request.POST
                if (match := re.fullmatch(r'groups-(\d+)-material', key))
            }
        )
        groups = []
        for index in indices:
            prefix = f'groups-{index}'
            row_indices = sorted(
                {
                    int(match.group(1))
                    for key in request.POST
                    if (match := re.fullmatch(rf'{prefix}-rows-(\d+)-color', key))
                }
            )
            rows = []
            for row_index in row_indices:
                key = f'{prefix}-rows-{row_index}'
                rows.append(
                    {
                        'color': request.POST.get(f'{key}-color', ''),
                        'lokasi': request.POST.get(f'{key}-lokasi', ''),
                        'yards_text': request.POST.get(f'{key}-yards', ''),
                        'upload': request.FILES.get(f'file-{index}-{row_index}'),
                        'errors': [],
                    }
                )
            groups.append(
                {
                    'material': request.POST.get(f'{prefix}-material', ''),
                    'rows': rows,
                    'errors': [],
                }
            )
        return groups
    # Existing callers may still submit the flat input format; PO input is ignored.
    groups = []
    for index, material in enumerate(request.POST.getlist('material[]')):

        def value(name):
            values = request.POST.getlist(f'{name}[]')
            return values[index] if index < len(values) else ''

        details_text = value('roll_details')
        if details_text and process_details:
            yards, parse_errors = parse_yards(value('yards'))
            try:
                details = json.loads(details_text)
            except (ValueError, TypeError):
                details = None
            if not isinstance(details, list) or len(details) != len(yards):
                raise ValidationError('Jumlah detail roll tidak sesuai daftar yard.')
            if parse_errors:
                raise ValidationError(parse_errors)
            panels = {}
            for at, yard in enumerate(yards):
                detail = details[at]
                if not isinstance(detail, dict) or any(
                    not isinstance(detail.get(key, ''), str)
                    for key in ('material', 'color', 'lokasi')
                ):
                    raise ValidationError(f'Detail roll {at + 1} tidak valid.')
                actual_material = detail.get('material', '').strip() or material
                actual_color = detail.get('color', '').strip() or value('color')
                actual_location = detail.get('lokasi', '').strip() or value('lokasi')
                panel = panels.setdefault(
                    actual_material.casefold(),
                    {
                        'material': actual_material,
                        'rows': {},
                        'errors': [],
                    },
                )
                row = panel['rows'].setdefault(
                    (actual_color.casefold(), actual_location.casefold()),
                    {
                        'color': actual_color,
                        'lokasi': actual_location,
                        'yards_text': '',
                        'errors': [],
                    },
                )
                row['yards_text'] += f'{yard}\n'
            for panel in panels.values():
                panel['rows'] = list(panel['rows'].values())
                groups.append(panel)
            upload = request.FILES.get(f'file-{index}')
            if upload:
                groups.append(
                    {
                        'material': material,
                        'rows': [
                            {
                                'color': value('color'),
                                'lokasi': value('lokasi'),
                                'yards_text': '',
                                'upload': upload,
                                'errors': [],
                            }
                        ],
                        'errors': [],
                    }
                )
            continue
        groups.append(
            {
                'material': material,
                'rows': [
                    {
                        'color': value('color'),
                        'lokasi': value('lokasi'),
                        'yards_text': value('yards'),
                        'upload': request.FILES.get(f'file-{index}'),
                        'errors': [],
                    }
                ],
                'errors': [],
            }
        )
    return groups


def posted_groups(request, drafts=None):
    drafts = drafts if drafts is not None else draft_groups(request)
    groups, errors = [], []
    for index, draft in enumerate(drafts):
        material = draft['material'].strip()
        if not material and not any(
            row['color'].strip() or row['yards_text'].strip() or row.get('upload')
            for row in draft['rows']
        ):
            continue
        if not material or len(material) > 160:
            draft['errors'].append('Nama bahan wajib diisi, maksimal 160 karakter.')
        if not draft['rows']:
            draft['errors'].append('Tambahkan minimal satu baris warna.')
        rows = []
        for at, row in enumerate(draft['rows']):
            color = row['color'].strip()
            location = row['lokasi'].strip()
            yards, row_errors = parse_yards(row['yards_text'])
            if row.get('upload'):
                try:
                    if row['upload'].size > 10 * 1024 * 1024:
                        raise ValueError('File yard maksimal 10 MB.')
                    imported, file_errors = import_yards(row['upload'])
                    yards.extend(imported)
                    row_errors.extend(file_errors)
                    # Uploaded values remain editable if another field fails validation.
                    row['yards_text'] += '\n' + '\n'.join(str(yard) for yard in imported)
                except (
                    ValueError,
                    UnicodeError,
                    OSError,
                    KeyError,
                    BadZipFile,
                    InvalidFileException,
                ) as error:
                    row_errors.append(f'File yard tidak dapat dibaca: {error}')
            if not color or len(color) > 160:
                row_errors.append('Warna wajib diisi, maksimal 160 karakter.')
            if len(location) > 160:
                row_errors.append('Lokasi maksimal 160 karakter.')
            if not yards:
                row_errors.append('Masukkan minimal satu yard per roll yang positif.')
            row['errors'] = row_errors
            errors.extend(f'Bahan {index + 1}, baris {at + 1}: {error}' for error in row_errors)
            rows.append({'color': color, 'lokasi': location, 'yards': yards})
        errors.extend(f'Bahan {index + 1}: {error}' for error in draft['errors'])
        groups.append({'material': material, 'rows': rows})
    if len(groups) > 10:
        errors.append('Satu invoice maksimal berisi 10 panel bahan; baris warna tidak dibatasi 10.')
    if not groups and not request.POST.get('existing'):
        errors.append('Tambahkan minimal satu nama bahan beserta warna dan yard per roll.')
    if errors:
        raise ValidationError(errors)
    return groups


@access('purchasing')
def invoice_yard_preview(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    upload = request.FILES.get('file')
    if not upload:
        return JsonResponse({'errors': ['Pilih file CSV atau XLSX.']}, status=400)
    if upload.size > 10 * 1024 * 1024:
        return JsonResponse({'errors': ['File yard maksimal 10 MB.']}, status=400)
    try:
        yards, errors = import_yards(upload)
    except (ValueError, UnicodeError, OSError, KeyError, BadZipFile, InvalidFileException):
        return JsonResponse(
            {'errors': ['File yard tidak dapat dibaca. Periksa format file.']}, status=400
        )
    return JsonResponse(
        {
            'yards': [str(yard) for yard in yards],
            'roll_count': len(yards),
            'total_yard': str(sum(yards)),
            'errors': errors,
        },
        status=400 if errors else 200,
    )


@access('purchasing')
def invoice_edit(request, pk=None):
    invoice = get_object_or_404(Invoice, pk=pk) if pk else None
    form_error = False
    existing = (
        list(
            invoice.roll_set.select_related(
                'material', 'color', 'lokasi', 'invoice_po__po'
            ).order_by('material__name', 'color__name', 'urut')
        )
        if invoice
        else []
    )
    initial = (
        {
            key: getattr(invoice, key)
            for key in InvoiceForm.base_fields
            if key not in ('vendor', 'invoice_file')
        }
        if invoice
        else {}
    )
    if invoice:
        initial['vendor'] = invoice.vendor.name
    form = InvoiceForm(request.POST or None, request.FILES or None, initial=initial)
    draft_error = None
    try:
        input_groups = draft_groups(request) if request.method == 'POST' else []
    except ValidationError as error:
        input_groups = draft_groups(request, process_details=False)
        draft_error = error
    locked_metadata = any(roll.status != 'tersedia' or roll.alokasi_id for roll in existing)
    if locked_metadata:
        for key in ('nomor', 'tanggal'):
            form.fields[key].widget.attrs['readonly'] = True
    for roll in existing:
        prefix = f'existing-{roll.pk}'
        roll.edit_material = request.POST.get(f'{prefix}-material', roll.material.name)
        roll.edit_color = request.POST.get(f'{prefix}-color', roll.color.name)
        roll.edit_lokasi = request.POST.get(
            f'{prefix}-lokasi', roll.lokasi.name if roll.lokasi_id else ''
        )
        roll.edit_yard = request.POST.get(prefix, str(roll.yard))
    if request.method == 'POST':
        write_only(request)
        try:
            if draft_error:
                raise draft_error
            groups = posted_groups(request, input_groups)
            if not form.is_valid():
                raise ValidationError('Periksa data invoice yang ditandai.')
            with transaction.atomic():
                invoice_data = {
                    **form.cleaned_data,
                    'request_id': request.POST.get('request_id') or None,
                    'existing_roll_input': {
                        key: value
                        for key, value in request.POST.items()
                        if key.startswith('existing-')
                    },
                }
                saved = simpan_invoice(invoice_data, groups, request.user, invoice)
            messages.success(request, 'Invoice tersimpan.')
            return redirect('invoice_detail', pk=saved.pk)
        except ValidationError as error:
            flash_error(request, error)
            form_error = True
    return render(
        request,
        'invoice_form.html',
        {
            'title': 'Ubah invoice' if invoice else 'Tambah bahan',
            'active': 'vendor',
            'form': form,
            'invoice': invoice,
            'attachment': (
                InvoiceAttachment.objects.filter(invoice=invoice).defer('content').first()
                if invoice
                else None
            ),
            'existing': existing,
            'input_groups': input_groups,
            'vendors': Master.objects.filter(kind='vendor', active=True),
            'materials': Master.objects.filter(kind='material', active=True),
            'colors': Master.objects.filter(kind='color', active=True),
            'warehouses': Master.objects.filter(kind='warehouse', active=True),
            'form_error': form_error,
            'locked_metadata': locked_metadata,
            'existing_material_count': invoice.material_groups.count() if invoice else 0,
            'request_id': request.POST.get('request_id') or uuid4().hex,
        },
    )


@access('purchasing', 'direktur')
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related('vendor'), pk=pk)
    rolls = list(
        invoice.roll_set.select_related(
            'material', 'color', 'lokasi', 'invoice_po__po', 'alokasi__po', 'alokasi__cmt'
        ).order_by('invoice_po__urut', 'material__name', 'color__name', 'urut')
    )
    attachment = InvoiceAttachment.objects.filter(invoice=invoice).defer('content').first()
    return render(
        request,
        'invoice_detail.html',
        {
            'title': invoice.nomor,
            'active': 'vendor',
            'invoice': invoice,
            'attachment': attachment,
            'material_count': len({roll.material_id for roll in rolls}),
            'rolls': rolls,
            'roll_count': roll_count(invoice.roll_set.all()),
            'yard_total': yard_total(invoice.roll_set.all()),
            'cmts': Master.objects.filter(kind='cmt', active=True),
            'back': safe_back(request, f'/vendor/{invoice.vendor_id}/'),
            'back_url': safe_back(request, f'/vendor/{invoice.vendor_id}/'),
        },
    )


@access('purchasing', 'direktur')
def invoice_attachment(request, pk):
    attachment = get_object_or_404(InvoiceAttachment, invoice_id=pk)
    response = HttpResponse(attachment.content, content_type=attachment.content_type)
    response['Content-Disposition'] = content_disposition_header(True, attachment.filename)
    response['Content-Length'] = attachment.size
    response['Cache-Control'] = 'private, no-store'
    return response


@access('purchasing', 'direktur')
def invoice_cancel(request, pk):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    write_only(request)
    invoice = get_object_or_404(Invoice, pk=pk)
    try:
        batalkan_invoice(invoice, request.user)
        messages.success(request, 'Invoice dibatalkan.')
        return redirect('vendor_detail', pk=invoice.vendor_id)
    except ValidationError as error:
        flash_error(request, error)
        return redirect('invoice_detail', pk=pk)


@access('purchasing', 'direktur')
def allocation_create(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    write_only(request)
    try:
        cmt = get_object_or_404(Master, pk=request.POST.get('cmt'), kind='cmt')
        allocation = ajukan_alokasi(request.POST.getlist('roll'), cmt, request.user)
        messages.success(
            request, f'{allocation.roll_set.count()} roll berhasil diajukan ke {cmt.name}.'
        )
        return redirect('allocation_detail', pk=allocation.pk)
    except (ValidationError, ValueError) as error:
        messages.error(request, str(error))
        return redirect(safe_back(request, '/alokasi/'))


@access('purchasing', 'direktur')
def allocation_list(request):
    status = request.GET.get('status', '')
    rows = (
        Alokasi.objects.select_related('po', 'cmt')
        .prefetch_related('items__roll__invoice__vendor')
        .annotate(roll_count=Count('items'), yard_total=Sum('items__roll__yard'))
        .order_by('-id')
    )
    if status in Alokasi.Status.values:
        rows = rows.filter(status=status)
    page_obj = Paginator(rows, 30).get_page(request.GET.get('page'))
    rows = list(page_obj.object_list)
    for row in rows:
        source_rolls = [item.roll for item in row.items.all()]
        allocation_display(row, source_rolls)
        row.invoice_labels = ', '.join(dict.fromkeys(roll.invoice.nomor for roll in source_rolls))
        row.vendor_labels = ', '.join(
            dict.fromkeys(roll.invoice.vendor.name for roll in source_rolls)
        )
    return render(
        request,
        'allocation_list.html',
        {
            'title': 'Alokasi',
            'active': 'allocation',
            'rows': rows,
            'status': status,
            'statuses': Alokasi.Status.choices,
            'page_obj': page_obj,
        },
    )


@access('purchasing', 'direktur')
def allocation_detail(request, pk):
    allocation = get_object_or_404(Alokasi.objects.select_related('po', 'cmt'), pk=pk)
    if request.method == 'POST':
        write_only(request)
        action = request.POST.get('action', '')
        try:
            if action in ('acc', 'tolak', 'batalkan'):
                putuskan_alokasi(
                    allocation,
                    action,
                    request.user,
                    request.POST.get('alasan', ''),
                    tgl_kirim=input_date(request.POST.get('tanggal')) if action == 'acc' else None,
                    sj_kirim=request.POST.get('surat_jalan', ''),
                )
            elif action == 'kirim':
                ids = request.POST.getlist('roll')
                tanggal = input_date(request.POST.get('tanggal'))
                kirim_alokasi_legacy(
                    allocation,
                    ids,
                    tanggal,
                    request.user,
                    sj_kirim=request.POST.get('surat_jalan', ''),
                )
            else:
                raise ValidationError('Aksi tidak dikenal.')
            messages.success(request, 'Alokasi diperbarui.')
        except ValidationError as error:
            flash_error(request, error)
        return redirect('allocation_detail', pk=pk)
    rolls = [
        item.roll
        for item in allocation.items.select_related(
            'roll__invoice__vendor', 'roll__material', 'roll__color'
        ).order_by('roll__invoice_id', 'roll__material__name', 'roll__color__name', 'roll__urut')
    ]
    allocation_display(allocation, rolls)
    return render(
        request,
        'allocation_detail.html',
        {
            'title': f'Alokasi #{pk}',
            'active': 'allocation',
            'allocation': allocation,
            'rolls': rolls,
            'roll_count': len(rolls),
            'yard_total': sum((roll.yard for roll in rolls), 0),
            'today': timezone.localdate(),
            'legacy_ready': any(roll.status == 'siap_kirim' for roll in rolls),
            'has_legacy_ready': any(roll.status == 'siap_kirim' for roll in rolls),
            'can_cancel': allocation.status in ('menunggu', 'disetujui')
            and not allocation.po_id
            and bool(rolls)
            and all(roll.status in ('menunggu', 'siap_kirim') for roll in rolls),
            'default_ship_date': timezone.localdate().isoformat(),
        },
    )


@access('purchasing', 'direktur')
def cmt_list(request):
    cmts = list(Master.objects.filter(kind='cmt', active=True))
    status_counts = (
        Roll.objects.filter(alokasi__isnull=False)
        .values('alokasi__cmt_id', 'status')
        .annotate(total=Count('id'))
    )
    counts = defaultdict(dict)
    for row in status_counts:
        counts[row['alokasi__cmt_id']][row['status']] = row['total']
    po_counts = defaultdict(lambda: [0, 0])
    shortage_counts = defaultdict(int)
    for po in balance_flags(list(Po.objects.filter(cmt__isnull=False))):
        po_counts[po.cmt_id][0 if po.balance else 1] += 1
        shortage_counts[po.cmt_id] += po.summary['code'] == 'kurang_kirim'
    unassigned_counts = dict(
        Alokasi.objects.filter(status='disetujui', po__isnull=True)
        .values('cmt_id')
        .annotate(total=Count('pk'))
        .values_list('cmt_id', 'total')
    )
    for cmt in cmts:
        cmt.counts = counts[cmt.pk]
        cmt.at_cmt = sum(
            counts[cmt.pk].get(status, 0) for status in ('diterima', 'terpakai', 'rusak')
        )
        cmt.balance_count, cmt.unbalanced_count = po_counts[cmt.pk]
        cmt.po_count = sum(po_counts[cmt.pk])
        cmt.shortage_count = shortage_counts[cmt.pk]
        cmt.done_count = cmt.balance_count
        cmt.unassigned_count = unassigned_counts.get(cmt.pk, 0)
    return render(
        request,
        'cmt_list.html',
        {
            'title': 'CMT',
            'active': 'cmt',
            'cmts': cmts,
        },
    )


@access('purchasing', 'direktur')
def cmt_detail(request, pk):
    cmt = get_object_or_404(Master, pk=pk, kind='cmt')
    cmt_error = None
    posted = request.POST if request.method == 'POST' else {}
    if request.method == 'POST':
        write_only(request)
        action = request.POST.get('action', '')
        try:
            allocation_id = request.POST.get('allocation', '')
            if not allocation_id.isdigit():
                raise ValidationError('Pilih pengiriman bahan yang ingin diperbarui.')
            allocation = get_object_or_404(Alokasi, pk=allocation_id, cmt=cmt)
            if action == 'receive':
                receive_form = ReceiveForm(request.POST)
                if not receive_form.is_valid():
                    raise ValidationError('Tanggal terima bahan wajib diisi dengan benar.')
                terima_alokasi(
                    allocation,
                    request.POST.getlist('rolls') or request.POST.getlist('roll'),
                    receive_form.cleaned_data['tanggal'],
                    request.user,
                )
                messages.success(request, 'Penerimaan bahan tercatat.')
            elif action == 'assign_po':
                assign_form = AssignPoForm(request.POST)
                if not assign_form.is_valid():
                    raise ValidationError(
                        [
                            f'{assign_form.fields[key].label}: {error}'
                            for key, errors in assign_form.errors.items()
                            for error in errors
                        ]
                    )
                assigned_po = tautkan_po(
                    allocation,
                    assign_form.cleaned_data['nomor_po'],
                    request.user,
                    produk=assign_form.cleaned_data['produk'],
                )
                messages.success(
                    request, f'Nomor {assigned_po.nomor} terhubung ke pengiriman {allocation.pk}.'
                )
            elif action == 'product':
                if not allocation.po_id:
                    raise ValidationError('Isi nomor PO sebelum mengubah nama produk.')
                ubah_produk_po(allocation.po, request.POST.get('produk', ''), request.user, cmt=cmt)
                messages.success(request, 'Nama produk tersimpan.')
            else:
                raise ValidationError('Aksi CMT tidak dikenal.')
            page_query = ''
            for key in ('page', 'allocation'):
                if request.GET.get(key, '').isdigit():
                    page_query = f'?{key}={request.GET[key]}'
                    break
            return redirect(f'/cmt/{pk}/{page_query}#pengiriman-{allocation.pk}')
        except ValidationError as error:
            cmt_error = error.messages
            flash_error(request, error)
    allocation_queryset = (
        Alokasi.objects.filter(cmt=cmt)
        .select_related('po')
        .prefetch_related(
            'items__roll__invoice__vendor', 'items__roll__material', 'items__roll__color'
        )
        .order_by('-id')
    )
    page_number = request.GET.get('page')
    selected_allocation = request.GET.get('allocation', '')
    if (
        not page_number
        and selected_allocation.isdigit()
        and allocation_queryset.filter(pk=selected_allocation).exists()
    ):
        page_number = allocation_queryset.filter(pk__gt=selected_allocation).count() // 20 + 1
    page_obj = Paginator(allocation_queryset, 20).get_page(page_number)
    allocations = list(page_obj.object_list)
    for allocation in allocations:
        allocation.roll_list = [item.roll for item in allocation.items.all()]
        allocation.roll_count = len(allocation.roll_list)
        allocation.yard_total = sum((roll.yard for roll in allocation.roll_list), 0)
        allocation.received_count = sum(
            roll.status in ('diterima', 'terpakai', 'rusak') for roll in allocation.roll_list
        )
        allocation.shipped_count = sum(roll.status == 'dikirim' for roll in allocation.roll_list)
        allocation_display(allocation, allocation.roll_list)
        allocation.last_send = max(
            (roll.tgl_kirim for roll in allocation.roll_list if roll.tgl_kirim), default=None
        )
        allocation.receipt_label = (
            (
                'Diterima'
                if allocation.roll_count and allocation.received_count == allocation.roll_count
                else 'Diterima sebagian'
                if allocation.received_count
                else 'Dikirim'
            )
            if allocation.status == 'disetujui'
            else allocation.get_status_display()
        )
        if getattr(allocation, 'status_label', None) and allocation.received_count == 0:
            allocation.receipt_label = allocation.status_label
        allocation.receipt_key = (
            'diterima'
            if allocation.received_count == allocation.roll_count and allocation.roll_count
            else 'diterima_sebagian'
            if allocation.received_count
            else getattr(allocation, 'status_key', 'dikirim')
        )
        allocation.can_assign = (
            allocation.status == 'disetujui'
            and not allocation.po_id
            and bool(allocation.roll_list)
            and all(roll.status == 'diterima' for roll in allocation.roll_list)
        )
        allocation.material_labels = ', '.join(
            dict.fromkeys(
                f'{roll.material.name} / {roll.color.name}' for roll in allocation.roll_list
            )
        )
        allocation.invoice_labels = ', '.join(
            dict.fromkeys(roll.invoice.nomor for roll in allocation.roll_list)
        )
        allocation.vendor_labels = ', '.join(
            dict.fromkeys(roll.invoice.vendor.name for roll in allocation.roll_list)
        )
    pos = balance_flags(list(Po.objects.filter(cmt=cmt).order_by('-id')))
    return render(
        request,
        'cmt_detail.html',
        {
            'title': cmt.name,
            'active': 'cmt',
            'cmt': cmt,
            'allocations': allocations,
            'pos': pos,
            'today': timezone.localdate(),
            'cmt_error': cmt_error,
            'posted': posted,
            'selected_rolls': request.POST.getlist('rolls') if cmt_error else [],
            'page_obj': page_obj,
        },
    )


@access('purchasing', 'direktur')
def po_list(request):
    cmts = list(
        Master.objects.filter(kind='cmt')
        .filter(Q(active=True) | Q(purchase_orders__isnull=False))
        .distinct()
    )
    grouped = defaultdict(list)
    for po in balance_flags(list(Po.objects.filter(cmt__isnull=False))):
        grouped[po.cmt_id].append(po)
    for cmt in cmts:
        cmt.po_count = len(grouped[cmt.pk])
        cmt.short_count = sum(po.summary['code'] == 'kurang_kirim' for po in grouped[cmt.pk])
        cmt.shortage_count = cmt.short_count
    return render(
        request,
        'po_list.html',
        {
            'title': 'PO',
            'active': 'po',
            'cmts': cmts,
            'legacy_count': Po.objects.filter(cmt__isnull=True).count(),
        },
    )


def render_po_list(request, cmt=None, legacy=False):
    q = request.GET.get('q', '').strip()
    filter_value = request.GET.get('filter', '')
    queryset = Po.objects.filter(cmt=cmt).select_related('cmt').order_by('-id')
    if q:
        queryset = queryset.filter(Q(nomor__icontains=q) | Q(produk__icontains=q))
    pos = balance_flags(list(queryset))
    # Old list filters retain meaning when following bookmarked URLs.
    filter_value = {'selesai': 'done', 'balance': 'done', 'belum': 'kurang_kirim'}.get(
        filter_value, filter_value
    )
    if filter_value:
        pos = [po for po in pos if po.summary['code'] == filter_value]
    page_obj = Paginator(pos, 30).get_page(request.GET.get('page'))
    return render(
        request,
        'po_list.html',
        {
            'title': f'PO · {cmt.name}' if cmt else 'PO historis',
            'active': 'po',
            'cmt': cmt,
            'legacy': legacy,
            'pos': page_obj.object_list,
            'filter': filter_value,
            'q': q,
            'page_obj': page_obj,
            'po_count': len(pos),
            'status_choices': [
                ('kurang_kirim', 'Kurang kirim'),
                ('done', 'Done'),
                ('belum_ada_hasil', 'Belum ada hasil'),
                ('belum_lengkap', 'Belum lengkap'),
                ('lebih_kirim', 'Lebih kirim'),
                ('belum_ada_produksi', 'Belum ada hasil produksi'),
            ],
            'back': request.get_full_path(),
        },
    )


@access('purchasing', 'direktur')
def po_cmt_list(request, pk):
    return render_po_list(request, get_object_or_404(Master, pk=pk, kind='cmt'))


@access('purchasing', 'direktur')
def po_legacy_list(request):
    return render_po_list(request, legacy=True)


def po_rows(po, rolls):
    shipments = defaultdict(dict)
    dates = set()
    for item in KirimGudang.objects.filter(po=po).select_related('material', 'color'):
        pair = (item.material_id, item.color_id)
        shipments[pair][item.tanggal] = shipments[pair].get(item.tanggal, 0) + item.pcs
        dates.add(item.tanggal)
    summary = getattr(po, 'summary', None) or po_status(po)
    source_materials = defaultdict(dict)
    for roll in rolls:
        source_materials[roll.color_id][roll.material_id] = roll.material
    rows = []
    for status in summary['rows']:
        material_id = status['material_id']
        color_id = status['color_id']
        is_legacy = material_id is None
        selected = [
            roll for roll in rolls if roll.material_id == material_id and roll.color_id == color_id
        ]
        total_yard = None if is_legacy else sum((roll.yard for roll in selected), 0)
        rows.append(
            {
                **status,
                'row_key': f'{material_id or "legacy"}-{color_id}',
                'is_legacy': is_legacy,
                'material_label': (
                    'Bahan belum ditentukan' if is_legacy else status['material'].name
                ),
                'mapping_materials': sorted(
                    source_materials[color_id].values(),
                    key=lambda material: material.name.casefold(),
                ),
                'invoices': ', '.join(dict.fromkeys(roll.invoice.nomor for roll in selected)),
                'surat_jalan': ', '.join(
                    dict.fromkeys(
                        roll.invoice.surat_jalan for roll in selected if roll.invoice.surat_jalan
                    )
                ),
                'rolls': None if is_legacy else len(selected),
                'yard': total_yard,
                'sent': shipments[(material_id, color_id)],
                'pemakaian': (
                    None if is_legacy else hitung_pemakaian(total_yard, status['hasil'] or 0)
                ),
            }
        )
    return rows, sorted(dates)


@access('purchasing', 'direktur')
def po_detail(request, pk):
    po = get_object_or_404(Po.objects.select_related('cmt'), pk=pk)
    detail_error = None
    action = request.POST.get('action')
    form = PoForm(request.POST if action in ('edit', 'info') else None, instance=po)
    if request.method == 'POST':
        write_only(request)
        try:
            if action in ('edit', 'info'):
                if not form.is_valid():
                    raise ValidationError(
                        '; '.join(
                            f'{field}: {error}'
                            for field, errors in form.errors.items()
                            for error in errors
                        )
                    )
                with transaction.atomic():
                    locked = Po.objects.select_for_update().get(pk=po.pk)
                    if locked.tgl_order != form.cleaned_data['tgl_order']:
                        previous = locked.tgl_order
                        locked.tgl_order = form.cleaned_data['tgl_order']
                        locked.save(update_fields=['tgl_order'])
                        log(
                            request.user,
                            'ubah tanggal order PO',
                            po.nomor,
                            f'{previous or "belum diisi"} → {locked.tgl_order or "belum diisi"}',
                        )
            elif action == 'hasil':
                color = get_object_or_404(Master, pk=request.POST.get('color'), kind='color')
                material = (
                    get_object_or_404(Master, pk=request.POST.get('material'), kind='material')
                    if request.POST.get('material')
                    else None
                )
                simpan_hasil(
                    po, color, input_pcs(request.POST.get('pcs')), request.user, material=material
                )
            elif action == 'kirim':
                color = get_object_or_404(Master, pk=request.POST.get('color'), kind='color')
                material = (
                    get_object_or_404(Master, pk=request.POST.get('material'), kind='material')
                    if request.POST.get('material')
                    else None
                )
                warehouse_id = request.POST.get('gudang')
                warehouse = None
                if warehouse_id:
                    warehouse = get_object_or_404(
                        Master, pk=warehouse_id, kind='warehouse', active=True
                    )
                kirim_gudang(
                    po,
                    color,
                    {
                        'tanggal': input_date(request.POST.get('tanggal')),
                        'pcs': input_pcs(request.POST.get('pcs')),
                        'gudang': warehouse,
                        'surat_jalan': request.POST.get('surat_jalan', ''),
                        'catatan': request.POST.get('catatan', ''),
                        'request_id': request.POST.get('request_id') or None,
                    },
                    request.user,
                    material=material,
                )
            elif action == 'map_material':
                color = get_object_or_404(Master, pk=request.POST.get('color'), kind='color')
                if not request.POST.get('material'):
                    raise ValidationError('Pilih bahan untuk hasil dan kiriman lama.')
                material = get_object_or_404(
                    Master, pk=request.POST.get('material'), kind='material'
                )
                map_legacy_production(po, color, material, request.user)
            else:
                raise ValidationError('Aksi PO tidak dikenal.')
            messages.success(request, 'PO diperbarui.')
            back = safe_back(request, f'/po/cmt/{po.cmt_id}/' if po.cmt_id else '/po/historis/')
            from urllib.parse import urlencode

            return redirect(f'/po/{pk}/?{urlencode({"back": back})}')
        except (ValidationError, ValueError) as error:
            messages.error(request, str(error))
            detail_error = str(error)
            po.refresh_from_db()
    rolls = list(
        po_rolls(po)
        .select_related('invoice', 'material', 'color', 'alokasi__cmt')
        .order_by('material__name', 'color__name', 'invoice__nomor', 'urut')
    )
    po.summary = po_status(po)
    rows, dates = po_rows(po, rolls)
    for row in rows:
        row['input_hasil'] = (
            request.POST.get('pcs', '')
            if detail_error
            and action == 'hasil'
            and str(row['color_id']) == request.POST.get('color')
            and str(row['material_id']) == request.POST.get('material')
            else row['hasil']
        )
    totals = {
        'rolls': sum(row['rolls'] or 0 for row in rows),
        'yard': sum((row['yard'] or 0 for row in rows), 0),
        'hasil': po.summary['hasil'],
        'sent': {date: sum(row['sent'].get(date, 0) for row in rows) for date in dates},
        'shipped': po.summary['terkirim'],
        'remaining': po.summary['remaining'],
        'over': po.summary['over'],
    }
    return render(
        request,
        'po_detail.html',
        {
            'title': po.nomor,
            'active': 'po',
            'po': po,
            'summary': po.summary,
            'rolls': rolls,
            'rows': rows,
            'dates': dates,
            'totals': totals,
            'balance': po.summary['is_done'],
            'form': form,
            'colors': [row['color'] for row in rows],
            'production_materials': sorted(
                {
                    row['material_id']: row['material'] for row in rows if row['material_id']
                }.values(),
                key=lambda material: material.name.casefold(),
            ),
            'shipment_rows': [row for row in rows if row['material_id'] is not None],
            'legacy_rows': [row for row in rows if row['is_legacy']],
            'warehouses': Master.objects.filter(kind='warehouse', active=True),
            'tgl_masuk': min((roll.tgl_terima for roll in rolls if roll.tgl_terima), default=None),
            'materials': ', '.join(dict.fromkeys(roll.material.name for roll in rolls)),
            'shipments': KirimGudang.objects.filter(po=po)
            .select_related('material', 'color', 'gudang')
            .order_by('-tanggal', '-id'),
            'request_id': request.POST.get('request_id') or uuid4().hex,
            'posted': request.POST if detail_error else {},
            'detail_error': detail_error,
            'today': timezone.localdate(),
            'back': safe_back(request, f'/po/cmt/{po.cmt_id}/' if po.cmt_id else '/po/historis/'),
            'back_url': safe_back(
                request, f'/po/cmt/{po.cmt_id}/' if po.cmt_id else '/po/historis/'
            ),
        },
    )


@access('purchasing', 'direktur')
def roll_status(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    write_only(request)
    ids = request.POST.getlist('roll')
    action = request.POST.get('action', '')
    actions = {
        'terima': 'diterima',
        'terpakai': 'terpakai',
        'rusak': 'rusak',
        'kembali': 'tersedia',
        'koreksi': 'diterima',
        'batal_kirim': 'siap_kirim',
    }
    try:
        if action not in actions:
            raise ValidationError('Aksi roll tidak dikenal.')
        fields = {
            'alasan': request.POST.get('alasan', ''),
            'catatan': request.POST.get('alasan', ''),
        }
        if action == 'terima':
            fields['tgl_terima'] = input_date(request.POST.get('tanggal'))
        pindah_status(
            ids,
            actions[action],
            request.user,
            **fields,
        )
        messages.success(request, 'Status roll diperbarui.')
    except (ValidationError, ValueError) as error:
        messages.error(request, str(error))
    return redirect(request.META.get('HTTP_REFERER', '/'))


@access('admin')
def accounts(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'create':
                form = AccountForm(request.POST)
                if not form.is_valid():
                    raise ValidationError(
                        '; '.join(
                            f'{field}: {error}'
                            for field, errors in form.errors.items()
                            for error in errors
                        )
                    )
                data = form.cleaned_data
                user = User.objects.create_user(
                    username=data['username'], password=data['password'], role=data['role']
                )
            else:
                user = get_object_or_404(User, pk=request.POST.get('id'))
                if user.pk == request.user.pk:
                    raise ValidationError('Akun sendiri tidak dapat diubah di sini.')
                if action == 'toggle':
                    user.is_active = not user.is_active
                elif action == 'role':
                    if request.POST.get('role') not in User.Role.values:
                        raise ValidationError('Peran tidak dikenal.')
                    user.role = request.POST['role']
                elif action == 'password':
                    if len(request.POST.get('password', '')) < 6:
                        raise ValidationError('Kata sandi minimal 6 karakter.')
                    user.set_password(request.POST['password'])
                else:
                    raise ValidationError('Aksi akun tidak dikenal.')
                user.save()
            log(request.user, action, f'Akun {user.username}')
            messages.success(request, 'Akun diperbarui.')
        except (ValidationError, ValueError) as error:
            messages.error(request, str(error))
        return redirect('accounts')
    return render(
        request,
        'accounts.html',
        {
            'title': 'Akun',
            'active': 'accounts',
            'users': User.objects.order_by('username'),
            'form': AccountForm(),
            'roles': User.Role.choices,
        },
    )


@access('admin', 'direktur', 'purchasing')
def history(request):
    rows = Log.objects.select_related('user').order_by('-waktu')
    if request.GET.get('user', '').isdigit():
        rows = rows.filter(user_id=request.GET['user'])
    for key, lookup in [('from', 'waktu__date__gte'), ('to', 'waktu__date__lte')]:
        if request.GET.get(key):
            try:
                value = date.fromisoformat(request.GET[key])
            except ValueError:
                continue
            rows = rows.filter(**{lookup: value})
    return render(
        request,
        'history.html',
        {
            'title': 'Riwayat',
            'active': 'history',
            'rows': rows[:200],
            'users': User.objects.order_by('username'),
        },
    )


def download_xlsx(name, headings, records):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headings)
    for row in records:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{name}.xlsx"'
    return response


@access('purchasing', 'direktur')
def invoice_export(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    rows = (
        Roll.objects.filter(invoice=invoice)
        .select_related('material', 'color', 'invoice_po__po', 'alokasi__po', 'alokasi__cmt')
        .order_by('invoice_po__urut', 'material__name', 'color__name', 'urut')
    )
    return download_xlsx(
        f'invoice-{pk}',
        [
            'Invoice',
            'Bahan',
            'Warna',
            'Roll',
            'Yard',
            'Status',
            'No. pengiriman',
            'PO',
            'CMT',
            'Tanggal kirim',
            'Tanggal terima',
        ],
        [
            [
                invoice.nomor,
                roll.material.name,
                roll.color.name,
                roll.urut,
                roll.yard,
                roll.get_status_display(),
                roll.alokasi_id,
                roll.alokasi.po.nomor if roll.alokasi_id and roll.alokasi.po_id else '',
                roll.alokasi.cmt.name if roll.alokasi_id else '',
                roll.tgl_kirim,
                roll.tgl_terima,
            ]
            for roll in rows
        ],
    )


@access('purchasing', 'direktur')
def po_export(request, pk):
    po = get_object_or_404(Po.objects.select_related('cmt'), pk=pk)
    po.summary = po_status(po)
    rolls = list(po_rolls(po).select_related('invoice', 'material', 'color'))
    rows, dates = po_rows(po, rolls)
    return download_xlsx(
        f'po-{pk}',
        [
            'CMT',
            'PO',
            'Nama produk',
            'Tanggal order',
            'Tgl kirim gudang terakhir',
            'Status PO',
            'Invoice sumber',
            'Surat jalan invoice',
            'Bahan',
            'Warna',
            'Roll',
            'Total yard alokasi',
            'Hasil produksi (pcs)',
            'Total terkirim (pcs)',
            'Sisa kirim (pcs)',
            'Lebih kirim (pcs)',
            'Status bahan / warna',
            *[date.isoformat() for date in dates],
            'Pemakaian (yard/pcs)',
        ],
        [
            [
                po.cmt.name if po.cmt_id else 'Belum dipetakan',
                po.nomor,
                po.produk,
                po.tgl_order,
                po.summary['last_shipment'],
                po.summary['label'],
                row['invoices'],
                row['surat_jalan'],
                row['material_label'],
                row['color'].name,
                row['rolls'],
                row['yard'],
                row['hasil'],
                row['shipped'],
                row['remaining'],
                row['over'],
                row['status_label'],
                *[row['sent'].get(date, 0) for date in dates],
                row['pemakaian'] if row['pemakaian'] is not None else '',
            ]
            for row in rows
        ],
    )


def forbidden(request, exception):
    return render(
        request, 'error.html', {'title': 'Akses ditolak', 'text': 'Akses ditolak.'}, status=403
    )


def not_found(request, exception):
    return render(
        request,
        'error.html',
        {'title': 'Tidak ditemukan', 'text': 'Halaman tidak ditemukan.'},
        status=404,
    )


def server_error(request):
    return render(
        request,
        'error.html',
        {'title': 'Terjadi kesalahan', 'text': 'Terjadi kesalahan. Coba kembali.'},
        status=500,
    )
