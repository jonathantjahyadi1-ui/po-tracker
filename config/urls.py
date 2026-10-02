from django.urls import path

from tracking import views

urlpatterns = [
    path('healthz/', views.health, name='health'),
    path('login/', views.sign_in, name='login'),
    path('logout/', views.sign_out, name='logout'),
    path('', views.dashboard, name='dashboard'),
    path('master/', views.master_page, name='master'),
    path('vendor/', views.vendor_list, name='vendor_list'),
    path('vendor/<int:pk>/', views.vendor_detail, name='vendor_detail'),
    path('invoice/baru/', views.invoice_edit, name='invoice_new'),
    path('invoice/<int:pk>/ubah/', views.invoice_edit, name='invoice_edit'),
    path('invoice/<int:pk>/batalkan/', views.invoice_cancel, name='invoice_cancel'),
    path('invoice/<int:pk>/unduh/', views.invoice_export, name='invoice_export'),
    path('invoice/<int:pk>/lampiran/', views.invoice_attachment, name='invoice_attachment'),
    path('invoice/<int:pk>/', views.invoice_detail, name='invoice_detail'),
    path('alokasi/', views.allocation_list, name='allocation_list'),
    path('alokasi/ajukan/', views.allocation_create, name='allocation_create'),
    path('alokasi/<int:pk>/', views.allocation_detail, name='allocation_detail'),
    path('cmt/', views.cmt_list, name='cmt_list'),
    path('cmt/<int:pk>/', views.cmt_detail, name='cmt_detail'),
    path('po/', views.po_list, name='po_list'),
    path('po/<int:pk>/unduh/', views.po_export, name='po_export'),
    path('po/<int:pk>/', views.po_detail, name='po_detail'),
    path('roll/status/', views.roll_status, name='roll_status'),
    path('akun/', views.accounts, name='accounts'),
    path('riwayat/', views.history, name='history'),
]
handler403 = views.forbidden
handler404 = views.not_found
handler500 = views.server_error
