"""Lease heartbeat for slow, blocking platform requests; every commit still applies fencing."""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager


@contextmanager
def heartbeat(renew, lease_seconds: int):
    stopping = threading.Event()
    def loop():
        while not stopping.wait(max(0.1, lease_seconds / 3)):
            try:
                renew()
            except Exception:
                logging.getLogger("aegis.lease").warning("lease heartbeat failed; fencing will prevent stale commits")
                break
    thread = threading.Thread(target=loop, daemon=True, name="aegis-lease-heartbeat")
    thread.start()
    try:
        yield
    finally:
        stopping.set()
        thread.join(timeout=2)
