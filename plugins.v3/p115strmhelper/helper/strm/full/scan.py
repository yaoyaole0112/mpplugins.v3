from http.client import IncompleteRead
from itertools import batched
from time import sleep
from typing import Any, Callable, Dict, Iterator, Set, Tuple


_NETWORK_RETRY_DELAYS = (1, 2)
_URLLIB_TRANSPORT_ERRORS = {
    "ConnectTimeoutError",
    "IncompleteRead",
    "ProtocolError",
    "ReadTimeoutError",
}


def _is_transient_network_error(error: Exception) -> bool:
    """
    检查异常链中是否包含可恢复的连接中断或超时
    """
    seen: Set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ConnectionError, TimeoutError, IncompleteRead)):
            return True
        if any(
            cls.__module__ in ("urllib3_future.exceptions", "urllib3.exceptions")
            and cls.__name__ in _URLLIB_TRANSPORT_ERRORS
            for cls in type(current).__mro__
        ):
            return True
        current = current.__cause__
    return False


def _is_missing_directory(error: Exception) -> bool:
    if (
        not isinstance(error, KeyError)
        or len(error.args) != 1
        or not str(error.args[0]).isdigit()
    ):
        return False
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module_name = frame.f_globals.get("__name__")
        if module_name == "p115client.tool.iterdir" and frame.f_code.co_name in (
            "get_ancestors",
            "get_path",
        ):
            return True
        traceback = traceback.tb_next
    return False


class DirectoryScanError(RuntimeError):
    """
    目录扫描恢复失败，携带可直接展示的错误说明
    """


class FullScanIterator:
    """
    分批扫描网盘文件，对目录节点缺失或临时网络故障进行有限重试

    Attributes:
        retried: 是否已重试扫描，用于禁止不稳定扫描后的删除操作
    """

    def __init__(
        self,
        client: Any,
        iter_func: Callable[..., Iterator[Dict[str, Any]]],
        iter_kwargs: Dict[str, Any],
        batch_size: int,
        path: str,
        warn: Callable[[str], None],
    ) -> None:
        """
        保存扫描参数与用户提示回调
        """
        self._client = client
        self._iter_func = iter_func
        self._kwargs = dict(iter_kwargs)
        self._batch_size = batch_size
        self._path = path
        self._warn = warn
        self.retried = False

    def __iter__(self) -> Iterator[Tuple[Dict[str, Any], ...]]:
        """
        只交付完整读取的批次，重试时跳过已经交付的文件

        :raises DirectoryScanError: 重试预算用尽后扫描仍然失败

        :yields Tuple: 待处理的文件批次
        """
        delivered: Set[str] = set()
        missing_directory_retried = False
        network_retries = 0
        network_retry_limit = len(_NETWORK_RETRY_DELAYS)
        while True:
            # 使用新的任务缓存，避免共享缓存清理或旧扫描后台线程影响重试
            kwargs = dict(self._kwargs, id_to_dirnode={})
            source = None
            batches = None
            delay = 0
            try:
                while True:
                    try:
                        if batches is None:
                            source = iter(self._iter_func(self._client, **kwargs))
                            batches = iter(batched(source, self._batch_size))
                        batch = next(batches)
                    except StopIteration:
                        return
                    except Exception as error:
                        missing_directory = _is_missing_directory(error)
                        if missing_directory and not missing_directory_retried:
                            missing_directory_retried = True
                            self.retried = True
                            self._warn(
                                f"【全量STRM生成】网盘目录“{self._path}”的目录信息不完整"
                                f"（缺失目录 ID：{error.args[0]}），正在清理本次任务的目录缓存"
                                "并从头重试（1/1），已提交的文件会自动跳过"
                            )
                            break
                        transient_network = (
                            not missing_directory and _is_transient_network_error(error)
                        )
                        if transient_network and network_retries < network_retry_limit:
                            delay = _NETWORK_RETRY_DELAYS[network_retries]
                            network_retries += 1
                            self.retried = True
                            self._warn(
                                f"【全量STRM生成】网盘目录“{self._path}”扫描遇到临时网络错误"
                                f"（{type(error).__name__}：{error}），"
                                f"等待 {delay} 秒后从头重试"
                                f"（{network_retries}/{network_retry_limit}），已提交的文件会自动跳过"
                            )
                            break
                        if missing_directory:
                            detail = (
                                "清理目录缓存并重试 1 次后，"
                                f"仍缺少目录信息（目录 ID：{error.args[0]}）。"
                                "请在 115 网页端检查目录是否可访问、是否有移动或删除。"
                            )
                        elif transient_network:
                            detail = (
                                f"网络重试 {network_retry_limit} 次后仍失败"
                                f"（{type(error).__name__}：{error}）。"
                                "请检查 MoviePilot 到 115 的网络连接，待连接稳定后重新全量同步。"
                            )
                        else:
                            raise
                        raise DirectoryScanError(
                            f"网盘目录“{self._path}”{detail}"
                            "本次扫描已停止，已提交的文件仍会完成处理。"
                            "本次不会清理该目录的失效文件。"
                        ) from error
                    pending = []
                    for item in batch:
                        file_id = str(item["id"])
                        if file_id not in delivered:
                            pending.append(item)
                            delivered.add(file_id)
                    if pending:
                        yield tuple(pending)
            finally:
                close = getattr(source, "close", None)
                if close is not None:
                    close()
            if delay:
                sleep(delay)
