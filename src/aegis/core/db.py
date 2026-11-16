from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .tenancy import reset_current_tenant, set_current_tenant


def _build_engine(url: str) -> Engine:
    settings = get_settings()
    engine = create_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_size,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )
    timeout = int(settings.db_statement_timeout_ms)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:  # pragma: no cover - trivial
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = {timeout}")
            cursor.execute("SET TIME ZONE 'UTC'")
        dbapi_connection.commit()

    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return _build_engine(get_settings().database_url)


@lru_cache(maxsize=1)
def get_admin_engine() -> Engine:
    return _build_engine(get_settings().admin_database_url)


def dispose_engines() -> None:
    for factory in (get_engine, get_admin_engine):
        if factory.cache_info().currsize:
            factory().dispose()
        factory.cache_clear()


def _session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@contextmanager
def tenant_session(tenant_id: uuid.UUID | str, *, isolation_level: str | None = None,
                   bind_context: bool = True) -> Iterator[Session]:
    """One transaction bound to a tenant.

    ``SET LOCAL app.tenant_id`` scopes every RLS policy to the tenant for the lifetime of
    the transaction only, so a pooled connection never leaks the tenant to the next user.
    """

    tenant_uuid = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    engine = get_engine()
    if isolation_level:
        engine = engine.execution_options(isolation_level=isolation_level)
    session = _session_factory(engine)()
    token = set_current_tenant(tenant_uuid) if bind_context else None
    try:
        with session.begin():
            session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(tenant_uuid)})
            yield session
    finally:
        session.close()
        if token is not None:
            reset_current_tenant(token)


@contextmanager
def global_session() -> Iterator[Session]:
    """App-role transaction without tenant (only global tables such as tenant / user_session are visible)."""

    session = _session_factory(get_engine())()
    try:
        with session.begin():
            yield session
    finally:
        session.close()


@contextmanager
def admin_session() -> Iterator[Session]:
    """Owner-role transaction that bypasses RLS. Restricted to schedulers, job claiming and bootstrap."""

    session = _session_factory(get_admin_engine())()
    try:
        with session.begin():
            yield session
    finally:
        session.close()


def ping() -> bool:
    with get_engine().connect() as connection:
        return connection.execute(text("SELECT 1")).scalar_one() == 1
