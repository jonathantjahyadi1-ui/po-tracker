#!/usr/bin/env bash
set -euo pipefail
pip install -r requirements.lock.txt
python manage.py collectstatic --noinput
python manage.py check --deploy --fail-level WARNING
