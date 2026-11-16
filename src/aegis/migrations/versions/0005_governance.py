"""Reconciliation follow-up with tenant-safe references."""
from alembic import op
revision = '0005_governance'
down_revision = '0004_execution'
branch_labels = None
depends_on = None

def upgrade():
    op.execute("\nCREATE TABLE reconciliation_case (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tbatch_id UUID NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tassignee_id UUID, \n\tresolution_check_id UUID, \n\trevision INTEGER NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_reconciliation_case PRIMARY KEY (id), \n\tCONSTRAINT uq_reconciliation_case_tenant_id_batch_id UNIQUE (tenant_id, batch_id), \n\tCONSTRAINT ck_reconciliation_case_status_valid CHECK (status in ('open','in_progress','resolved')), \n\tCONSTRAINT ck_reconciliation_case_revision_valid CHECK (revision >= 1), \n\tCONSTRAINT fk_reconciliation_case_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_reconciliation_case_batch_id_ingestion_batch FOREIGN KEY(batch_id) REFERENCES ingestion_batch (id), \n\tCONSTRAINT fk_reconciliation_case_assignee_id_app_user FOREIGN KEY(assignee_id) REFERENCES app_user (id), \n\tCONSTRAINT fk_reconciliation_case_resolution_check_id_source_recon_2190 FOREIGN KEY(resolution_check_id) REFERENCES source_reconciliation (id), \n\tCONSTRAINT uq_reconciliation_case_tenant_ref UNIQUE (tenant_id, id), \n\tCONSTRAINT fk_reconciliation_case_resolution_check_id_tenant FOREIGN KEY(tenant_id, resolution_check_id) REFERENCES source_reconciliation (tenant_id, id), \n\tCONSTRAINT fk_reconciliation_case_batch_id_tenant FOREIGN KEY(tenant_id, batch_id) REFERENCES ingestion_batch (tenant_id, id), \n\tCONSTRAINT fk_reconciliation_case_assignee_id_tenant FOREIGN KEY(tenant_id, assignee_id) REFERENCES app_user (tenant_id, id)\n)\n\n")
    op.execute('CREATE INDEX ix_reconciliation_case_tenant_id ON reconciliation_case (tenant_id)')
    op.execute('ALTER TABLE reconciliation_case ENABLE ROW LEVEL SECURITY')
    op.execute("CREATE POLICY tenant_isolation ON reconciliation_case USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)")

def downgrade():
    op.drop_table('reconciliation_case')
