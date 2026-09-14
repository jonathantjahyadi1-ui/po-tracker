from getpass import getpass
from django.core.management.base import BaseCommand,CommandError
from django.contrib.auth.password_validation import validate_password
from tracking.models import User,Audit

class Command(BaseCommand):
    help='Buat Super Admin awal melalui input password interaktif.'
    def add_arguments(self,parser):
        parser.add_argument('--username',required=True)
        parser.add_argument('--email',default='')
    def handle(self,*args,**opts):
        if User.objects.filter(username=opts['username']).exists():
            raise CommandError('Nama pengguna sudah ada.')
        password=getpass('Kata sandi baru (minimal 6 karakter): ')
        if password!=getpass('Ulangi kata sandi: '):
            raise CommandError('Kata sandi tidak sama.')
        obj=User(username=opts['username'],email=opts['email'],role='admin')
        validate_password(password,obj); obj.set_password(password); obj.full_clean(); obj.save()
        Audit.objects.create(actor=obj,role=obj.role,action='bootstrap_admin',entity='User',object_id=str(obj.pk))
        self.stdout.write('Akun Super Admin dibuat. Masuk untuk membuat akun Purchasing dan Direktur.')
