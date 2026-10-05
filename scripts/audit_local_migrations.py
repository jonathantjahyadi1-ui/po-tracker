"""Audit migrations using consistent read-only backups, printing conservation only."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
OUTPUT = WORKSPACE / '.verification'
TABLES = {
    'invoice': 'tracking_invoice',
    'roll': 'tracking_roll',
    'allocation': 'tracking_alokasi',
    'po': 'tracking_po',
    'hasil': 'tracking_hasil',
    'shipment': 'tracking_kirimgudang',
    'attachment': 'tracking_invoiceattachment',
    'log': 'tracking_log',
}


def digest(rows):
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def snapshot(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {'counts': {}, 'totals': {}, 'digests': {}}
        for name, table in TABLES.items():
            if table not in tables:
                result['counts'][name] = 0
                result['digests'][name] = digest([])
                continue
            result['counts'][name] = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            columns = [row[1] for row in db.execute(f'PRAGMA table_info({table})')]
            # Ignore only fields added by these migrations. Retained legacy fields must match.
            columns = [
                name for name in columns if name not in ('invoice_color_id', 'cmt_id', 'request_id')
            ]
            if table == 'tracking_alokasi':
                columns = [name for name in columns if name not in ('tgl_kirim', 'sj_kirim')]
            if table == 'tracking_alokasi' and 'cmt_id' not in columns:
                columns.append('cmt_id')
            rows = db.execute(f'SELECT {",".join(columns)} FROM {table} ORDER BY id').fetchall()
            result['digests'][name] = digest(rows)
        for label, table, column in (
            ('yard', 'tracking_roll', 'yard'),
            ('hasil_pcs', 'tracking_hasil', 'pcs'),
            ('shipment_pcs', 'tracking_kirimgudang', 'pcs'),
        ):
            values = [] if table not in tables else db.execute(f'SELECT {column} FROM {table}')
            result['totals'][label] = str(sum((Decimal(str(row[0])) for row in values), Decimal(0)))
        return result


def run_migrations(copy):
    environment = os.environ.copy()
    environment.update(
        DEBUG='true',
        DEV_DATABASE_URL='',
        TEST_DATABASE_URL='',
        DATABASE_SCHEMA='',
        LEGACY_USERS_SCHEMA='',
        LOCAL_DATABASE=str(copy),
        SECRET_KEY='local-migration-verification-only',
    )
    subprocess.run(
        [sys.executable, 'manage.py', 'migrate', '--noinput'],
        cwd=WORKSPACE,
        env=environment,
        check=True,
    )
    subprocess.run(
        [sys.executable, 'manage.py', 'audit_workflow'], cwd=WORKSPACE, env=environment, check=True
    )


def main():
    OUTPUT.mkdir(exist_ok=True)
    report = {}
    for filename in ('local.sqlite3', 'severli.sqlite3'):
        original = (WORKSPACE / filename).resolve(strict=True)
        copy = (OUTPUT / ('migrated-' + filename)).resolve()
        if original.parent != WORKSPACE or copy.parent != OUTPUT:
            raise RuntimeError('Verification paths must stay in the workspace.')
        before_original = hashlib.sha256(original.read_bytes()).hexdigest()
        with sqlite3.connect(original.as_uri() + '?mode=ro', uri=True) as source:
            with sqlite3.connect(copy) as destination:
                source.backup(destination)
        before = snapshot(copy)
        with sqlite3.connect(copy.as_uri() + '?mode=ro', uri=True) as sample:
            available = {row[0] for row in sample.execute('SELECT name FROM sqlite_master')}
        required = set(TABLES.values()) - {'tracking_invoiceattachment'}
        missing = sorted(required - available)
        if missing:
            unchanged = before_original == hashlib.sha256(original.read_bytes()).hexdigest()
            report[filename] = {
                'schema_exception': 'Different historical application schema; automatic migration '
                'is not applicable.',
                'missing_current_tables': missing,
                'original_unchanged': unchanged,
            }
            print(json.dumps({filename: report[filename]}, indent=2))
            continue
        run_migrations(copy)
        after = snapshot(copy)
        unchanged_original = before_original == hashlib.sha256(original.read_bytes()).hexdigest()
        conserved = before == after
        report[filename] = {
            'before': before,
            'after': after,
            'conserved': conserved,
            'original_unchanged': unchanged_original,
        }
        print(
            json.dumps(
                {
                    filename: {
                        'before_counts': before['counts'],
                        'after_counts': after['counts'],
                        'before_totals': before['totals'],
                        'after_totals': after['totals'],
                        'retained_data_conserved': conserved,
                        'original_unchanged': unchanged_original,
                    }
                },
                indent=2,
            )
        )
    (OUTPUT / 'migration-conservation.json').write_text(
        json.dumps(report, indent=2), encoding='utf8'
    )
    if not all(
        item.get('conserved', True) and item['original_unchanged'] for item in report.values()
    ):
        raise SystemExit('Migration conservation failed; inspect the report.')


if __name__ == '__main__':
    main()
