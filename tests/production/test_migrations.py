import uuid
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from aegis.core.config import get_settings
from aegis.core.dbadmin import downgrade, upgrade
from aegis.models import Base, tenant_tables


@pytest.mark.pg
def test_clean_migrations_roundtrip_and_metadata_drift(pg):
    base = make_url(get_settings().admin_database_url)
    name = 'aegis_migration_'+uuid.uuid4().hex[:10]+'_test'
    admin = create_engine(base.set(database='postgres'), isolation_level='AUTOCOMMIT')
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        upgrade(url=url)
        with engine.connect() as c:
            assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '0005_governance'
            assert not compare_metadata(MigrationContext.configure(c, opts={'compare_type': True}), Base.metadata)
            enabled = set(c.execute(text("SELECT relname FROM pg_class WHERE relrowsecurity")).scalars())
            assert set(tenant_tables()) <= enabled
        downgrade('0002_production_invariants', url=url)
        upgrade(url=url)
        downgrade('base', url=url)
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename<>'alembic_version'")).scalar_one() == 0
        upgrade(url=url)
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
