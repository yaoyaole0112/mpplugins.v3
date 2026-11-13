import copy
import hashlib
import json
import re
import time
import types
from pathlib import Path, PurePosixPath

from .cloud_storage import CloudError, cloud_path
from .upload_adapter import UPLOAD_GATE, UploadNotStarted


def bounded_client(client, check=None):
    instance = copy.copy(client)
    original_stub = getattr(instance, "stub", None)
    if original_stub is None:
        return instance

    class DeadlineStub:
        def __getattr__(self, name):
            method = getattr(original_stub, name)
            def call(*args, **kwargs):
                if check:
                    check()
                kwargs.setdefault("timeout", 86400 if name == "RemoteUploadChannel" else 60)
                return method(*args, **kwargs)
            return call

    instance.stub = DeadlineStub()
    return instance


def bounded_storage(storage):
    instance = copy.copy(storage)
    instance.oper = copy.copy(storage.oper)
    instance.oper.client = bounded_client(storage.oper.client)
    return instance


def cloud_account(oper, path):
    mount = "/" + PurePosixPath(cloud_path(path)).parts[1]
    item = bounded_client(oper.client).find_file_by_path(mount)
    account = getattr(item, "CloudAPI", None)
    provider = getattr(account, "name", "")
    username = getattr(account, "userName", "")
    if (not isinstance(provider, str) or "115" not in provider
            or not isinstance(username, str) or not username
            or getattr(item, "isLocal", True)
            or getattr(item, "fileEncryptionType", 0) != 0):
        raise CloudError("CloudDrive目标必须是已登录的115非加密网盘目录")
    endpoint = getattr(oper, "_download_base", "")
    if not isinstance(endpoint, str) or not endpoint:
        raise CloudError("无法确认CloudDrive服务地址，保留本地文件")
    return [endpoint, mount, provider, username, str(getattr(item, "id", ""))]


def upload_scope(oper, target, folder):
    if cloud_path(target) == "/":
        raise CloudError("请选择CloudDrive中115挂载点下的上传目录")
    values = cloud_account(oper, target) + [cloud_path(target), folder]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _check_upload_tasks(client, path):
    terminal, finished = client.upload_terminal_enums()
    if finished is None or not terminal:
        raise CloudError("CloudDrive上传状态接口不兼容，保留本地文件")
    observed = set()
    expected_total = None
    for page in range(64):
        response = client.get_upload_file_list(get_all=False, items_per_page=100, page_number=page)
        total = getattr(response, "totalCount", None)
        if not isinstance(total, int) or isinstance(total, bool) or total < 0 or not hasattr(response, "uploadFiles"):
            raise CloudError("CloudDrive上传分页状态不完整，等待核验，不重复上传")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise CloudError("CloudDrive上传队列在分页期间变化，稍后重新核验")
        rows = list(response.uploadFiles)
        for task in rows:
            destination = getattr(task, "destPath", "")
            if destination and cloud_path(destination) == path and getattr(task, "statusEnum", None) != finished:
                raise CloudError("CloudDrive目标仍在上传或上传未成功；等待核验，不重复上传")
        identities = {(getattr(task, "key", ""), getattr(task, "destPath", "")) for task in rows}
        if len(identities) != len(rows) or observed.intersection(identities):
            raise CloudError("CloudDrive上传分页存在重复任务，无法确认是否遗漏未完成上传")
        observed.update(identities)
        if len(observed) == expected_total:
            return
        if not rows or len(observed) > expected_total:
            raise CloudError("CloudDrive上传任务分页读取不完整，等待核验")
    raise CloudError("CloudDrive上传队列超过安全分页上限，等待核验")


def uploaded_sha1(oper, item):
    path = cloud_path(item["path"])
    client = bounded_client(oper.client)
    _check_upload_tasks(client, path)
    parent = PurePosixPath(path).parent.as_posix()
    refreshed = list(client.get_sub_files(parent, force_refresh=True))
    if not any(str(getattr(entry, "id", "")) == str(item.get("fileid")) for entry in refreshed):
        raise CloudError("CloudDrive刷新云端目录失败，保留本地文件")
    remote = client.find_file_by_path(path)
    if (str(getattr(remote, "id", "")) != str(item.get("fileid"))
            or getattr(remote, "fullPathName", "") != path
            or getattr(remote, "size", None) != item.get("size")
            or not getattr(remote, "isCloudFile", False)
            or getattr(remote, "isLocal", True)
            or getattr(remote, "fileEncryptionType", 0) != 0):
        raise CloudError("CloudDrive云端文件身份或落盘状态未确认，保留本地文件")
    account = getattr(remote, "CloudAPI", None)
    if "115" not in str(getattr(account, "name", "")):
        raise CloudError("CloudDrive上传目标不是115网盘")
    digest = getattr(remote, "fileHashes", {}).get(2, "")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", digest):
        raise CloudError("CloudDrive尚未返回云端SHA1；保留本地文件等待核验，不转发、不重复上传")
    _check_upload_tasks(client, path)
    return digest.lower()


def upload_file(oper, target, path, identity, stopping, progress, checkpoint, rate_limit=8, on_start=None):
    from .local_upload import local_identity
    function = getattr(oper.upload, "__func__", None)
    if (not isinstance(function, types.FunctionType)
            or not {"global_vars", "transfer_process", "sleep"} <= function.__globals__.keys()
            or not callable(getattr(oper, "get_item_strict", None))):
        raise UploadNotStarted("CloudDrive上传接口不兼容，请更新CloudDrive2插件；未上传")
    original_stop = function.__globals__["global_vars"]
    last_checkpoint = [0.0]

    def check():
        if stopping.is_set():
            raise InterruptedError("插件停止，取消CloudDrive上传")
        if local_identity(path.stat()) != list(identity):
            raise OSError("CloudDrive上传期间本地文件变化，保留源文件")

    def wait(seconds):
        deadline = time.monotonic() + max(0, seconds)
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if stopping.wait(min(0.5, remaining)):
                check()
            if time.monotonic() - last_checkpoint[0] >= 1:
                checkpoint()
                last_checkpoint[0] = time.monotonic()

    def transfer_process(source):
        if Path(source).resolve() != path.resolve():
            raise CloudError("CloudDrive上传进度来源不一致")
        def update(percent):
            check()
            progress(percent)
            if time.monotonic() - last_checkpoint[0] >= 1:
                checkpoint()
                last_checkpoint[0] = time.monotonic()
        return update

    class StopProxy:
        def is_transfer_stopped(self, source):
            check()
            return original_stop.is_transfer_stopped(source)

        def __getattr__(self, name):
            return getattr(original_stop, name)

    client = bounded_client(oper.client, check)
    next_write = [0.0]

    class ClientProxy:
        def __getattr__(self, name):
            method = getattr(client, name)
            if name not in {"write_to_file", "remote_read_data"}:
                return method
            def send(*args, **kwargs):
                check()
                data = kwargs.get("data", args[2] if name == "write_to_file" and len(args) > 2 else b"")
                if rate_limit:
                    next_write[0] = max(next_write[0], time.monotonic()) + len(data) / (rate_limit * 1024 * 1024)
                    wait(max(0, next_write[0] - time.monotonic()))
                result = method(*args, **kwargs)
                check()
                return result
            return send

    while not UPLOAD_GATE.acquire(timeout=0.5):
        check()
    try:
        check()
        instance = copy.copy(oper)
        instance.client = ClientProxy()
        bindings = dict(function.__globals__, global_vars=StopProxy(),
                        transfer_process=transfer_process, sleep=wait)
        for owner in type(oper).__mro__:
            for name, method in vars(owner).items():
                if (name not in instance.__dict__ and isinstance(method, types.FunctionType)
                        and method.__globals__ is function.__globals__):
                    cloned = types.FunctionType(method.__code__, bindings, method.__name__,
                                                method.__defaults__, method.__closure__)
                    cloned.__kwdefaults__ = method.__kwdefaults__
                    setattr(instance, name, types.MethodType(cloned, instance))
        existing = instance.get_item_strict(Path(target.path) / path.name)
        if existing is not None:
            return existing
        attempt = getattr(instance, "_upload_once", None)
        if callable(attempt):
            def upload_once(*args, **kwargs):
                result = attempt(*args, **kwargs)
                if result is None:
                    raise CloudError("CloudDrive远程上传结果未确认；停止内部重复上传，等待目标核验")
                return result
            instance._upload_once = upload_once
        if on_start:
            on_start()
        result = instance.upload(target, path, new_name=path.name)
        check()
        return result
    finally:
        UPLOAD_GATE.release()
