import os
import runpy
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase


class DatabaseConfigTests(SimpleTestCase):
    def load_settings(self, *, testing=False, **env):
        values = {'DEBUG': 'true', **env}
        argv = ['manage.py', 'test' if testing else 'runserver']
        path = Path(__file__).resolve().parent.parent / 'config' / 'settings.py'
        with (
            patch.dict(os.environ, values, clear=True),
            patch('sys.argv', argv),
            patch('dotenv.load_dotenv'),
        ):
            return runpy.run_path(str(path))

    def test_debug_uses_supabase_url_and_explicit_schema(self):
        settings = self.load_settings(
            DATABASE_URL='postgresql://buyer:p%40ss@db.example:5432/postgres',
            DATABASE_SCHEMA='severli',
        )
        database = settings['DATABASES']['default']
        self.assertEqual(settings['DATABASE_URL_SOURCE'], 'DATABASE_URL')
        self.assertEqual(database['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(database['HOST'], 'db.example')
        self.assertEqual(database['PASSWORD'], 'p@ss')
        self.assertEqual(database['OPTIONS']['options'], '-c search_path=severli')

    def test_development_url_overrides_production_url_in_debug(self):
        settings = self.load_settings(
            DATABASE_URL='postgresql://buyer:secret@production.example/postgres',
            DEV_DATABASE_URL='postgresql://buyer:secret@development.example/postgres',
            DATABASE_SCHEMA='severli',
        )
        self.assertEqual(settings['DATABASE_URL_SOURCE'], 'DEV_DATABASE_URL')
        self.assertEqual(settings['DATABASES']['default']['HOST'], 'development.example')

    def test_explicit_local_database_selects_sqlite(self):
        settings = self.load_settings(
            LOCAL_DATABASE='offline.sqlite3',
            DATABASE_URL='postgresql://buyer:secret@production.example/postgres',
        )
        self.assertEqual(settings['DATABASES']['default']['ENGINE'], 'django.db.backends.sqlite3')
        self.assertEqual(settings['DATABASES']['default']['NAME'], 'offline.sqlite3')

    def test_debug_without_urls_still_uses_sqlite(self):
        settings = self.load_settings()
        self.assertEqual(settings['DATABASES']['default']['ENGINE'], 'django.db.backends.sqlite3')

    def test_tests_never_use_operational_urls_or_sqlite_file(self):
        settings = self.load_settings(
            testing=True,
            DATABASE_URL='postgresql://buyer:secret@production.example/postgres',
            DEV_DATABASE_URL='postgresql://buyer:secret@development.example/postgres',
            LOCAL_DATABASE='offline.sqlite3',
        )
        self.assertEqual(settings['DATABASES']['default']['ENGINE'], 'django.db.backends.sqlite3')
        self.assertIsNone(settings['DATABASE_URL'])
        self.assertEqual(settings['DATABASE_URL_SOURCE'], 'TEST_DATABASE_URL')

    def test_explicit_test_url_is_used_only_for_tests(self):
        settings = self.load_settings(
            testing=True,
            DATABASE_URL='postgresql://buyer:secret@production.example/postgres',
            TEST_DATABASE_URL='postgresql://buyer:secret@test.example/test_database',
        )
        self.assertEqual(settings['DATABASES']['default']['HOST'], 'test.example')

    def test_postgres_requires_schema_in_debug(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'DATABASE_SCHEMA wajib diisi'):
            self.load_settings(DATABASE_URL='postgresql://buyer:secret@db.example/postgres')

    def test_invalid_schema_is_rejected(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'Nama schema database tidak valid'):
            self.load_settings(DATABASE_SCHEMA='severli,public')

    def test_production_ignores_development_and_local_overrides(self):
        settings = self.load_settings(
            DEBUG='false',
            SECRET_KEY='test-production-secret',
            DATABASE_URL='postgresql://buyer:secret@production.example/postgres',
            DEV_DATABASE_URL='postgresql://buyer:secret@development.example/postgres',
            LOCAL_DATABASE='offline.sqlite3',
            DATABASE_SCHEMA='severli',
        )
        self.assertEqual(settings['DATABASES']['default']['HOST'], 'production.example')

    def test_invalid_url_does_not_fall_back_to_sqlite(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'DATABASE_URL harus PostgreSQL'):
            self.load_settings(DATABASE_URL='https://project.supabase.co')
