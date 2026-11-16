from datetime import UTC, datetime
import pytest
from sqlalchemy import select
from aegis.core.config import get_settings
from aegis.core.db import tenant_session
from aegis.core.errors import InvalidInput
from aegis.core.tenancy import system_principal
from aegis.integration.connections import create_connection, request_sync
from aegis.integration.connectors.mock_platforms import authorize, manifest_for_batch
from aegis.integration.markets import AMAZON_MARKETS, TIKTOK_MARKETS
from aegis.integration.reconciliation import reconcile
from aegis.integration.runtime import claim_sync_run, execute_sync_run
from aegis.models import IngestionBatch, DemandObservation


@pytest.mark.parametrize('kind', ['mock_amazon','mock_tiktok'])
def test_official_shaped_mock_flows_use_real_parsers_and_independent_reconciliation(seeded,monkeypatch,kind):
    tenant,_,_=seeded; p=system_principal(tenant)
    monkeypatch.setattr(get_settings(),'integration_mode','mock')
    config=({'seller_id':'MOCK-SELLER','marketplace_id':'ATVPDKIKX0DER','region':'NA','streams':['order_lines','inventory_snapshots']}
            if kind=='mock_amazon' else {'shop_id':'MOCK-SHOP','shop_cipher':'MOCK-CIPHER','market':'US'})
    config.update(location_code='W',initial_since='2026-09-30T00:00:00Z')
    with tenant_session(tenant) as s:
        conn=create_connection(s,p,name=kind,kind=kind,config=config)
        assert conn.is_mock
        authorize(s,p,conn); conn_id=conn.id
        request_sync(s,p,conn,mode='backfill',window_start=datetime(2026,9,30,tzinfo=UTC),window_end=datetime(2026,10,3,tzinfo=UTC))
    result=execute_sync_run(claim_sync_run())
    assert result=='succeeded'
    with tenant_session(tenant) as s:
        from aegis.models import IntegrationConnection
        conn=s.get(IntegrationConnection,conn_id)
        batches=list(s.scalars(select(IngestionBatch).where(IngestionBatch.connection_id==conn_id)))
        for batch in batches:
            assert batch.status=='complete'
            check=reconcile(s,p,batch.id,**manifest_for_batch(s,conn,batch))
            assert check.status=='matched' and check.is_mock
        observations=list(s.scalars(select(DemandObservation).where(DemandObservation.source_system==conn.source_system)))
        assert sum(row.units for row in observations)==(5 if kind=='mock_amazon' else 1)
        assert len(AMAZON_MARKETS)==23 and len(TIKTOK_MARKETS)==20
        with pytest.raises(InvalidInput):
            create_connection(s,p,name='live',kind='amazon_sp',config={'seller_id':'x','marketplace_id':'ATVPDKIKX0DER','region':'NA','location_code':'W','initial_since':'2026-09-01T00:00:00Z'})


def test_sample_import_stays_mock_in_live_deployment_and_reconciles_independent_fixture(tenant):
    from pathlib import Path
    from aegis.bootstrap import import_sample
    from aegis.core.errors import Conflict
    from aegis.integration.reconciliation import require_reconciled
    from aegis.models import IntegrationConnection
    result=import_sample(tenant,Path('data/sample_network.json'))
    assert result['quarantined']==0
    with tenant_session(tenant) as s:
        conn=s.scalar(select(IntegrationConnection).where(IntegrationConnection.name=='sample-import'))
        assert conn.is_mock
        assert require_reconciled(s,tenant,live=False)
        with pytest.raises(Conflict):require_reconciled(s,tenant,live=True)
