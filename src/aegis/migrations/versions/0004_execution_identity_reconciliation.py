"""Google OIDC, reconciliation and physical inventory execution (frozen DDL)."""
from alembic import op
import sqlalchemy as sa
revision = '0004_execution'
down_revision = '0003_foundation'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("integration_connection", sa.Column("is_mock", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("inventory_reservation", sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("inventory_reservation", sa.Column("consumed_units", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("inventory_reservation", sa.Column("released_units", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("inventory_reservation", sa.Column("settled_units", sa.Integer(), nullable=False, server_default="0"))
    op.execute("UPDATE inventory_reservation SET released_units=units WHERE status='released'")
    op.drop_constraint("ck_inventory_reservation_status_valid", "inventory_reservation")
    op.create_check_constraint(op.f("ck_inventory_reservation_status_valid"), "inventory_reservation", "status in ('held','released','consumed')")
    op.create_check_constraint(op.f("ck_inventory_reservation_counters_valid"), "inventory_reservation", "consumed_units >= 0 AND released_units >= 0 AND settled_units >= 0 AND settled_units <= consumed_units AND consumed_units + released_units <= units")
    op.execute("\nCREATE TABLE oauth_attempt (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tstate_hash VARCHAR(64) NOT NULL, \n\tbrowser_hash VARCHAR(64) NOT NULL, \n\tsecret_ciphertext TEXT NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_oauth_attempt PRIMARY KEY (id), \n\tCONSTRAINT ck_oauth_attempt_status_valid CHECK (status in ('pending','exchanging','succeeded','failed')), \n\tCONSTRAINT fk_oauth_attempt_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, \n\tCONSTRAINT uq_oauth_attempt_state_hash UNIQUE (state_hash), \n\tCONSTRAINT uq_oauth_attempt_tenant_ref UNIQUE (tenant_id, id)\n)\n\n")
    op.execute('CREATE INDEX ix_oauth_attempt_tenant_id ON oauth_attempt (tenant_id)')
    op.execute("ALTER TABLE oauth_attempt ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON oauth_attempt USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute('\nCREATE TABLE external_identity (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tuser_id UUID NOT NULL, \n\tprovider VARCHAR(20) NOT NULL, \n\tsubject VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_external_identity PRIMARY KEY (id), \n\tCONSTRAINT uq_external_identity_tenant_id_provider_subject UNIQUE (tenant_id, provider, subject), \n\tCONSTRAINT uq_external_identity_tenant_id_provider_user_id UNIQUE (tenant_id, provider, user_id), \n\tCONSTRAINT fk_external_identity_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_external_identity_user_id_app_user FOREIGN KEY(user_id) REFERENCES app_user (id), \n\tCONSTRAINT uq_external_identity_tenant_ref UNIQUE (tenant_id, id), \n\tCONSTRAINT fk_external_identity_user_id_tenant FOREIGN KEY(tenant_id, user_id) REFERENCES app_user (tenant_id, id)\n)\n\n')
    op.execute('CREATE INDEX ix_external_identity_tenant_id ON external_identity (tenant_id)')
    op.execute("ALTER TABLE external_identity ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON external_identity USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute("\nCREATE TABLE source_reconciliation (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tbatch_id UUID NOT NULL, \n\tconnection_id UUID NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tsource_digest VARCHAR(64) NOT NULL, \n\tlocal_digest VARCHAR(64) NOT NULL, \n\tdetails JSONB NOT NULL, \n\tevidence TEXT NOT NULL, \n\tis_mock BOOLEAN NOT NULL, \n\tchecked_by VARCHAR(320) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_source_reconciliation PRIMARY KEY (id), \n\tCONSTRAINT ck_source_reconciliation_status_valid CHECK (status in ('matched','mismatch')), \n\tCONSTRAINT fk_source_reconciliation_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_source_reconciliation_batch_id_ingestion_batch FOREIGN KEY(batch_id) REFERENCES ingestion_batch (id), \n\tCONSTRAINT fk_source_reconciliation_connection_id_integration_connection FOREIGN KEY(connection_id) REFERENCES integration_connection (id), \n\tCONSTRAINT uq_source_reconciliation_tenant_ref UNIQUE (tenant_id, id), \n\tCONSTRAINT fk_source_reconciliation_connection_id_tenant FOREIGN KEY(tenant_id, connection_id) REFERENCES integration_connection (tenant_id, id), \n\tCONSTRAINT fk_source_reconciliation_batch_id_tenant FOREIGN KEY(tenant_id, batch_id) REFERENCES ingestion_batch (tenant_id, id)\n)\n\n")
    op.execute('CREATE INDEX ix_source_reconciliation_batch_id ON source_reconciliation (batch_id)')
    op.execute('CREATE INDEX ix_source_reconciliation_tenant_id ON source_reconciliation (tenant_id)')
    op.execute("ALTER TABLE source_reconciliation ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON source_reconciliation USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute('\nCREATE TABLE execution_observation (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\treservation_id UUID NOT NULL, \n\tconnection_id UUID NOT NULL, \n\tsource_version BIGINT NOT NULL, \n\texternal_ref VARCHAR(200) NOT NULL, \n\tshipped INTEGER NOT NULL, \n\treceived INTEGER NOT NULL, \n\tcancelled INTEGER NOT NULL, \n\tis_final BOOLEAN NOT NULL, \n\toccurred_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tpayload_digest VARCHAR(64) NOT NULL, \n\tevidence TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_execution_observation PRIMARY KEY (id), \n\tCONSTRAINT uq_execution_observation_tenant_id_reservation_id_sourc_169c UNIQUE (tenant_id, reservation_id, source_version), \n\tCONSTRAINT ck_execution_observation_quantities_valid CHECK (shipped >= 0 AND received >= 0 AND cancelled >= 0 AND received <= shipped), \n\tCONSTRAINT fk_execution_observation_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_execution_observation_reservation_id_inventory_reservation FOREIGN KEY(reservation_id) REFERENCES inventory_reservation (id), \n\tCONSTRAINT fk_execution_observation_connection_id_integration_connection FOREIGN KEY(connection_id) REFERENCES integration_connection (id), \n\tCONSTRAINT uq_execution_observation_tenant_ref UNIQUE (tenant_id, id), \n\tCONSTRAINT fk_execution_observation_connection_id_tenant FOREIGN KEY(tenant_id, connection_id) REFERENCES integration_connection (tenant_id, id), \n\tCONSTRAINT fk_execution_observation_reservation_id_tenant FOREIGN KEY(tenant_id, reservation_id) REFERENCES inventory_reservation (tenant_id, id)\n)\n\n')
    op.execute('CREATE INDEX ix_execution_observation_reservation_id ON execution_observation (reservation_id)')
    op.execute('CREATE INDEX ix_execution_observation_tenant_id ON execution_observation (tenant_id)')
    op.execute("ALTER TABLE execution_observation ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON execution_observation USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute('\nCREATE TABLE inventory_settlement (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\treservation_id UUID NOT NULL, \n\tobservation_id UUID NOT NULL, \n\tsource_check_id UUID NOT NULL, \n\tdestination_check_id UUID, \n\tevidence TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_inventory_settlement PRIMARY KEY (id), \n\tCONSTRAINT uq_inventory_settlement_tenant_id_observation_id UNIQUE (tenant_id, observation_id), \n\tCONSTRAINT fk_inventory_settlement_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_inventory_settlement_reservation_id_inventory_reservation FOREIGN KEY(reservation_id) REFERENCES inventory_reservation (id), \n\tCONSTRAINT fk_inventory_settlement_observation_id_execution_observation FOREIGN KEY(observation_id) REFERENCES execution_observation (id), \n\tCONSTRAINT fk_inventory_settlement_source_check_id_source_reconciliation FOREIGN KEY(source_check_id) REFERENCES source_reconciliation (id), \n\tCONSTRAINT fk_inventory_settlement_destination_check_id_source_rec_78d0 FOREIGN KEY(destination_check_id) REFERENCES source_reconciliation (id), \n\tCONSTRAINT uq_inventory_settlement_tenant_ref UNIQUE (tenant_id, id), \n\tCONSTRAINT fk_inventory_settlement_reservation_id_tenant FOREIGN KEY(tenant_id, reservation_id) REFERENCES inventory_reservation (tenant_id, id), \n\tCONSTRAINT fk_inventory_settlement_source_check_id_tenant FOREIGN KEY(tenant_id, source_check_id) REFERENCES source_reconciliation (tenant_id, id), \n\tCONSTRAINT fk_inventory_settlement_destination_check_id_tenant FOREIGN KEY(tenant_id, destination_check_id) REFERENCES source_reconciliation (tenant_id, id), \n\tCONSTRAINT fk_inventory_settlement_observation_id_tenant FOREIGN KEY(tenant_id, observation_id) REFERENCES execution_observation (tenant_id, id)\n)\n\n')
    op.execute('CREATE INDEX ix_inventory_settlement_tenant_id ON inventory_settlement (tenant_id)')
    op.execute("ALTER TABLE inventory_settlement ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON inventory_settlement USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute("CREATE FUNCTION aegis_immutable_operation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'operation evidence is immutable'; END $$")
    op.execute("CREATE TRIGGER immutable_operation BEFORE UPDATE OR DELETE ON source_reconciliation FOR EACH ROW EXECUTE FUNCTION aegis_immutable_operation()")
    op.execute("CREATE TRIGGER immutable_operation BEFORE UPDATE OR DELETE ON execution_observation FOR EACH ROW EXECUTE FUNCTION aegis_immutable_operation()")
    op.execute("CREATE TRIGGER immutable_operation BEFORE UPDATE OR DELETE ON inventory_settlement FOR EACH ROW EXECUTE FUNCTION aegis_immutable_operation()")


def downgrade():
    op.drop_table("inventory_settlement")
    op.drop_table("execution_observation")
    op.drop_table("source_reconciliation")
    op.drop_table("external_identity")
    op.drop_table("oauth_attempt")
    op.execute("DROP FUNCTION aegis_immutable_operation()")
    op.drop_constraint("ck_inventory_reservation_counters_valid", "inventory_reservation")
    op.drop_constraint("ck_inventory_reservation_status_valid", "inventory_reservation")
    op.execute("UPDATE inventory_reservation SET status='released' WHERE status='consumed'")
    op.create_check_constraint(op.f("ck_inventory_reservation_status_valid"), "inventory_reservation", "status in ('held','released')")
    op.drop_column("inventory_reservation", "next_poll_at")
    op.drop_column("inventory_reservation", "settled_units")
    op.drop_column("inventory_reservation", "released_units")
    op.drop_column("inventory_reservation", "consumed_units")
    op.drop_column("integration_connection", "is_mock")
