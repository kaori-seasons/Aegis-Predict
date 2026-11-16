"""Tenant-safe references, single active plan, and immutable evidence.

Revision ID: 0002_production_invariants
Revises: 0001
"""
from alembic import op
from sqlalchemy import text

revision = "0002_production_invariants"
down_revision = "0001"
branch_labels = None
depends_on = None


# Frozen schema identifiers: migrations never depend on future ORM changes.
TENANT_ID_TABLES = ('app_user', 'audit_log', 'fx_rate', 'id_mapping', 'integration_connection', 'location', 'outbox_event', 'policy_version', 'sku', 'inventory_snapshot', 'lane', 'market_signal', 'order_line', 'plan_line_pin', 'plan_run', 'purchase_order_line', 'reconciliation_result', 'sku_component', 'supplier_terms', 'sync_run', 'transport_option', 'user_session', 'plan_line', 'raw_record', 'quarantine_record', 'writeback_command')
REFERENCES = (('daily_demand', 'location_id', 'location'), ('daily_demand', 'sku_id', 'sku'), ('inventory_snapshot', 'location_id', 'location'), ('inventory_snapshot', 'sku_id', 'sku'), ('lane', 'from_location_id', 'location'), ('lane', 'to_location_id', 'location'), ('market_signal', 'location_id', 'location'), ('market_signal', 'sku_id', 'sku'), ('order_line', 'location_id', 'location'), ('order_line', 'sku_id', 'sku'), ('plan_line_pin', 'location_id', 'location'), ('plan_line_pin', 'sku_id', 'sku'), ('plan_run', 'policy_version_id', 'policy_version'), ('plan_run', 'writeback_connection_id', 'integration_connection'), ('purchase_order_line', 'location_id', 'location'), ('purchase_order_line', 'sku_id', 'sku'), ('reconciliation_result', 'connection_id', 'integration_connection'), ('sku_component', 'component_sku_id', 'sku'), ('sku_component', 'parent_sku_id', 'sku'), ('supplier_terms', 'sku_id', 'sku'), ('sync_run', 'connection_id', 'integration_connection'), ('sync_stream_state', 'connection_id', 'integration_connection'), ('transport_option', 'from_location_id', 'location'), ('transport_option', 'to_location_id', 'location'), ('user_session', 'user_id', 'app_user'), ('plan_input_blob', 'plan_run_id', 'plan_run'), ('plan_line', 'location_id', 'location'), ('plan_line', 'plan_run_id', 'plan_run'), ('plan_line', 'sku_id', 'sku'), ('raw_record', 'connection_id', 'integration_connection'), ('raw_record', 'sync_run_id', 'sync_run'), ('quarantine_record', 'connection_id', 'integration_connection'), ('quarantine_record', 'raw_record_id', 'raw_record'), ('writeback_command', 'connection_id', 'integration_connection'), ('writeback_command', 'plan_line_id', 'plan_line'), ('writeback_command', 'plan_run_id', 'plan_run'))


def upgrade():
    for table in TENANT_ID_TABLES:
        op.create_unique_constraint(f"uq_{table}_tenant_ref", table, ["tenant_id", "id"])
    for child, column, parent in REFERENCES:
        op.create_foreign_key(f"fk_{child}_{column}_tenant", child, parent, ["tenant_id", column], ["tenant_id", "id"])
    op.create_index("uq_plan_run_active", "plan_run", ["tenant_id"], unique=True,
                    postgresql_where=text("status in ('queued','solving','pending_review','released')"))
    op.create_unique_constraint("uq_connection_source", "integration_connection", ["tenant_id", "source_system"])
    op.execute("""
        CREATE FUNCTION aegis_immutable_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_TABLE_NAME = 'raw_record' THEN
            IF (to_jsonb(NEW) - ARRAY['normalize_status','normalized_at','mapper_version','quality_override'])
              IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['normalize_status','normalized_at','mapper_version','quality_override']) THEN
              RAISE EXCEPTION 'raw evidence is immutable';
            END IF;
          ELSIF TG_TABLE_NAME = 'policy_version' THEN
            IF (to_jsonb(NEW) - 'is_active') IS DISTINCT FROM (to_jsonb(OLD) - 'is_active') THEN
              RAISE EXCEPTION 'policy versions are immutable';
            END IF;
          ELSE
            RAISE EXCEPTION 'frozen plan input is immutable';
          END IF;
          RETURN NEW;
        END $$
    """)
    for table in ("raw_record", "policy_version", "plan_input_blob"):
        op.execute(f"CREATE TRIGGER immutable_evidence BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION aegis_immutable_evidence()")


def downgrade():
    for table in ("raw_record", "policy_version", "plan_input_blob"):
        op.execute(f"DROP TRIGGER immutable_evidence ON {table}")
    op.execute("DROP FUNCTION aegis_immutable_evidence()")
    op.drop_constraint("uq_connection_source", "integration_connection")
    op.drop_index("uq_plan_run_active", table_name="plan_run")
    for child, column, parent in reversed(list(REFERENCES)):
        op.drop_constraint(f"fk_{child}_{column}_tenant", child)
    for table in reversed(TENANT_ID_TABLES):
        op.drop_constraint(f"uq_{table}_tenant_ref", table)
