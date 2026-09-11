from django.utils import timezone

NAV=[('dashboard','Ringkasan','/','▦'),('orders','PO produksi','/orders/','▤'),('receipts','Penerimaan bahan','/receipts/','↓'),('stock','Stok bahan','/stock/','▥'),('allocations','Alokasi bahan','/allocations/','⇄'),('approvals','Persetujuan','/approvals/','✓'),('shipments','Pengiriman & CMT','/shipments/','→'),('reconciliation','Sisa bahan','/reconciliation/','↺'),('reports','Laporan & arsip','/reports/','▧'),('masters','Data master','/masters/','⊞')]

def navigation(request):
    nav=list(NAV)
    if request.user.is_authenticated:
        if request.user.role=='admin':
            nav.append(('accounts','Akun pengguna','/accounts/','♙'))
        if request.user.role in ['admin','purchasing','approver']:
            nav.append(('audit','Audit log','/audit/','≡'))
    return {'navigation':nav,'today':timezone.localdate()}