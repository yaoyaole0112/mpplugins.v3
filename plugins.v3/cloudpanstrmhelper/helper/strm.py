from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Set
from urllib.parse import quote

from app.chain.storage import StorageChain
from app.core.config import settings
from app.log import logger
from app.schemas import FileItem

from .path import local_mapped_path, local_strm_path, parse_path_pairs, split_exts


class StrmSyncHelper:
    """
    基于 MoviePilot StorageChain 的 STRM 全量/增量同步
    """

    def __init__(
        self,
        storage: str,
        moviepilot_address: str,
        mediaext: str,
        sidecar_ext: str,
        overwrite_mode: str = "never",
        auto_download_sidecar: bool = False,
        strm_url_mode: str = "plugin",
        strm_url_template: str = "",
        plugin_id: str = "CloudPanStrmHelper",
    ):
        self.storage = storage
        self.moviepilot_address = (moviepilot_address or "").rstrip("/")
        self.mediaext = split_exts(mediaext)
        self.sidecar_ext = split_exts(sidecar_ext)
        self.overwrite_mode = overwrite_mode or "never"
        self.auto_download_sidecar = auto_download_sidecar
        self.strm_url_mode = strm_url_mode or "plugin"
        self.strm_url_template = strm_url_template or ""
        self.plugin_id = plugin_id
        self.chain = StorageChain()
        self.strm_count = 0
        self.strm_skip_count = 0
        self.strm_fail_count = 0
        self.sidecar_count = 0
        self.remove_count = 0
        self.fail_dict: Dict[str, str] = {}

    def iter_files(self, pan_dir: str) -> Iterable[FileItem]:
        root = self.chain.get_file_item(storage=self.storage, path=Path(pan_dir))
        if not root:
            logger.error("【STRM同步】网盘目录不存在: %s %s", self.storage, pan_dir)
            return
        yield from self._walk(root)

    def _walk(self, fileitem: FileItem) -> Iterable[FileItem]:
        items = None
        try:
            items = self.chain.list_files(fileitem, recursion=True)
        except TypeError:
            items = None
        except Exception as err:
            logger.warning("【STRM同步】递归列出失败，改用逐层扫描: %s", err)
            items = None
        if items:
            for item in items:
                if getattr(item, "type", None) != "dir":
                    yield item
            return
        try:
            children = self.chain.list_files(fileitem) or []
        except Exception as err:
            logger.warning("【STRM同步】列出目录失败 %s: %s", fileitem.path, err)
            return
        for item in children:
            if getattr(item, "type", None) == "dir":
                yield from self._walk(item)
            else:
                yield item

    def build_strm_url(self, fileitem: FileItem) -> str:
        pan_path = fileitem.path or ""
        encoded_path = quote(pan_path, safe="")
        download_url = getattr(fileitem, "download_url", None)
        values = {
            "address": self.moviepilot_address,
            "storage": self.storage,
            "storage_encoded": quote(self.storage, safe=""),
            "path": pan_path,
            "path_encoded": encoded_path,
            "name": fileitem.name or "",
            "fileid": getattr(fileitem, "fileid", "") or "",
            "pickcode": getattr(fileitem, "pickcode", "") or "",
            "download_url": download_url or "",
        }
        if self.strm_url_mode == "download" and download_url:
            return str(download_url)
        if self.strm_url_mode == "template" and self.strm_url_template:
            try:
                return self.strm_url_template.format(**values)
            except Exception as err:
                logger.error("【STRM同步】URL 模板渲染失败，回退插件 302: %s", err)
        if self.strm_url_mode == "115" and values["pickcode"]:
            url = (
                f"{self.moviepilot_address}/api/v1/plugin/P115StrmHelper/redirect_url"
                f"?apikey={settings.API_TOKEN}&pickcode={values['pickcode']}"
            )
            if fileitem.name:
                url += f"&file_name={quote(fileitem.name)}"
            return url
        return (
            f"{self.moviepilot_address}/api/v1/plugin/{self.plugin_id}/redirect_url"
            f"?apikey={settings.API_TOKEN}"
            f"&storage={values['storage_encoded']}&path={encoded_path}"
        )

    def write_strm(
        self, local_dir: str, pan_dir: str, fileitem: FileItem, overwrite: bool = False
    ) -> Optional[str]:
        strm_path = local_strm_path(local_dir, pan_dir, fileitem.path)
        try:
            if strm_path.exists() and self.overwrite_mode == "never" and not overwrite:
                self.strm_skip_count += 1
                return str(strm_path)
            strm_path.parent.mkdir(parents=True, exist_ok=True)
            strm_path.write_text(self.build_strm_url(fileitem), encoding="utf-8")
            self.strm_count += 1
            logger.info("【STRM同步】生成 STRM: %s", strm_path)
            return str(strm_path)
        except Exception as err:
            self.strm_fail_count += 1
            self.fail_dict[str(strm_path)] = str(err)
            logger.error("【STRM同步】生成失败 %s: %s", strm_path, err)
            return None

    def download_sidecar(self, local_dir: str, pan_dir: str, fileitem: FileItem) -> None:
        if not self.auto_download_sidecar:
            return
        dest = local_mapped_path(local_dir, pan_dir, fileitem.path)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            saved = None
            try:
                saved = self.chain.download_file(fileitem, dest.parent)
            except TypeError:
                saved = self.chain.download_file(fileitem)
            if saved:
                saved_path = Path(saved)
                if saved_path.resolve() != dest.resolve():
                    dest.write_bytes(saved_path.read_bytes())
                self.sidecar_count += 1
                logger.info("【STRM同步】下载附属文件: %s", dest)
        except Exception as err:
            logger.warning("【STRM同步】附属文件下载失败 %s: %s", fileitem.path, err)

    def sync_paths(
        self,
        path_text: str,
        snapshot: Optional[Dict[str, Any]] = None,
        incremental: bool = False,
        remove_unless: bool = False,
        progress: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        snapshot = snapshot or {}
        new_snapshot: Dict[str, Any] = {}
        seen_strm: Set[str] = set()
        pairs = parse_path_pairs(path_text)
        if not pairs:
            logger.warning("【STRM同步】未配置有效路径，格式：本地目录#网盘目录")
            return self.stats()

        for local_dir, pan_dir in pairs:
            logger.info("【STRM同步】开始扫描 %s -> %s", pan_dir, local_dir)
            if progress:
                progress(f"扫描 {pan_dir}")
            for fileitem in self.iter_files(pan_dir):
                pan_path = fileitem.path or ""
                suffix = Path(fileitem.name or pan_path).suffix.lower()
                key = f"{self.storage}:{pan_path}"
                stamp = {
                    "size": getattr(fileitem, "size", None) or 0,
                    "mtime": getattr(fileitem, "modify_time", None) or 0,
                }
                new_snapshot[key] = stamp
                if suffix in self.mediaext:
                    changed = (
                        not incremental
                        or snapshot.get(key) != stamp
                        or not local_strm_path(local_dir, pan_dir, pan_path).exists()
                    )
                    if changed:
                        strm = self.write_strm(
                            local_dir, pan_dir, fileitem, overwrite=incremental
                        )
                        if strm:
                            seen_strm.add(str(Path(strm)))
                    else:
                        self.strm_skip_count += 1
                        seen_strm.add(str(local_strm_path(local_dir, pan_dir, pan_path)))
                elif suffix in self.sidecar_ext:
                    if not incremental or snapshot.get(key) != stamp:
                        self.download_sidecar(local_dir, pan_dir, fileitem)

            if remove_unless:
                self._remove_unless_strm(local_dir, seen_strm)

        return {"snapshot": new_snapshot, **self.stats()}

    def _remove_unless_strm(self, local_dir: str, keep: Set[str]) -> None:
        root = Path(local_dir)
        if not root.exists():
            return
        keep_resolved = {str(Path(item).resolve()) for item in keep}
        for strm in root.rglob("*.strm"):
            if str(strm.resolve()) in keep_resolved:
                continue
            try:
                strm.unlink(missing_ok=True)
                self.remove_count += 1
                logger.info("【STRM同步】清理失效 STRM: %s", strm)
            except Exception as err:
                logger.warning("【STRM同步】清理失败 %s: %s", strm, err)

    def stats(self) -> Dict[str, Any]:
        return {
            "strm_count": self.strm_count,
            "strm_skip_count": self.strm_skip_count,
            "strm_fail_count": self.strm_fail_count,
            "sidecar_count": self.sidecar_count,
            "remove_count": self.remove_count,
            "fail_dict": self.fail_dict,
        }
