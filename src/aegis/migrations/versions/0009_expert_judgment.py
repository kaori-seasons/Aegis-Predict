"""Launch reference profiles and private Delphi judgments."""

from alembic import op

revision = "0009_expert_judgment"
down_revision = "0008_commerce_signals"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE launch_profile_revision (
	sku_id UUID NOT NULL, 
	location_id UUID NOT NULL, 
	channel VARCHAR(80) NOT NULL, 
	positioning TEXT NOT NULL, 
	pricing_strategy TEXT NOT NULL, 
	retail_price FLOAT, 
	currency VARCHAR(3) NOT NULL, 
	price_basis VARCHAR(20) NOT NULL, 
	launch_plan TEXT NOT NULL, 
	initial_supply_units INTEGER, 
	first_batch_scope VARCHAR(30) NOT NULL, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	raw_record_id BIGINT NOT NULL, 
	source_system VARCHAR(80) NOT NULL, 
	revision INTEGER NOT NULL, 
	published_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	received_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	available_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	is_deleted BOOLEAN NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_launch_profile_revision PRIMARY KEY (id), 
	CONSTRAINT uq_launch_profile_identity UNIQUE (tenant_id, source_system, sku_id, location_id, channel, revision), 
	CONSTRAINT ck_launch_profile_revision_revision_positive CHECK (revision>=1), 
	CONSTRAINT ck_launch_profile_revision_price_valid CHECK (retail_price IS NULL OR (retail_price>=0 AND retail_price<1e12)), 
	CONSTRAINT ck_launch_profile_revision_initial_supply_valid CHECK (initial_supply_units IS NULL OR initial_supply_units BETWEEN 0 AND 10000000), 
	CONSTRAINT ck_launch_profile_revision_batch_scope_valid CHECK (first_batch_scope IN ('unknown','warehouse_channel','sku_launch')), 
	CONSTRAINT ck_launch_profile_revision_price_basis_valid CHECK (price_basis IN ('unknown','tax_inclusive','tax_exclusive')), 
	CONSTRAINT ck_launch_profile_revision_availability_valid CHECK (available_at>=received_at AND available_at>=published_at), 
	CONSTRAINT fk_launch_profile_revision_sku_id_sku FOREIGN KEY(sku_id) REFERENCES sku (id) ON DELETE RESTRICT, 
	CONSTRAINT fk_launch_profile_revision_location_id_location FOREIGN KEY(location_id) REFERENCES location (id) ON DELETE RESTRICT, 
	CONSTRAINT fk_launch_profile_revision_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, 
	CONSTRAINT fk_launch_profile_revision_raw_record_id_raw_record FOREIGN KEY(raw_record_id) REFERENCES raw_record (id), 
	CONSTRAINT uq_launch_profile_revision_tenant_ref UNIQUE (tenant_id, id), 
	CONSTRAINT fk_launch_profile_revision_location_id_tenant FOREIGN KEY(tenant_id, location_id) REFERENCES location (tenant_id, id), 
	CONSTRAINT fk_launch_profile_revision_sku_id_tenant FOREIGN KEY(tenant_id, sku_id) REFERENCES sku (tenant_id, id), 
	CONSTRAINT fk_launch_profile_revision_raw_record_id_tenant FOREIGN KEY(tenant_id, raw_record_id) REFERENCES raw_record (tenant_id, id)
)

""")
    op.execute(
        "CREATE INDEX ix_launch_profile_asof ON launch_profile_revision (tenant_id, source_system, sku_id, location_id, available_at)"
    )
    op.execute(
        "CREATE INDEX ix_launch_profile_revision_tenant_id ON launch_profile_revision (tenant_id)"
    )
    op.execute("ALTER TABLE launch_profile_revision ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON launch_profile_revision USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)"
    )
    op.execute(
        "CREATE TRIGGER decision_immutable BEFORE UPDATE OR DELETE ON launch_profile_revision FOR EACH ROW EXECUTE FUNCTION decision_immutable()"
    )
    op.execute("""
CREATE TABLE expert_response (
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	study_id UUID NOT NULL, 
	round_number INTEGER NOT NULL, 
	user_id UUID NOT NULL, 
	payload JSONB NOT NULL, 
	digest VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_expert_response PRIMARY KEY (id), 
	CONSTRAINT uq_expert_response_tenant_id_study_id_round_number_user_id UNIQUE (tenant_id, study_id, round_number, user_id), 
	CONSTRAINT ck_expert_response_round_valid CHECK (round_number BETWEEN 1 AND 5), 
	CONSTRAINT fk_expert_response_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES tenant (id) ON DELETE RESTRICT, 
	CONSTRAINT fk_expert_response_study_id_decision_case FOREIGN KEY(study_id) REFERENCES decision_case (id), 
	CONSTRAINT fk_expert_response_user_id_app_user FOREIGN KEY(user_id) REFERENCES app_user (id), 
	CONSTRAINT uq_expert_response_tenant_ref UNIQUE (tenant_id, id), 
	CONSTRAINT fk_expert_response_study_id_tenant FOREIGN KEY(tenant_id, study_id) REFERENCES decision_case (tenant_id, id), 
	CONSTRAINT fk_expert_response_user_id_tenant FOREIGN KEY(tenant_id, user_id) REFERENCES app_user (tenant_id, id)
)

""")
    op.execute(
        "CREATE INDEX ix_expert_response_tenant_id ON expert_response (tenant_id)"
    )
    op.execute("ALTER TABLE expert_response ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON expert_response USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)"
    )
    op.execute(
        "CREATE TRIGGER decision_immutable BEFORE UPDATE OR DELETE ON expert_response FOR EACH ROW EXECUTE FUNCTION decision_immutable()"
    )
    op.execute(
        "CREATE TRIGGER decision_fact_capture AFTER INSERT ON launch_profile_revision FOR EACH ROW EXECUTE FUNCTION capture_decision_fact('id')"
    )
    op.execute(
        "CREATE TRIGGER commerce_server_clock BEFORE INSERT ON launch_profile_revision FOR EACH ROW EXECUTE FUNCTION commerce_revision_clock()"
    )
    op.drop_constraint(
        op.f("ck_decision_case_kind_valid"), "decision_case", type_="check"
    )
    op.create_check_constraint(
        "kind_valid",
        "decision_case",
        "kind in ('bom_draft','override','model_release','risk','expert_study')",
    )
    op.execute(
        "CREATE FUNCTION expert_response_clock() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.created_at:=clock_timestamp(); RETURN NEW; END; $$"
    )
    op.execute(
        "CREATE TRIGGER expert_response_clock BEFORE INSERT ON expert_response FOR EACH ROW EXECUTE FUNCTION expert_response_clock()"
    )


def downgrade():
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM decision_case WHERE kind='expert_study') THEN RAISE EXCEPTION 'archive expert studies before downgrade'; END IF; END $$"
    )
    op.drop_table("expert_response")
    op.drop_table("launch_profile_revision")
    op.execute("DROP FUNCTION expert_response_clock()")
    op.drop_constraint(
        op.f("ck_decision_case_kind_valid"), "decision_case", type_="check"
    )
    op.create_check_constraint(
        "kind_valid",
        "decision_case",
        "kind in ('bom_draft','override','model_release','risk')",
    )
