import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import DecimalField, F, Max, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from .models import Invoice, InvoicePayment
from .money import format_money, parse_money
from .services import ZERO, lock_invoice, log
from .uploads import validate_document


def require_director(user):
    if (
        not user.is_authenticated
        or not user.is_active
        or user.is_superuser
        or user.role != 'direktur'
    ):
        raise PermissionDenied('Hanya Direktur yang dapat mencatat pembayaran.')


@dataclass(frozen=True)
class PaymentSummary:
    paid: Decimal | None
    remaining: Decimal | None
    progress: Decimal | None
    status: str
    status_key: str
    last_payment: date | None


def payment_summary(invoice, payments=None):
    if payments is not None:
        paid = sum((payment.amount for payment in payments), ZERO)
        last = max((payment.payment_date for payment in payments), default=None)
    elif hasattr(invoice, '_paid'):
        paid, last = invoice._paid, invoice._last_payment
    else:
        aggregate = invoice.payments.aggregate(paid=Sum('amount'), last=Max('payment_date'))
        paid, last = aggregate['paid'] or ZERO, aggregate['last']
    if not invoice.payment_reconciled or invoice.total_rp <= 0:
        return PaymentSummary(None, None, None, 'Perlu rekonsiliasi', 'reconciliation', last)
    remaining = invoice.total_rp - paid
    if remaining < ZERO:
        return PaymentSummary(paid, remaining, None, 'Perlu rekonsiliasi', 'reconciliation', last)
    if remaining == ZERO:
        progress, status, key = Decimal('100.0'), 'Paid', 'paid'
    elif paid == ZERO:
        progress, status, key = Decimal('0.0'), 'Unpaid', 'unpaid'
    else:
        progress = min(
            Decimal('99.9'),
            (paid * 100 / invoice.total_rp).quantize(Decimal('0.1'), rounding=ROUND_DOWN),
        )
        status, key = 'Dicicil', 'dicicil'
    return PaymentSummary(paid, remaining, progress, status, key, last)


def payment_queryset(filters):
    """One row per invoice regardless of material, allocation or PO cardinality."""
    totals = (
        InvoicePayment.objects.filter(invoice_id=OuterRef('pk'))
        .order_by()
        .values('invoice_id')
        .annotate(paid=Sum('amount'), last=Max('payment_date'))
    )
    money = DecimalField(max_digits=16, decimal_places=2)
    queryset = (
        Invoice.objects.filter(dibatalkan=False)
        .select_related('vendor')
        .annotate(
            _paid=Coalesce(Subquery(totals.values('paid')[:1]), Value(ZERO), output_field=money),
            _last_payment=Subquery(totals.values('last')[:1]),
        )
    )
    if filters['q']:
        queryset = queryset.filter(
            Q(nomor__icontains=filters['q']) | Q(vendor__name__icontains=filters['q'])
        )
    if filters['date_from']:
        queryset = queryset.filter(tanggal__gte=filters['date_from'])
    if filters['date_to']:
        queryset = queryset.filter(tanggal__lte=filters['date_to'])
    ready = Q(payment_reconciled=True, total_rp__gt=0)
    status_conditions = {
        'unpaid': ready & Q(_paid=ZERO),
        'dicicil': ready & Q(_paid__gt=ZERO, _paid__lt=F('total_rp')),
        'paid': ready & Q(_paid=F('total_rp')),
        'reconciliation': ~ready | Q(_paid__gt=F('total_rp')),
    }
    if filters['status']:
        queryset = queryset.filter(status_conditions[filters['status']])
    return queryset.order_by('-tanggal', '-pk')


def payment_retry(request_id, fingerprint):
    previous = InvoicePayment.objects.filter(request_id=request_id).defer('proof_content').first()
    if previous and previous.fingerprint != fingerprint:
        raise ValidationError('Permintaan ini sudah digunakan. Buka ulang form pembayaran.')
    return previous


@transaction.atomic
def record_payment(invoice, data, user):
    require_director(user)
    kind = data.get('kind')
    if kind not in InvoicePayment.Kind.values:
        raise ValidationError('Pilih Lunas atau Cicil.')
    amount = parse_money(data.get('amount'))
    seen = parse_money(data.get('remaining_seen'))
    if amount <= ZERO:
        raise ValidationError('Nominal pembayaran wajib lebih besar dari nol.')
    payment_date = data.get('payment_date')
    if not isinstance(payment_date, date) or isinstance(payment_date, datetime):
        raise ValidationError('Tanggal pembayaran wajib diisi dengan benar.')
    request_id = data.get('request_id')
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,64}', request_id):
        raise ValidationError('Permintaan pembayaran tidak valid. Buka ulang form pembayaran.')
    upload = validate_document(data.get('proof'), 'Bukti pembayaran', 5)
    try:
        content = upload.read(5 * 1024 * 1024 + 1)
        upload.seek(0)
    except (OSError, ValueError):
        raise ValidationError('Bukti pembayaran tidak berhasil dibaca. Unggah ulang berkas.')
    if len(content) != upload.size or len(content) > 5 * 1024 * 1024:
        raise ValidationError('Bukti pembayaran tidak berhasil dibaca. Unggah ulang berkas.')
    filename = upload.name.replace('\\', '/').rsplit('/', 1)[-1][:180]
    payload = {
        'invoice': invoice.pk,
        'user': user.pk,
        'amount': str(amount),
        'remaining_seen': str(seen),
        'kind': kind,
        'date': payment_date.isoformat(),
        'filename': filename,
        'proof': hashlib.sha256(content).hexdigest(),
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    invoice = lock_invoice(invoice.pk)
    previous = payment_retry(request_id, fingerprint)
    if previous:
        return previous
    if invoice.dibatalkan:
        raise ValidationError('Invoice yang dibatalkan tidak dapat dibayar.')
    summary = payment_summary(invoice)
    if summary.status_key == 'reconciliation':
        raise ValidationError('Posisi pembayaran invoice ini perlu direkonsiliasi terlebih dahulu.')
    if kind == InvoicePayment.Kind.LUNAS and (seen != summary.remaining or amount != seen):
        raise ValidationError(
            f'Sisa tagihan terbaru Rp {format_money(summary.remaining)}. '
            'Tinjau ulang nominal dan bukti, lalu buka ulang form pembayaran.',
            code='conflict',
        )
    if summary.remaining == ZERO:
        raise ValidationError('Invoice sudah Paid dan tidak menerima pembayaran tambahan.')
    if amount > summary.remaining:
        raise ValidationError(
            f'Nominal melebihi sisa tagihan terbaru Rp {format_money(summary.remaining)}. '
            'Tinjau ulang pembayaran.',
            code='conflict',
        )
    try:
        with transaction.atomic():
            payment = InvoicePayment.objects.create(
                invoice=invoice,
                amount=amount,
                payment_date=payment_date,
                kind=kind,
                recorded_by=user,
                request_id=request_id,
                fingerprint=fingerprint,
                proof_filename=filename,
                proof_content_type=upload.verified_content_type,
                proof_size=len(content),
                proof_content=content,
            )
    except IntegrityError:
        previous = payment_retry(request_id, fingerprint)
        if previous:
            return previous
        raise
    log(user, 'pembayaran invoice', invoice.nomor, f'{kind}: Rp {format_money(amount)}')
    return payment
