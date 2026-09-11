from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Q

from tracking.models import Audit, User


INITIAL_USERNAME = 'Jonathan'
INITIAL_EMAIL = 'jonathantjahyadi1@gmail.com'
INITIAL_PASSWORD_HASH = '__INITIAL_PASSWORD_HASH__'
BOOTSTRAP_ACTION = 'bootstrap_initial_admin'


class Command(BaseCommand):
    help = 'Buat akun Super Admin Jonathan sekali pada pemasangan pertama.'

    @transaction.atomic
    def handle(self, *args, **options):
        if connection.vendor == 'postgresql':
            # Serialize concurrent deploys before checking the bootstrap marker.
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [721910011])

        # The immutable audit entry survives changes to name, password and role.
        # Restarting must never reactivate or restore a previously changed account.
        if Audit.objects.filter(action=BOOTSTRAP_ACTION, entity='User').exists():
            self.stdout.write('Akun awal sudah diinisialisasi; data akun tidak diubah.')
            return

        if User.objects.filter(
            Q(username__iexact=INITIAL_USERNAME) | Q(email__iexact=INITIAL_EMAIL)
        ).exists():
            raise CommandError(
                'Nama pengguna atau email akun awal sudah digunakan. '
                'Periksa akun yang ada sebelum menjalankan inisialisasi.'
            )

        user = User(
            username=INITIAL_USERNAME,
            first_name='Jonathan',
            email=INITIAL_EMAIL,
            password=INITIAL_PASSWORD_HASH,
            role=User.Role.ADMIN,
            is_active=True,
        )
        user.full_clean()
        user.save()
        Audit.objects.create(
            actor=user,
            role=user.role,
            action=BOOTSTRAP_ACTION,
            entity='User',
            object_id=str(user.pk),
            after={'username': user.username, 'email': user.email, 'role': user.role},
        )
        self.stdout.write(self.style.SUCCESS('Akun Super Admin Jonathan dibuat.'))