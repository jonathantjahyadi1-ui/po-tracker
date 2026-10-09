"""Verify additive Payment migration on a consistent SQLite backup before local apply."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
OUTPUT = WORKSPACE / '.verification'


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def snapshot(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        result = {}
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tracking_%'"
        ).fetchall()
        for (table,) in tables:
            columns = [
                row[1] for row in db.execute(f'PRAGMA table_info({quote_identifier(table)})')
            ]
            if table == 'tracking_invoice':
                columns = [column for column in columns if column != 'payment_reconciled']
            selected = ','.join(quote_identifier(column) for column in columns)
            rows = db.execute(
                f'SELECT {selected} FROM {quote_identifier(table)} ORDER BY id'
            ).fetchall()
            result[table] = {
                'rows': len(rows),
                'columns': columns,
                'digest': hashlib.sha256(repr(rows).encode()).hexdigest(),
            }
        return result


def backup(source, destination):
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)


def migrate(path):
    environment = os.environ.copy()
    environment.update(
        DEBUG='true',
        DEV_DATABASE_URL='',
        TEST_DATABASE_URL='',
        DATABASE_SCHEMA='',
        LEGACY_USERS_SCHEMA='',
        LOCAL_DATABASE=str(path),
        SECRET_KEY='payment-local-migration-verification',
    )
    subprocess.run(
        [sys.executable, 'manage.py', 'migrate', '--noinput'],
        cwd=WORKSPACE,
        env=environment,
        check=True,
    )
    subprocess.run(
        [sys.executable, 'manage.py', 'check'], cwd=WORKSPACE, env=environment, check=True
    )


def compare(before, after):
    for table, expected in before.items():
        if after.get(table) != expected:
            raise RuntimeError(f'Data existing berubah pada {table}; migrasi lokal dibatalkan.')
    if 'tracking_invoicepayment' not in before and after['tracking_invoicepayment']['rows']:
        raise RuntimeError('Migrasi tidak boleh membuat transaksi pembayaran historis.')


def payment_counts(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        return {
            'invoices': db.execute('SELECT COUNT(*) FROM tracking_invoice').fetchone()[0],
            'needs_reconciliation': db.execute(
                'SELECT COUNT(*) FROM tracking_invoice WHERE NOT payment_reconciled '
                'AND NOT dibatalkan'
            ).fetchone()[0],
            'missing_invoice_total': db.execute(
                'SELECT COUNT(*) FROM tracking_invoice WHERE total_rp <= 0 AND NOT dibatalkan'
            ).fetchone()[0],
            'payments': db.execute('SELECT COUNT(*) FROM tracking_invoicepayment').fetchone()[0],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='severli.sqlite3')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    source = (WORKSPACE / args.database).resolve(strict=True)
    if source.parent != WORKSPACE or source.suffix != '.sqlite3':
        raise RuntimeError('Database harus berupa SQLite lokal dalam workspace.')
    OUTPUT.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    saved = OUTPUT / f'payment-before-{stamp}.sqlite3'
    candidate = OUTPUT / f'payment-migrated-{stamp}.sqlite3'
    backup(source, saved)
    before = snapshot(saved)
    if 'tracking_invoice' not in before:
        raise RuntimeError('Database tidak memiliki invoice PO Tracker; tidak akan dimigrasi.')
    backup(saved, candidate)
    migrate(candidate)
    compare(before, snapshot(candidate))
    counts = payment_counts(candidate)
    report = {
        'source': str(source),
        'backup': str(saved),
        'verified_copy': str(candidate),
        'existing_tables_preserved': len(before),
        'counts': counts,
        'applied': False,
    }
    if args.apply:
        if snapshot(source) != before:
            raise RuntimeError('Data aktif berubah sejak backup. Ulangi audit sebelum migrasi.')
        migrate(source)
        compare(before, snapshot(source))
        report['applied'] = True
    path = OUTPUT / f'payment-migration-{stamp}.json'
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({**report, 'report': str(path)}, indent=2))


if __name__ == '__main__':
    main()
