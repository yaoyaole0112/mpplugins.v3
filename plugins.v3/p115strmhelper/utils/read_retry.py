from functools import wraps
from http.client import IncompleteRead, RemoteDisconnected
from time import sleep
from urllib.parse import urlsplit

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


def _is_read_request(url, method, kwargs) -> bool:
    if method.upper() in {"GET", "HEAD"}:
        return True
    if method.upper() != "POST":
        return False
    try:
        endpoint = urlsplit(str(url))
        port = endpoint.port
    except ValueError:
        return False
    data = kwargs.get("data")
    return (
        endpoint.scheme == "https"
        and endpoint.hostname == "webapi.115.com"
        and port in {None, 443}
        and endpoint.path == "/files/file"
        and not endpoint.query
        and endpoint.username is None
        and endpoint.password is None
        and isinstance(data, dict)
        and set(data) == {"file_id"}
        and isinstance(data["file_id"], (str, int))
        and not kwargs.get("files")
        and kwargs.get("json") is None
    )


@wraps(request)
def retry_read_request(url, method="GET", *, async_=False, **kwargs):
    if async_ or not _is_read_request(url, method, kwargs):
        return request(url=url, method=method, async_=async_, **kwargs)
    for attempt in range(3):
        try:
            return request(url=url, method=method, async_=False, **kwargs)
        except Exception as error:
            if attempt == 2 or not _is_transient_read_error(error):
                raise
            delay = 2 ** attempt
            logger.warning(
                f"【全量STRM生成】云端只读请求（{method.upper()}）连接中断或超时（{type(error).__name__}），"
                f"{delay} 秒后重试当前请求（{attempt + 1}/2），保留已扫描进度"
            )
            sleep(delay)
