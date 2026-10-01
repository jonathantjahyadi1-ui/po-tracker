from collections import defaultdict
from datetime import date
from functools import wraps
from hashlib import sha256
from io import BytesIO

from django.contrib import messages
from django.contrib.auth import login, logout
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q, Sum
from django.http import HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from openpyxl import Workbook

from .forms import AccountForm, InvoiceForm, LoginForm, PoForm
from .models import Alokasi, Hasil, Invoice, KirimGudang, Log, Master, Po, Roll, User
from .parsers import import_yards, parse_yards, suspicious_yards
from .services import (
    ajukan_alokasi,
    batalkan_invoice,
    hitung_done,
    hitung_pemakaian,
    kirim_gudang,
    log,
    pindah_status,
    po_balance,
    putuskan_alokasi,
    roll_count,
    selesaikan_po,
    simpan_hasil,
    simpan_invoice,
    ubah_roll_invoice,
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
    ids = [po.pk for po in pos]
    hasil = defaultdict(dict)
    kirim = defaultdict(dict)
    colors = defaultdict(set)
    for row in (
        Roll.objects.filter(alokasi__po_id__in=ids).values('alokasi__po_id', 'color_id').distinct()
    ):
        colors[row['alokasi__po_id']].add(row['color_id'])
    for row in Hasil.objects.filter(po_id__in=ids).values('po_id', 'color_id', 'pcs'):
        hasil[row['po_id']][row['color_id']] = row['pcs']
    for row in (
        KirimGudang.objects.filter(po_id__in=ids)
        .values('po_id', 'color_id')
        .annotate(total=Sum('pcs'))
    ):
        kirim[row['po_id']][row['color_id']] = row['total']
    for po in pos:
        po.balance = (
            bool(hasil[po.pk])
            and colors[po.pk] == set(hasil[po.pk])
            and all(pcs == kirim[po.pk].get(color, 0) for color, pcs in hasil[po.pk].items())
        )
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
    ready = Roll.objects.filter(status='siap_kirim').count()
    pos = balance_flags(list(Po.objects.filter(selesai=False).order_by('-id')))
    return render(
        request,
        'dashboard.html',
        {
            'title': 'Ringkasan',
            'active': 'dashboard',
            'stock': stock,
            'waiting_count': len(waiting),
            'waiting_rolls': sum(a.rolls for a in waiting),
            'ready': ready,
            'unbalanced_count': sum(not po.balance for po in pos),
            'waiting': waiting[:5],
            'unbalanced': [po for po in pos if not po.balance][:5],
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
    invoices = list(
        Invoice.objects.filter(vendor=vendor, dibatalkan=False)
        .order_by('-tanggal', '-id')
        .prefetch_related(
            Prefetch(
                'roll_set',
                queryset=Roll.objects.select_related('material', 'color', 'alokasi__cmt'),
            )
        )
    )
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
            elif roll.status in ('menunggu', 'siap_kirim'):
                row['pending'][roll.alokasi.cmt.name] += 1
            elif roll.tgl_kirim:
                row['sent'][roll.tgl_kirim][roll.alokasi.cmt.name] += 1
        invoice.rows = list(groups.values())
        for row in invoice.rows:
            row['yard'] = grouped_yards[(invoice.pk, row['material'].pk, row['color'].pk)]
            row['pending'] = dict(row['pending'])
            row['sent'] = {date: dict(counts) for date, counts in row['sent'].items()}
        invoice.dates = sorted({date for row in invoice.rows for date in row['sent']})
        invoice.yards = sum((row['yard'] for row in invoice.rows), 0)
    return render(
        request,
        'vendor_detail.html',
        {
            'title': vendor.name,
            'active': 'vendor',
            'vendor': vendor,
            'invoices': invoices,
        },
    )


def posted_groups(request):
    names = request.POST.getlist('material[]')
    colors = request.POST.getlist('color[]')
    locations = request.POST.getlist('lokasi[]')
    notes = request.POST.getlist('catatan[]')
    texts = request.POST.getlist('yards[]')
    groups = []
    for index, name in enumerate(names):
        text = texts[index] if index < len(texts) else ''
        upload = request.FILES.get(f'file-{index}')
        if not name.strip() and not text.strip() and not upload:
            continue
        yards, errors = parse_yards(text)
        if upload:
            imported, file_errors = import_yards(upload)
            yards.extend(imported)
            errors.extend(file_errors)
        if errors:
            raise ValidationError(errors)
        if not name.strip() or index >= len(colors) or not colors[index].strip():
            raise ValidationError(f'Bahan {index + 1}: bahan dan warna wajib diisi.')
        if not yards:
            raise ValidationError(f'Bahan {index + 1}: masukkan yard per roll.')
        groups.append(
            {
                'material': name,
                'color': colors[index],
                'lokasi': locations[index] if index < len(locations) else '',
                'catatan': notes[index] if index < len(notes) else '',
                'yards': yards,
                'suspicious': suspicious_yards(yards),
            }
        )
    if not groups and not request.POST.get('existing'):
        raise ValidationError('Tambahkan setidaknya satu bahan.')
    return groups


@access('purchasing', 'direktur')
def invoice_edit(request, pk=None):
    invoice = get_object_or_404(Invoice, pk=pk) if pk else None
    form_error = False
    existing = (
        list(
            invoice.roll_set.select_related('material', 'color', 'lokasi').order_by(
                'material__name', 'color__name', 'urut'
            )
        )
        if invoice
        else []
    )
    initial = (
        {key: getattr(invoice, key) for key in InvoiceForm.base_fields if key != 'vendor'}
        if invoice
        else {}
    )
    if invoice:
        initial['vendor'] = invoice.vendor.name
    form = InvoiceForm(request.POST or None, initial=initial)
    if request.method == 'POST':
        write_only(request)
        try:
            if not form.is_valid():
                raise ValidationError('Periksa data invoice yang ditandai.')
            groups = posted_groups(request)
            with transaction.atomic():
                if invoice:
                    ubah_roll_invoice(invoice, request.POST, request.user)
                saved = simpan_invoice(form.cleaned_data, groups, request.user, invoice)
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
            'existing': existing,
            'vendors': Master.objects.filter(kind='vendor', active=True),
            'materials': Master.objects.filter(kind='material', active=True),
            'colors': Master.objects.filter(kind='color', active=True),
            'warehouses': Master.objects.filter(kind='warehouse', active=True),
            'form_error': form_error,
        },
    )


@access('purchasing', 'direktur')
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related('vendor'), pk=pk)
    rolls = list(
        invoice.roll_set.select_related(
            'material', 'color', 'lokasi', 'alokasi__po', 'alokasi__cmt'
        ).order_by('material__name', 'color__name', 'urut')
    )
    return render(
        request,
        'invoice_detail.html',
        {
            'title': invoice.nomor,
            'active': 'vendor',
            'invoice': invoice,
            'rolls': rolls,
            'roll_count': roll_count(invoice.roll_set.all()),
            'yard_total': yard_total(invoice.roll_set.all()),
            'cmts': Master.objects.filter(kind='cmt', active=True),
            'pos': Po.objects.all(),
        },
    )


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
        allocation = ajukan_alokasi(
            request.POST.getlist('roll'), request.POST.get('po', ''), cmt, request.user
        )
        messages.success(request, 'Alokasi diajukan.')
        return redirect('allocation_detail', pk=allocation.pk)
    except (ValidationError, ValueError) as error:
        messages.error(request, str(error))
        return redirect(request.META.get('HTTP_REFERER', '/alokasi/'))


@access('purchasing', 'direktur')
def allocation_list(request):
    status = request.GET.get('status', '')
    rows = (
        Alokasi.objects.select_related('po', 'cmt')
        .annotate(roll_count=Count('roll'))
        .order_by('-id')
    )
    if status in Alokasi.Status.values:
        rows = rows.filter(status=status)
    return render(
        request,
        'allocation_list.html',
        {
            'title': 'Alokasi',
            'active': 'allocation',
            'rows': rows,
            'status': status,
            'statuses': Alokasi.Status.choices,
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
                putuskan_alokasi(allocation, action, request.user, request.POST.get('alasan', ''))
            elif action in ('kirim', 'terima'):
                ids = request.POST.getlist('roll')
                tanggal = input_date(request.POST.get('tanggal'))
                fields = {'tgl_kirim': tanggal, 'sj_kirim': request.POST.get('surat_jalan', '')}
                if action == 'terima':
                    fields = {'tgl_terima': tanggal}
                if Roll.objects.filter(pk__in=ids).exclude(alokasi=allocation).exists():
                    raise ValidationError('Roll tidak termasuk alokasi ini.')
                pindah_status(
                    ids, 'dikirim' if action == 'kirim' else 'diterima', request.user, **fields
                )
            else:
                raise ValidationError('Aksi tidak dikenal.')
            messages.success(request, 'Alokasi diperbarui.')
        except ValidationError as error:
            flash_error(request, error)
        return redirect('allocation_detail', pk=pk)
    rolls = allocation.roll_set.select_related('invoice', 'material', 'color').order_by(
        'invoice_id', 'material__name', 'color__name', 'urut'
    )
    return render(
        request,
        'allocation_detail.html',
        {
            'title': f'Alokasi #{pk}',
            'active': 'allocation',
            'allocation': allocation,
            'rolls': rolls,
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
    po_links = Alokasi.objects.filter(roll__isnull=False).values_list('cmt_id', 'po_id').distinct()
    po_status = {po.pk: po.balance for po in balance_flags(list(Po.objects.all()))}
    po_counts = defaultdict(lambda: [0, 0])
    for cmt_id, po_id in po_links:
        po_counts[cmt_id][0 if po_status[po_id] else 1] += 1
    for cmt in cmts:
        cmt.counts = counts[cmt.pk]
        cmt.at_cmt = sum(counts[cmt.pk].get(status, 0) for status in ('dikirim', 'diterima'))
        cmt.balance_count, cmt.unbalanced_count = po_counts[cmt.pk]
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
    allocations = (
        Alokasi.objects.filter(cmt=cmt)
        .select_related('po')
        .annotate(roll_count=Count('roll'))
        .order_by('-id')
    )
    pos = balance_flags(
        list(
            Po.objects.filter(
                id__in=Alokasi.objects.filter(cmt=cmt, roll__isnull=False).values('po_id')
            ).order_by('-id')
        )
    )
    return render(
        request,
        'cmt_detail.html',
        {
            'title': cmt.name,
            'active': 'cmt',
            'cmt': cmt,
            'allocations': allocations,
            'pos': pos,
        },
    )


@access('purchasing', 'direktur')
def po_list(request):
    filter_value = request.GET.get('filter', 'belum')
    pos = balance_flags(list(Po.objects.order_by('-id')))
    if filter_value == 'selesai':
        pos = [po for po in pos if po.selesai]
    elif filter_value == 'balance':
        pos = [po for po in pos if po.balance and not po.selesai]
    else:
        filter_value = 'belum'
        pos = [po for po in pos if not po.balance and not po.selesai]
    return render(
        request,
        'po_list.html',
        {
            'title': 'PO',
            'active': 'po',
            'pos': pos,
            'filter': filter_value,
        },
    )


def po_rows(po, rolls):
    colors = {roll.color_id: roll.color for roll in rolls}
    results = {item.color_id: item.pcs for item in Hasil.objects.filter(po=po)}
    roll_totals = {
        row['color_id']: row
        for row in Roll.objects.filter(alokasi__po=po)
        .values('color_id')
        .annotate(rolls=Count('id'), yard=Sum('yard'))
    }
    usage = dict(
        Roll.objects.filter(alokasi__po=po, status__in=['dikirim', 'diterima', 'terpakai'])
        .values('color_id')
        .annotate(total=Sum('yard'))
        .values_list('color_id', 'total')
    )
    shipments = defaultdict(dict)
    dates = set()
    for item in KirimGudang.objects.filter(po=po).select_related('color'):
        shipments[item.color_id][item.tanggal] = (
            shipments[item.color_id].get(item.tanggal, 0) + item.pcs
        )
        dates.add(item.tanggal)
    rows = []
    for color_id, color in colors.items():
        selected = [roll for roll in rolls if roll.color_id == color_id]
        result = results.get(color_id, 0)
        sent = sum(shipments[color_id].values())
        rows.append(
            {
                'color': color,
                'invoices': ', '.join(dict.fromkeys(roll.invoice.nomor for roll in selected)),
                'surat_jalan': ', '.join(
                    dict.fromkeys(
                        roll.invoice.surat_jalan for roll in selected if roll.invoice.surat_jalan
                    )
                ),
                'rolls': roll_totals[color_id]['rolls'],
                'yard': roll_totals[color_id]['yard'],
                'hasil': result,
                'sent': shipments[color_id],
                'done': hitung_done(result, sent),
                'pemakaian': hitung_pemakaian(usage.get(color_id, 0), result),
            }
        )
    return rows, sorted(dates)


@access('purchasing', 'direktur')
def po_detail(request, pk):
    po = get_object_or_404(Po, pk=pk)
    if request.method == 'POST':
        write_only(request)
        if po.selesai:
            raise PermissionDenied
        action = request.POST.get('action')
        try:
            if action == 'edit':
                form = PoForm(request.POST, instance=po)
                if not form.is_valid():
                    raise ValidationError(
                        '; '.join(
                            f'{field}: {error}'
                            for field, errors in form.errors.items()
                            for error in errors
                        )
                    )
                form.save()
                log(request.user, 'ubah PO', po.nomor)
            elif action == 'hasil':
                color = get_object_or_404(Master, pk=request.POST.get('color'), kind='color')
                simpan_hasil(po, color, input_pcs(request.POST.get('pcs')), request.user)
            elif action == 'kirim':
                color = get_object_or_404(Master, pk=request.POST.get('color'), kind='color')
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
                    },
                    request.user,
                )
            elif action == 'selesai':
                selesaikan_po(po, request.user)
            else:
                raise ValidationError('Aksi PO tidak dikenal.')
            messages.success(request, 'PO diperbarui.')
        except (ValidationError, ValueError) as error:
            messages.error(request, str(error))
        return redirect('po_detail', pk=pk)
    rolls = list(
        Roll.objects.filter(alokasi__po=po)
        .select_related('invoice', 'material', 'color', 'alokasi__cmt')
        .order_by('color__name', 'invoice__nomor', 'urut')
    )
    rows, dates = po_rows(po, rolls)
    totals = {
        'rolls': sum(row['rolls'] for row in rows),
        'yard': sum((row['yard'] for row in rows), 0),
        'hasil': sum(row['hasil'] for row in rows),
        'sent': {date: sum(row['sent'].get(date, 0) for row in rows) for date in dates},
        'done': sum(row['done'] for row in rows),
    }
    return render(
        request,
        'po_detail.html',
        {
            'title': po.nomor,
            'active': 'po',
            'po': po,
            'rolls': rolls,
            'rows': rows,
            'dates': dates,
            'totals': totals,
            'balance': po_balance(po),
            'form': PoForm(instance=po),
            'colors': [row['color'] for row in rows],
            'warehouses': Master.objects.filter(kind='warehouse', active=True),
            'tgl_masuk': min((roll.tgl_terima for roll in rolls if roll.tgl_terima), default=None),
            'materials': ', '.join(dict.fromkeys(roll.material.name for roll in rolls)),
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


@access('admin', 'direktur')
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
        .select_related('material', 'color', 'alokasi__po', 'alokasi__cmt')
        .order_by('material__name', 'color__name', 'urut')
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
                roll.alokasi.po.nomor if roll.alokasi_id else '',
                roll.alokasi.cmt.name if roll.alokasi_id else '',
                roll.tgl_kirim,
                roll.tgl_terima,
            ]
            for roll in rows
        ],
    )


@access('purchasing', 'direktur')
def po_export(request, pk):
    po = get_object_or_404(Po, pk=pk)
    rolls = list(Roll.objects.filter(alokasi__po=po).select_related('invoice', 'color'))
    rows, dates = po_rows(po, rolls)
    return download_xlsx(
        f'po-{pk}',
        [
            'PO',
            'Invoice',
            'Surat jalan',
            'Warna',
            'Roll',
            'Yard',
            'Hasil',
            *[date.isoformat() for date in dates],
            'DONE',
            'Pemakaian',
        ],
        [
            [
                po.nomor,
                row['invoices'],
                row['surat_jalan'],
                row['color'].name,
                row['rolls'],
                row['yard'],
                row['hasil'],
                *[row['sent'].get(date, 0) for date in dates],
                row['done'],
                row['pemakaian'] if row['pemakaian'] else '',
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
