from pathlib import Path
from typing import Iterable, List, Optional, Set

from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.log import logger
from app.schemas import FileItem

from .path import split_exts


class PanTransferHelper:
    """
    扫描网盘整理目录，交给 MoviePilot TransferChain 入库
    """

    def __init__(self, storage: str, mediaext: str, min_filesize: int = 0):
        self.storage = storage
        self.mediaext = split_exts(mediaext) or {
            f".{ext.strip().lower().lstrip('.')}" for ext in settings.RMT_MEDIAEXT
        }
        self.min_filesize = int(min_filesize or 0)
        self.storage_chain = StorageChain()
        self.transfer_chain = TransferChain()
        self.queued: List[str] = []
        self.skipped: List[str] = []
        self.failed: List[str] = []

    def iter_children(self, fileitem: FileItem) -> Iterable[FileItem]:
        try:
            items = self.storage_chain.list_files(fileitem) or []
        except Exception as err:
            logger.error("【网盘整理】列出失败 %s: %s", fileitem.path, err)
            return
        for item in items:
            yield item

    def _is_media(self, fileitem: FileItem) -> bool:
        if getattr(fileitem, "type", None) == "dir":
            try:
                return bool(self.storage_chain.is_bluray_folder(fileitem))
            except Exception:
                return True
        suffix = Path(fileitem.name or fileitem.path or "").suffix.lower()
        if not suffix and fileitem.extension:
            suffix = f".{str(fileitem.extension).lstrip('.').lower()}"
        return suffix in self.mediaext

    def _too_small(self, fileitem: FileItem) -> bool:
        if getattr(fileitem, "type", None) == "dir" or self.min_filesize <= 0:
            return False
        size = getattr(fileitem, "size", None) or 0
        return size < self.min_filesize * 1024 * 1024

    def scan_and_transfer(
        self, pan_paths: str, busy: Optional[Set[str]] = None
    ) -> dict:
        busy = busy or set()
        for line in (pan_paths or "").splitlines():
            pan_dir = line.strip()
            if not pan_dir or pan_dir.startswith("#"):
                continue
            root = self.storage_chain.get_file_item(
                storage=self.storage, path=Path(pan_dir)
            )
            if not root:
                logger.error("【网盘整理】目录不存在: %s %s", self.storage, pan_dir)
                self.failed.append(pan_dir)
                continue
            logger.info("【网盘整理】扫描目录: %s", pan_dir)
            for item in self.iter_children(root):
                path = item.path or ""
                if path in busy:
                    self.skipped.append(path)
                    continue
                if not self._is_media(item):
                    self.skipped.append(path)
                    continue
                if self._too_small(item):
                    logger.debug("【网盘整理】小于最小体积，跳过: %s", path)
                    self.skipped.append(path)
                    continue
                try:
                    success, message = self.transfer_chain.do_transfer(
                        fileitem=item,
                        background=True,
                    )
                    if success:
                        self.queued.append(path)
                        logger.info("【网盘整理】已加入整理队列: %s", path)
                    else:
                        self.failed.append(path)
                        logger.error("【网盘整理】提交失败 %s: %s", path, message)
                except Exception as err:
                    self.failed.append(path)
                    logger.error("【网盘整理】提交异常 %s: %s", path, err)
        return {
            "queued": self.queued,
            "skipped": self.skipped,
            "failed": self.failed,
        }
