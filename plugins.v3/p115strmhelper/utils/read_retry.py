from functools import wraps
from http.client import IncompleteRead, RemoteDisconnected
from time import sleep

from httpx import NetworkError, RemoteProtocolError, TimeoutException
from urllib3_future.exceptions import (
    MaxRetryError,
    NewConnectionError,
    ProtocolError,
    TimeoutError as UrllibTimeoutError,
)
from urllib3_future_request import request

from app.sdk.logging import logger


def _is_transient_read_error(error: Exception) -> bool:
    if isinstance(error, MaxRetryError):
        error = error.reason
    return isinstance(
        error,
        (
            ProtocolError,
            UrllibTimeoutError,
            NewConnectionError,
            NetworkError,
            RemoteProtocolError,
            TimeoutException,
            IncompleteRead,
            RemoteDisconnected,
            ConnectionError,
            TimeoutError,
        ),
    )


@wraps(request)
def retry_read_request(url, method="GET", *, async_=False, **kwargs):
    if async_ or method.upper() not in {"GET", "HEAD"}:
        return request(url=url, method=method, async_=async_, **kwargs)
    for attempt in range(3):
        try:
            return request(url=url, method=method, async_=False, **kwargs)
        except Exception as error:
            if attempt == 2 or not _is_transient_read_error(error):
                raise
            delay = 2 ** attempt
            logger.warning(
                f"【全量STRM生成】云端只读请求连接中断或超时（{type(error).__name__}），"
                f"{delay} 秒后重试当前请求（{attempt + 1}/2），保留已扫描进度"
            )
            sleep(delay)
