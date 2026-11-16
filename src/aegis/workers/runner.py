from __future__ import annotations

import logging
import signal
import threading
from ..core.config import get_settings
from ..core.logging import configure_logging


def run(kind: str, *, once: bool = False) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    while not stopping.is_set():
        try:
            if kind == "sync":
                from ..integration.runtime import normalize_tenant, run_sync_once, tenants_with_pending_raw
                from ..integration.writeback import run_writeback_once
                run_sync_once()
                run_writeback_once()
                from ..inventory.readback import poll_execution_once
                poll_execution_once()
                for tenant_id in tenants_with_pending_raw():
                    if stopping.is_set():
                        break
                    normalize_tenant(tenant_id, max_batches=5)
            elif kind == "plan":
                from ..planning.lifecycle import run_plan_once
                run_plan_once()
            elif kind == "scheduler":
                from ..integration.runtime import schedule_due_syncs, sweep_expired_leases
                from ..integration.outbox import dispatch_outbox
                sweep_expired_leases()
                schedule_due_syncs()
                dispatch_outbox(limit=20)
            else:
                raise ValueError(f"unknown worker: {kind}")
        except Exception:
            logging.getLogger("aegis.worker").exception("worker iteration failed", extra={"kind": kind})
            if once:
                raise
        if once:
            return
        stopping.wait(settings.worker_poll_seconds)
