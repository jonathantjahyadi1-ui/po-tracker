"""Compare the current navigation with HEAD using synthetic, isolated local data.

Only the two read functions from HEAD are evaluated. No old code is written over
the application and the operational database is never opened by this script.
"""

import ast
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = (ROOT / '.verification').resolve()


def main():
    if OUTPUT.parent != ROOT:
        raise RuntimeError('Benchmark output must stay in the workspace.')
    OUTPUT.mkdir(exist_ok=True)
    path = OUTPUT / f'navigation-{uuid4().hex}.sqlite3'
    os.environ.update(
        DJANGO_SETTINGS_MODULE='config.settings',
        DEBUG='true',
        DEV_DATABASE_URL='',
        LOCAL_DATABASE=str(path),
        SECRET_KEY='isolated-navigation-benchmark',
        DATABASE_SCHEMA='',
    )
    sys.path.insert(0, str(ROOT))
    import django

    django.setup()
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.core.management import call_command
    from django.db import connection
    from django.test import RequestFactory
    from django.test.utils import CaptureQueriesContext
    from django.utils import timezone

    from tracking import views
    from tracking.models import Alokasi, Invoice, Master, Po, Roll, User

    call_command('migrate', verbosity=0)
    buyer = User.objects.create_user('benchmark-only', role='purchasing')
    vendor = Master.objects.create(kind='vendor', name='Benchmark vendor')
    cmt = Master.objects.create(kind='cmt', name='Benchmark CMT')
    material = Master.objects.create(kind='material', name='Benchmark fabric')
    color = Master.objects.create(kind='color', name='Benchmark color')
    invoices = Invoice.objects.bulk_create(
        [
            Invoice(
                vendor=vendor,
                nomor=f'BENCH-{index}',
                total_rp=100000000,
                tanggal=timezone.localdate(),
                dibuat_oleh=buyer,
            )
            for index in range(20)
        ]
    )
    pos = Po.objects.bulk_create([Po(nomor=f'PO BENCH {i}', cmt=cmt) for i in range(1000)])
    allocations = Alokasi.objects.bulk_create(
        [Alokasi(cmt=cmt, po=po, dibuat_oleh=buyer, status='disetujui') for po in pos]
    )
    Roll.objects.bulk_create(
        [
            Roll(
                invoice=invoices[i // 1000],
                material=material,
                color=color,
                urut=i % 1000 + 1,
                yard=100,
                status='diterima',
                alokasi=allocations[i // 20],
            )
            for i in range(20000)
        ],
        batch_size=500,
    )
    baseline_source = subprocess.run(
        ['git', 'show', 'HEAD:tracking/views.py'],
        cwd=ROOT,
        check=True,
        capture_output=True,
        encoding='utf-8',
    ).stdout
    namespace = dict(vars(views))
    for node in ast.parse(baseline_source).body:
        if isinstance(node, ast.FunctionDef) and node.name in ('vendor_detail', 'render_po_list'):
            exec(
                compile(ast.get_source_segment(baseline_source, node), '<baseline>', 'exec'),
                namespace,
            )
    factory = RequestFactory()

    def measure(function, url, *args, **kwargs):
        samples, counts = [], []
        for _ in range(5):
            request = factory.get(url)
            request.user, request.session = buyer, {}
            request._messages = FallbackStorage(request)
            with CaptureQueriesContext(connection) as queries:
                started = perf_counter()
                response = function(request, *args, **kwargs)
                samples.append((perf_counter() - started) * 1000)
            if response.status_code != 200:
                raise RuntimeError('Benchmark endpoint failed.')
            counts.append(len(queries))
        return {
            'median_ms': round(statistics.median(samples), 2),
            'queries': int(statistics.median(counts)),
        }

    result = {'fixture': {'po': 1000, 'roll': 20000, 'invoice': 20}, 'pages': {}}
    for label, name, url, args, kwargs in (
        ('Vendor detail', 'vendor_detail', f'/vendor/{vendor.pk}/', (vendor.pk,), {}),
        ('PO per CMT', 'render_po_list', f'/po/cmt/{cmt.pk}/', (), {'cmt': cmt}),
    ):
        before = measure(namespace[name], url, *args, **kwargs)
        after = measure(getattr(views, name), url, *args, **kwargs)
        result['pages'][label] = {
            'before': before,
            'after': after,
            'improvement_percent': round(100 * (1 - after['median_ms'] / before['median_ms']), 1),
        }
    (OUTPUT / 'navigation-benchmark.json').write_text(
        json.dumps(result, indent=2), encoding='utf-8'
    )
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
