#!/usr/bin/env python3
"""Keep MoviePilot V3's management API from masking 115 listing failures."""

import argparse
from pathlib import Path


def _replace(source: str, old: str, new: str) -> str:
    if source.count(new) == 1:
        return source
    if source.count(old) != 1:
        raise ValueError(f"unsupported MoviePilot source: {old.splitlines()[0]}")
    return source.replace(old, new, 1)


def patch_endpoint(source: str) -> str:
    source = _replace(
        source,
        "from app.schemas.file import FileItem as _SchemaFileItem\n",
        "from app.schemas.exception import StorageQueryError\n"
        "from app.schemas.file import FileItem as _SchemaFileItem\n",
    )
    source = _replace(
        source,
        "    return _list_files(fileitem=fileitem, sort=sort, keyword=keyword)\n\n\ndef _list_files(",
        """    return _list_files(
        fileitem=fileitem, sort=sort, keyword=keyword,
        strict_115=fileitem.storage in {"u115", "115网盘Plus"},
    )


def _list_files(""",
    )
    source = _replace(
        source,
        "    keyword: Optional[str],\n) -> List[_SchemaFileItem]:",
        "    keyword: Optional[str],\n    strict_115: bool = False,\n) -> List[_SchemaFileItem]:",
    )
    return _replace(
        source,
        "    file_list = StorageChain().list_files(fileitem)\n",
        """    chain = StorageChain()
    if strict_115:
        from app.modules.filemanager.storages.u115 import NoCheckInException

        try:
            file_list = chain.run_module_strict("list_files", fileitem=fileitem, recursion=False)
        except (StorageQueryError, NoCheckInException) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except FileNotFoundError as error:
            raise HTTPException(status_code=503, detail="115 网盘目录暂时无法读取，请稍后重试") from error
        if file_list is None:
            raise HTTPException(status_code=503, detail="115 存储未就绪，请检查登录或插件状态")
    else:
        file_list = chain.list_files(fileitem)
""",
    )


def patch_u115(source: str) -> str:
    source = _replace(
        source,
        """                _fileitem = self.get_item(Path(fileitem.path))
                if not _fileitem:
                    logger.warn(f"【115】获取目录 {fileitem.path} 失败！")
                    return []
                cid = _fileitem.fileid""",
        """                _fileitem = self.get_item_strict(Path(fileitem.path))
                if not _fileitem or _fileitem.type != "dir" or not _fileitem.fileid:
                    raise StorageQueryError("【115】找不到要浏览的目录")
                cid = _fileitem.fileid""",
    )
    return _replace(
        source,
        """            if resp is None:
                raise FileNotFoundError(f"【115】{fileitem.path} 检索出错！")
            if not isinstance(resp, list) or not resp:
                break""",
        """            if not isinstance(resp, list):
                if self._limit_until > time.time():
                    raise StorageQueryError("【115】开放接口访问达到上限，请稍后重试")
                raise StorageQueryError("【115】开放接口未能读取目录，请检查登录或稍后重试")
            if not resp:
                break""",
    )


def apply(app: Path) -> None:
    changes = {}
    for relative, patch in (
        ("api/endpoints/storage.py", patch_endpoint),
        ("modules/filemanager/storages/u115.py", patch_u115),
    ):
        path = app / relative
        old = path.read_text(encoding="utf-8")
        new = patch(old)
        if new != old:
            compile(new, str(path), "exec")
            changes[path] = new
    for path, text in changes.items():
        path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=Path("/app/app"))
    apply(parser.parse_args().app)
