"""Transactional outbox: events are written in the same transaction as the state change.

A dispatcher publishes them afterwards (at-least-once). Without a configured subscriber the
events are only logged and marked published, which keeps the table bounded.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..core.db import admin_session
from ..core.tenancy import current_trace_id
from ..core.util import canonical_json, json_safe, utcnow
from ..models import OutboxEvent

logger = logging.getLogger("aegis.outbox")

MAX_DISPATCH_ATTEMPTS = 20


def emit(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    topic: str,
    aggregate_type: str,
    aggregate_id: Any,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        OutboxEvent(
            tenant_id=tenant_id,
            topic=topic,
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            payload=json_safe(payload or {}),
            trace_id=current_trace_id(),
        )
    )


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _publish(client: httpx.Client | None, settings: Settings, event: OutboxEvent) -> None:
    document = {
        "id": event.id,
        "tenant_id": str(event.tenant_id),
        "topic": event.topic,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "payload": event.payload,
        "trace_id": event.trace_id,
        "created_at": event.created_at,
    }
    if client is None:
        logger.info("event %s", event.topic, extra={"event": json_safe(document)})
        return
    body = canonical_json(document).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Aegis-Event-Id": str(event.id)}
    if settings.event_webhook_secret:
        headers["X-Aegis-Signature"] = sign(settings.event_webhook_secret, body)
    response = client.post(settings.event_webhook_url, content=body, headers=headers)
    response.raise_for_status()


def dispatch_outbox(*, limit: int = 100, settings: Settings | None = None, client: httpx.Client | None = None) -> int:
    """Publish due events across tenants (owner connection; rows are locked with SKIP LOCKED)."""

    settings = settings or get_settings()
    own_client = None
    if client is None and settings.event_webhook_url:
        own_client = client = httpx.Client(timeout=10.0)
    published = 0
    try:
        with admin_session() as session:
            now = utcnow()
            events = session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.published_at.is_(None),
                    OutboxEvent.attempts < MAX_DISPATCH_ATTEMPTS,
                    (OutboxEvent.next_attempt_at.is_(None)) | (OutboxEvent.next_attempt_at <= now),
                )
                .order_by(OutboxEvent.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for event in events:
                try:
                    _publish(client, settings, event)
                except Exception as exc:  # noqa: BLE001 - every failure is retried with backoff
                    event.attempts += 1
                    event.last_error = str(exc)[:2000]
                    event.next_attempt_at = now + timedelta(seconds=min(3600, 5 * 2 ** min(event.attempts, 10)))
                    logger.warning("outbox publish failed", extra={"event_id": event.id, "error": str(exc)})
                    continue
                event.published_at = now
                event.attempts += 1
                published += 1
    finally:
        if own_client is not None:
            own_client.close()
    return published


def purge_published(*, older_than_days: int = 14) -> int:
    with admin_session() as session:
        cutoff = utcnow() - timedelta(days=older_than_days)
        result = session.execute(
            OutboxEvent.__table__.delete().where(
                OutboxEvent.published_at.is_not(None), OutboxEvent.published_at < cutoff
            )
        )
        return result.rowcount or 0
