"""Process-wide Emby request pacing shared by all plugin instances."""

import asyncio
from contextlib import contextmanager
import threading
import time

import httpx

from app.sdk.logging import logger


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


class ReadRetryClient(httpx.AsyncClient):
    def __init__(self, *args, active=None, service_name="Emby", **kwargs):
        super().__init__(*args, **kwargs)
        self.active = active
        self.service_name = service_name

    async def _request_once(self, method, url, **kwargs):
        return await super().request(method, url, **kwargs)

    async def request(self, method, url, **kwargs):
        attempts = 3 if method.upper() in {"GET", "HEAD"} else 1
        for attempt in range(1, attempts + 1):
            if self.active:
                self.active()
            try:
                return await self._request_once(method, url, **kwargs)
            except (httpx.RemoteProtocolError, httpx.NetworkError, httpx.TimeoutException) as error:
                logger.warning("媒体增强 %s：%s 请求网络中断 %s，尝试 %d/%d；%s",
                               self.service_name, method.upper(), type(error).__name__, attempt, attempts,
                               "稍后重试只读请求" if attempt < attempts else "停止请求，不自动重放写入")
                if attempt == attempts:
                    raise type(error)(
                        f"{self.service_name} {method.upper()} 网络连接中断，尝试 {attempts} 次后停止；未自动重放写入",
                        request=error.request) from None
                await asyncio.sleep(attempt)


class EmbyClient(ReadRetryClient):
    async def _request_once(self, method, url, **kwargs):
        self.active()
        while not REQUEST_SLOTS.acquire(blocking=False):
            await asyncio.sleep(0.05)
            self.active()
        try:
            await asyncio.sleep(reserve_delay(write=method.upper() not in {"GET", "HEAD"}))
            self.active()
            return await super()._request_once(method, url, **kwargs)
        finally:
            REQUEST_SLOTS.release()
