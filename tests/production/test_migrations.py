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
            assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '0007_decision_operations'
            assert not compare_metadata(MigrationContext.configure(c, opts={'compare_type': True}), Base.metadata)
            enabled = set(c.execute(text("SELECT relname FROM pg_class WHERE relrowsecurity")).scalars())
            assert set(tenant_tables()) <= enabled
        # Upgrade an actual pre-0007 order authority, not only an empty schema.
        downgrade('0006_market_revisions',url=url)
        from aegis.models import Tenant,IntegrationConnection,Location,SourceAuthority
        tenant_id,connection_id,location_id,rule_id=[uuid.uuid4() for _ in range(4)]
        with engine.begin() as c:
            c.execute(Tenant.__table__.insert().values(id=tenant_id,slug='legacy-orders',name='Legacy orders'))
            c.execute(IntegrationConnection.__table__.insert().values(id=connection_id,tenant_id=tenant_id,name='legacy',kind='file_csv',source_system='file_csv:legacy'))
            c.execute(Location.__table__.insert().values(id=location_id,tenant_id=tenant_id,code='W'))
            c.execute(SourceAuthority.__table__.insert().values(id=rule_id,tenant_id=tenant_id,domain='demand',stream='order_lines',location_id=location_id,connection_id=connection_id,note='Legacy fixture',approved_by='fixture'))
        upgrade(url=url)
        with engine.connect() as c:
            assert c.execute(text('SELECT quantity_basis FROM source_authority')).scalar_one()=='sellable_units'
            assert c.execute(text("SELECT payload->>'quantity_basis' FROM fact_revision WHERE table_name='source_authority'")).scalar_one()=='sellable_units'
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
