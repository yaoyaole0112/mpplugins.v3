"""Process-wide Emby request pacing shared by all plugin instances."""

import asyncio
from contextlib import contextmanager
import threading
import time

import httpx


REQUEST_SLOTS = threading.BoundedSemaphore(2)
ENRICHMENT_SLOT = threading.Lock()
RESOLVE_SLOTS = threading.BoundedSemaphore(2)
PACE_LOCK = threading.Lock()
NEXT_REQUEST = 0.0


def reserve_delay(write=False):
    global NEXT_REQUEST
    with PACE_LOCK:
        now = time.monotonic()
        start = max(now, NEXT_REQUEST)
        NEXT_REQUEST = start + (0.5 if write else 0.15)
        return start - now


@contextmanager
def emby_request():
    with REQUEST_SLOTS:
        time.sleep(reserve_delay())
        yield


class EmbyClient(httpx.AsyncClient):
    def __init__(self, *args, active, **kwargs):
        super().__init__(*args, **kwargs)
        self.active = active

    async def request(self, method, url, **kwargs):
        self.active()
        while not REQUEST_SLOTS.acquire(blocking=False):
            await asyncio.sleep(0.05)
            self.active()
        try:
            await asyncio.sleep(reserve_delay(write=method.upper() not in {"GET", "HEAD"}))
            self.active()
            return await super().request(method, url, **kwargs)
        finally:
            REQUEST_SLOTS.release()
