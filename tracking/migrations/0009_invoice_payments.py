import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('tracking', '0008_production_sizes')]

    operations = [
        # Historical invoices have no verifiable payment ledger. Do not guess Unpaid.
        migrations.AddField(
            model_name='invoice',
            name='payment_reconciled',
            field=models.BooleanField(default=False),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='invoice',
            name='payment_reconciled',
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.CharField(
                choices=[
                    ('purchasing', 'Purchasing'),
                    ('direktur', 'Direktur'),
                    ('admin', 'Admin'),
                    ('accounting', 'Accounting'),
                ],
                default='direktur',
                max_length=12,
            ),
        ),
        migrations.CreateModel(
            name='InvoicePayment',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name='ID'
                    ),
                ),
                (
                    'created_at',
                    models.DateTimeField(default=django.utils.timezone.now, editable=False),
                ),
                ('amount', models.DecimalField(max_digits=16, decimal_places=2)),
                ('payment_date', models.DateField()),
                (
                    'kind',
                    models.CharField(
                        max_length=5, choices=[('lunas', 'Lunas'), ('cicil', 'Cicil')]
                    ),
                ),
                ('request_id', models.CharField(max_length=64, unique=True)),
                ('fingerprint', models.CharField(max_length=64)),
                ('proof_filename', models.CharField(max_length=180)),
                ('proof_content_type', models.CharField(max_length=40)),
                ('proof_size', models.PositiveIntegerField()),
                ('proof_content', models.BinaryField()),
                (
                    'invoice',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='payments',
                        to='tracking.invoice',
                    ),
                ),
                (
                    'recorded_by',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='invoice_payments',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'ordering': ['payment_date', 'created_at', 'pk'],
                'indexes': [
                    models.Index(
                        fields=['invoice', 'payment_date'], name='invoice_payment_date_idx'
                    )
                ],
                'constraints': [
                    models.CheckConstraint(
                        condition=models.Q(amount__gt=0), name='invoice_payment_positive'
                    ),
                    models.CheckConstraint(
                        condition=models.Q(kind__in=['lunas', 'cicil']), name='invoice_payment_kind'
                    ),
                ],
            },
        ),
    ]
