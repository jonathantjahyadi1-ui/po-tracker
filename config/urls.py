from django.urls import path
from tracking import views

urlpatterns=[
    path('',views.dashboard,name='dashboard'),
    path('healthz/',views.health,name='health'),
    path('login/',views.SignIn.as_view(),name='login'),
    path('logout/',views.sign_out,name='logout'),
    path('demo-login/',views.demo_login,name='demo_login'),
    path('reports/',views.reports_home,name='reports'),
    path('adjustment/',views.adjustment,name='adjustment'),
    path('evidence/<int:pk>/',views.download_evidence,name='evidence'),
    path('<str:kind>/export/',views.export,name='export'),
    path('<str:kind>/new/',views.edit,name='create'),
    path('<str:kind>/<int:pk>/edit/',views.edit,name='edit'),
    path('<str:kind>/<int:pk>/action/<str:action>/',views.action,name='action'),
    path('<str:kind>/<int:pk>/',views.detail,name='detail'),
    path('<str:kind>/',views.listing,name='listing'),
]
handler403=views.forbidden
handler404=views.not_found
handler500=views.server_error
