"""Data coverage, source-separated demand and inventory evidence.

Revision ID: 0003_foundation
Revises: 0002_production_invariants
Existing orders/daily totals need raw replay; no authority is assumed during migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_foundation"
down_revision = "0002_production_invariants"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("\nCREATE TABLE demand_observation (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tsource_system VARCHAR(80) NOT NULL, \n\tkind VARCHAR(30) NOT NULL, \n\tsku_id UUID NOT NULL, \n\tlocation_id UUID NOT NULL, \n\tchannel VARCHAR(80) NOT NULL, \n\tdemand_date DATE NOT NULL, \n\tunits INTEGER NOT NULL, \n\tstate VARCHAR(20) NOT NULL, \n\tsource_updated_at TIMESTAMP WITH TIME ZONE, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_demand_observation PRIMARY KEY (id), \n\tCONSTRAINT uq_demand_observation_tenant_id_source_system_kind_sku__eb27 UNIQUE (tenant_id, source_system, kind, sku_id, location_id, channel, demand_date), \n\tCONSTRAINT ck_demand_observation_units_nonnegative CHECK (units >= 0), \n\tCONSTRAINT ck_demand_observation_state_valid CHECK (state in ('observed','censored','missing')), \n\tCONSTRAINT ck_demand_observation_kind_valid CHECK (kind in ('order_lines','daily_demand')), \n\tCONSTRAINT fk_demand_observation_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_demand_observation_sku_id_sku FOREIGN KEY(sku_id) REFERENCES sku (id), \n\tCONSTRAINT fk_demand_observation_location_id_location FOREIGN KEY(location_id) REFERENCES location (id), \n\tCONSTRAINT uq_demand_observation_tenant_ref UNIQUE (tenant_id, id), \n\tCONSTRAINT fk_demand_observation_location_id_tenant FOREIGN KEY(tenant_id, location_id) REFERENCES location (tenant_id, id), \n\tCONSTRAINT fk_demand_observation_sku_id_tenant FOREIGN KEY(tenant_id, sku_id) REFERENCES sku (tenant_id, id)\n)\n\n")
    op.execute('CREATE INDEX ix_demand_observation_lookup ON demand_observation (tenant_id, location_id, demand_date)')
    op.execute('CREATE INDEX ix_demand_observation_tenant_id ON demand_observation (tenant_id)')
    op.execute("ALTER TABLE demand_observation ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON demand_observation USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute("\nCREATE TABLE source_authority (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tdomain VARCHAR(20) NOT NULL, \n\tlocation_id UUID NOT NULL, \n\tchannel VARCHAR(80) NOT NULL, \n\tconnection_id UUID NOT NULL, \n\tstream VARCHAR(60) NOT NULL, \n\tinbound_mode VARCHAR(20) NOT NULL, \n\tcoverage_start DATE, \n\tnote TEXT NOT NULL, \n\tapproved_by VARCHAR(320) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_source_authority PRIMARY KEY (id), \n\tCONSTRAINT uq_source_authority_tenant_id_domain_location_id_channel UNIQUE (tenant_id, domain, location_id, channel), \n\tCONSTRAINT ck_source_authority_domain_valid CHECK (domain in ('demand','inventory','inbound')), \n\tCONSTRAINT ck_source_authority_stream_valid CHECK (stream in ('order_lines','daily_demand','inventory_snapshots','purchase_orders')), \n\tCONSTRAINT ck_source_authority_inbound_mode_valid CHECK (inbound_mode in ('linked','disjoint')), \n\tCONSTRAINT ck_source_authority_channel_valid CHECK (domain = 'demand' OR channel = '*'), \n\tCONSTRAINT fk_source_authority_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_source_authority_location_id_location FOREIGN KEY(location_id) REFERENCES location (id), \n\tCONSTRAINT fk_source_authority_connection_id_integration_connection FOREIGN KEY(connection_id) REFERENCES integration_connection (id), \n\tCONSTRAINT uq_source_authority_tenant_ref UNIQUE (tenant_id, id), \n\tCONSTRAINT fk_source_authority_location_id_tenant FOREIGN KEY(tenant_id, location_id) REFERENCES location (tenant_id, id), \n\tCONSTRAINT fk_source_authority_connection_id_tenant FOREIGN KEY(tenant_id, connection_id) REFERENCES integration_connection (tenant_id, id)\n)\n\n")
    op.execute('CREATE INDEX ix_source_authority_tenant_id ON source_authority (tenant_id)')
    op.execute("ALTER TABLE source_authority ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON source_authority USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute("\nCREATE TABLE ingestion_batch (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tconnection_id UUID NOT NULL, \n\tsync_run_id UUID, \n\tstream VARCHAR(60) NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\twindow_start TIMESTAMP WITH TIME ZONE, \n\twindow_end TIMESTAMP WITH TIME ZONE, \n\tscope JSONB NOT NULL, \n\trecord_count INTEGER NOT NULL, \n\terror TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tlanded_at TIMESTAMP WITH TIME ZONE, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tCONSTRAINT pk_ingestion_batch PRIMARY KEY (id), \n\tCONSTRAINT ck_ingestion_batch_status_valid CHECK (status in ('landing','landed','complete','quarantined','failed')), \n\tCONSTRAINT fk_ingestion_batch_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_ingestion_batch_connection_id_integration_connection FOREIGN KEY(connection_id) REFERENCES integration_connection (id), \n\tCONSTRAINT fk_ingestion_batch_sync_run_id_sync_run FOREIGN KEY(sync_run_id) REFERENCES sync_run (id), \n\tCONSTRAINT uq_ingestion_batch_tenant_ref UNIQUE (tenant_id, id), \n\tCONSTRAINT fk_ingestion_batch_sync_run_id_tenant FOREIGN KEY(tenant_id, sync_run_id) REFERENCES sync_run (tenant_id, id), \n\tCONSTRAINT fk_ingestion_batch_connection_id_tenant FOREIGN KEY(tenant_id, connection_id) REFERENCES integration_connection (tenant_id, id)\n)\n\n")
    op.execute('CREATE INDEX ix_ingestion_batch_stream ON ingestion_batch (tenant_id, connection_id, stream, created_at)')
    op.execute('CREATE INDEX ix_ingestion_batch_tenant_id ON ingestion_batch (tenant_id)')
    op.execute("ALTER TABLE ingestion_batch ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON ingestion_batch USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute('\nCREATE TABLE ingestion_batch_record (\n\ttenant_id UUID NOT NULL, \n\tbatch_id UUID NOT NULL, \n\traw_record_id BIGINT NOT NULL, \n\tCONSTRAINT pk_ingestion_batch_record PRIMARY KEY (batch_id, raw_record_id), \n\tCONSTRAINT fk_ingestion_batch_record_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_ingestion_batch_record_batch_id_ingestion_batch FOREIGN KEY(batch_id) REFERENCES ingestion_batch (id), \n\tCONSTRAINT fk_ingestion_batch_record_raw_record_id_raw_record FOREIGN KEY(raw_record_id) REFERENCES raw_record (id), \n\tCONSTRAINT fk_ingestion_batch_record_raw_record_id_tenant FOREIGN KEY(tenant_id, raw_record_id) REFERENCES raw_record (tenant_id, id), \n\tCONSTRAINT fk_ingestion_batch_record_batch_id_tenant FOREIGN KEY(tenant_id, batch_id) REFERENCES ingestion_batch (tenant_id, id)\n)\n\n')
    op.execute('CREATE INDEX ix_ingestion_batch_record_tenant_id ON ingestion_batch_record (tenant_id)')
    op.execute("ALTER TABLE ingestion_batch_record ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON ingestion_batch_record USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute('\nCREATE TABLE inventory_inbound_link (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tsnapshot_id UUID NOT NULL, \n\tpurchase_order_line_id UUID NOT NULL, \n\tunits INTEGER NOT NULL, \n\tevidence TEXT NOT NULL, \n\tcreated_by VARCHAR(320) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_inventory_inbound_link PRIMARY KEY (id), \n\tCONSTRAINT uq_inventory_inbound_link_snapshot_id_purchase_order_line_id UNIQUE (snapshot_id, purchase_order_line_id), \n\tCONSTRAINT ck_inventory_inbound_link_units_positive CHECK (units > 0), \n\tCONSTRAINT fk_inventory_inbound_link_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_inventory_inbound_link_snapshot_id_inventory_snapshot FOREIGN KEY(snapshot_id) REFERENCES inventory_snapshot (id), \n\tCONSTRAINT fk_inventory_inbound_link_purchase_order_line_id_purcha_fedb FOREIGN KEY(purchase_order_line_id) REFERENCES purchase_order_line (id), \n\tCONSTRAINT uq_inventory_inbound_link_tenant_ref UNIQUE (tenant_id, id), \n\tCONSTRAINT fk_inventory_inbound_link_purchase_order_line_id_tenant FOREIGN KEY(tenant_id, purchase_order_line_id) REFERENCES purchase_order_line (tenant_id, id), \n\tCONSTRAINT fk_inventory_inbound_link_snapshot_id_tenant FOREIGN KEY(tenant_id, snapshot_id) REFERENCES inventory_snapshot (tenant_id, id)\n)\n\n')
    op.execute('CREATE INDEX ix_inventory_inbound_link_tenant_id ON inventory_inbound_link (tenant_id)')
    op.execute("ALTER TABLE inventory_inbound_link ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON inventory_inbound_link USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute("\nCREATE TABLE inventory_reservation (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tsku_id UUID NOT NULL, \n\tlocation_id UUID NOT NULL, \n\tsnapshot_id UUID NOT NULL, \n\tplan_line_id UUID NOT NULL, \n\tunits INTEGER NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\treleased_at TIMESTAMP WITH TIME ZONE, \n\trelease_evidence TEXT, \n\tCONSTRAINT pk_inventory_reservation PRIMARY KEY (id), \n\tCONSTRAINT uq_inventory_reservation_tenant_id_plan_line_id UNIQUE (tenant_id, plan_line_id), \n\tCONSTRAINT ck_inventory_reservation_units_positive CHECK (units > 0), \n\tCONSTRAINT ck_inventory_reservation_status_valid CHECK (status in ('held','released')), \n\tCONSTRAINT fk_inventory_reservation_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_inventory_reservation_sku_id_sku FOREIGN KEY(sku_id) REFERENCES sku (id), \n\tCONSTRAINT fk_inventory_reservation_location_id_location FOREIGN KEY(location_id) REFERENCES location (id), \n\tCONSTRAINT fk_inventory_reservation_snapshot_id_inventory_snapshot FOREIGN KEY(snapshot_id) REFERENCES inventory_snapshot (id), \n\tCONSTRAINT fk_inventory_reservation_plan_line_id_plan_line FOREIGN KEY(plan_line_id) REFERENCES plan_line (id), \n\tCONSTRAINT uq_inventory_reservation_tenant_ref UNIQUE (tenant_id, id), \n\tCONSTRAINT fk_inventory_reservation_sku_id_tenant FOREIGN KEY(tenant_id, sku_id) REFERENCES sku (tenant_id, id), \n\tCONSTRAINT fk_inventory_reservation_snapshot_id_tenant FOREIGN KEY(tenant_id, snapshot_id) REFERENCES inventory_snapshot (tenant_id, id), \n\tCONSTRAINT fk_inventory_reservation_plan_line_id_tenant FOREIGN KEY(tenant_id, plan_line_id) REFERENCES plan_line (tenant_id, id), \n\tCONSTRAINT fk_inventory_reservation_location_id_tenant FOREIGN KEY(tenant_id, location_id) REFERENCES location (tenant_id, id)\n)\n\n")
    op.execute('CREATE INDEX ix_inventory_reservation_stock ON inventory_reservation (tenant_id, sku_id, location_id, status)')
    op.execute('CREATE INDEX ix_inventory_reservation_tenant_id ON inventory_reservation (tenant_id)')
    op.execute("ALTER TABLE inventory_reservation ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON inventory_reservation USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)")
    op.add_column("ingestion_batch", sa.Column("digest", sa.String(64), nullable=True))
    op.add_column("inventory_snapshot", sa.Column("ingestion_batch_id", sa.UUID(), nullable=True))
    op.create_foreign_key("fk_inventory_snapshot_batch", "inventory_snapshot", "ingestion_batch", ["ingestion_batch_id"], ["id"])
    op.create_foreign_key("fk_inventory_snapshot_ingestion_batch_id_tenant", "inventory_snapshot", "ingestion_batch", ["tenant_id","ingestion_batch_id"], ["tenant_id","id"])
    op.add_column("order_line", sa.Column("demand_eligible", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("plan_run", sa.Column("mode", sa.String(20), nullable=False, server_default="shadow"))
    op.create_check_constraint(op.f("ck_plan_run_mode_valid"), "plan_run", "mode in ('shadow','execution')")
    op.create_check_constraint(op.f("ck_source_authority_stream_domain"), "source_authority", "(domain='demand' AND stream in ('order_lines','daily_demand')) OR (domain='inventory' AND stream='inventory_snapshots') OR (domain='inbound' AND stream='purchase_orders')")
    op.execute("""
      CREATE FUNCTION aegis_authority_scope_guard() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text || NEW.location_id::text || NEW.domain,0));
        IF EXISTS (SELECT 1 FROM source_authority a WHERE a.tenant_id=NEW.tenant_id
          AND a.location_id=NEW.location_id AND a.domain=NEW.domain AND a.id<>NEW.id
          AND (a.channel='*' OR NEW.channel='*')) THEN
          RAISE EXCEPTION 'overlapping source authority';
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER authority_scope_guard BEFORE INSERT OR UPDATE ON source_authority
        FOR EACH ROW EXECUTE FUNCTION aegis_authority_scope_guard();
      CREATE FUNCTION aegis_batch_member_guard() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NOT EXISTS (SELECT 1 FROM ingestion_batch b JOIN raw_record r
            ON r.connection_id=b.connection_id AND r.stream=b.stream AND r.tenant_id=b.tenant_id
            WHERE b.id=NEW.batch_id AND r.id=NEW.raw_record_id AND b.tenant_id=NEW.tenant_id AND b.status='landing') THEN
          RAISE EXCEPTION 'batch membership requires a matching landing batch';
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER batch_member_guard BEFORE INSERT OR UPDATE ON ingestion_batch_record
        FOR EACH ROW EXECUTE FUNCTION aegis_batch_member_guard();
      CREATE FUNCTION aegis_block_evidence_delete() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'evidence deletion requires a dedicated retention migration'; END $$;
    """)
    for table in ("raw_record", "plan_input_blob", "ingestion_batch_record"):
        op.execute(f"CREATE TRIGGER evidence_delete_guard BEFORE DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION aegis_block_evidence_delete()")
    # Existing plans were not explicitly classified and must never be released accidentally.
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='aegis_app') THEN
      GRANT SELECT,INSERT,UPDATE,DELETE ON ingestion_batch,ingestion_batch_record,source_authority,
         demand_observation,inventory_inbound_link,inventory_reservation TO aegis_app;
    END IF; END $$;""")


def downgrade():
    for table in ("raw_record", "plan_input_blob", "ingestion_batch_record"):
        op.execute(f"DROP TRIGGER evidence_delete_guard ON {table}")
    op.execute("DROP FUNCTION aegis_block_evidence_delete()")
    op.execute("DROP TRIGGER batch_member_guard ON ingestion_batch_record")
    op.execute("DROP FUNCTION aegis_batch_member_guard()")
    op.execute("DROP TRIGGER authority_scope_guard ON source_authority")
    op.execute("DROP FUNCTION aegis_authority_scope_guard()")
    op.drop_constraint("ck_source_authority_stream_domain", "source_authority")
    op.drop_constraint("ck_plan_run_mode_valid", "plan_run")
    op.drop_column("plan_run", "mode")
    op.drop_column("order_line", "demand_eligible")
    op.drop_constraint("fk_inventory_snapshot_ingestion_batch_id_tenant", "inventory_snapshot")
    op.drop_constraint("fk_inventory_snapshot_batch", "inventory_snapshot")
    op.drop_column("inventory_snapshot", "ingestion_batch_id")
    op.drop_table('inventory_reservation')
    op.drop_table('inventory_inbound_link')
    op.drop_table('ingestion_batch_record')
    op.drop_table('ingestion_batch')
    op.drop_table('source_authority')
    op.drop_table('demand_observation')
