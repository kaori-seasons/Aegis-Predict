from __future__ import annotations

import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..models import APPEND_ONLY_TABLES
from .config import get_settings

_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def alembic_config(url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["url"] = url or get_settings().admin_database_url
    return config


def upgrade(revision: str = "head", url: str | None = None) -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(revision: str, url: str | None = None) -> None:
    command.downgrade(alembic_config(url), revision)


def _role(name: str) -> str:
    if not _ROLE_RE.match(name):
        raise ValueError(f"invalid role name: {name!r}")
    return name


def ensure_app_role(connection: Connection, role: str, password: str | None) -> None:
    """Create the non-owner, non-BYPASSRLS application login role if it does not exist."""

    role = _role(role)
    exists = connection.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
    if not exists:
        if password:
            quoted = connection.execute(text("SELECT quote_literal(:p)"), {"p": password}).scalar_one()
            connection.execute(text(f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {quoted}"))
        else:
            connection.execute(text(f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS"))


def grant_app_role(connection: Connection, role: str) -> None:
    role = _role(role)
    connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    connection.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"))
    for table in sorted(APPEND_ONLY_TABLES):
        connection.execute(text(f"REVOKE UPDATE, DELETE ON {table} FROM {role}"))
    connection.execute(text(f"REVOKE ALL ON alembic_version FROM {role}"))
    connection.execute(text(f"GRANT SELECT ON alembic_version TO {role}"))
    connection.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"))


def current_revision(connection: Connection) -> str | None:
    try:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001 - table missing before the first migration
        return None


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head()
