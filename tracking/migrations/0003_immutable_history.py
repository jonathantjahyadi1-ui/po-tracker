from django.db import migrations

TABLES=['tracking_movement','tracking_audit','tracking_decision']

def install(apps,schema_editor):
    db=schema_editor.connection.vendor
    with schema_editor.connection.cursor() as cur:
        if db=='postgresql':
            cur.execute("""CREATE OR REPLACE FUNCTION immutable_history() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Append-only history cannot be changed'; END; $$""")
            for table in TABLES:
                cur.execute(f'CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION immutable_history()')
            # Explicitly shield every application table even if schema is exposed accidentally.
            for model in apps.get_app_config('tracking').get_models():
                table=model._meta.db_table
                cur.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
                cur.execute(f'REVOKE ALL ON TABLE {table} FROM PUBLIC')
                for role in ['anon','authenticated']:
                    cur.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',[role])
                    if cur.fetchone():
                        cur.execute(f'REVOKE ALL ON TABLE {table} FROM {role}')
        elif db=='sqlite':
            for table in TABLES:
                for op in ['UPDATE','DELETE']:
                    cur.execute(f"CREATE TRIGGER {table}_immutable_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT, 'Append-only history cannot be changed'); END")

def uninstall(apps,schema_editor):
    with schema_editor.connection.cursor() as cur:
        for table in TABLES:
            if schema_editor.connection.vendor=='postgresql':
                cur.execute(f'DROP TRIGGER IF EXISTS immutable_history ON {table}')
            elif schema_editor.connection.vendor=='sqlite':
                for op in ['update','delete']:
                    cur.execute(f'DROP TRIGGER IF EXISTS {table}_immutable_{op}')
        if schema_editor.connection.vendor=='postgresql':
            cur.execute('DROP FUNCTION IF EXISTS immutable_history()')

class Migration(migrations.Migration):
    dependencies=[('tracking','0002_movement_shipment_line_and_more')]
    operations=[migrations.RunPython(install,uninstall)]
