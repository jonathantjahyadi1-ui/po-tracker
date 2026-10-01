#!/usr/bin/env bash
set -euo pipefail
pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py check --deploy --fail-level WARNING
python manage.py prepare_database
python manage.py migrate --noinput
python manage.py import_legacy_users
