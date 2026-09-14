from functools import wraps
from inspect import Parameter, signature
from pathlib import Path
from threading import Lock, local
from typing import Callable, Optional, Tuple, TYPE_CHECKING

from app.log import logger

if TYPE_CHECKING:
    from ..helper.transfer import TransferTaskManager, TransferHandler


class TransferChainPatcher:
    """
    TransferChain 补丁管理器（MoviePilot V3）

    V3 将识别/选目录/检查点放在 `_plan_checkpoint_and_execute` 之前完成。
    插件不再整段替换 V2 的 `__handle_transfer`，而是：
    1. 保留宿主 V3 识别与目录计算
    2. 在提交 durable checkpoint 并真正执行文件操作前接管 115→115 任务
    """

    _original_handle_transfer = None
    _original_plan_checkpoint_and_execute = None
    _enabled = False
    _task_manager: Optional["TransferTaskManager"] = None
    _handler: Optional["TransferHandler"] = None
    _storage_module: str = ""
    _lock = Lock()
    _tls = local()

    @classmethod
    def enable(
        cls,
        task_manager: "TransferTaskManager",
        handler: "TransferHandler",
        storage_module: str,
    ):
        """
        启用补丁

        :param task_manager: TransferTaskManager 实例
        :param handler: TransferHandler 实例
        :param storage_module: 存储模块名称
        """
        with cls._lock:
            if cls._enabled:
                logger.debug("【整理接管】补丁已启用，跳过")
                return

            try:
                from app.chain.transfer import TransferChain

                cls._task_manager = task_manager
                cls._handler = handler
                cls._storage_module = storage_module

                cls._original_handle_transfer = (
                    TransferChain._TransferChain__handle_transfer
                )
                cls._original_plan_checkpoint_and_execute = (
                    TransferChain._plan_checkpoint_and_execute
                )

                @wraps(cls._original_plan_checkpoint_and_execute)
                def patched_plan_checkpoint_and_execute(
                    self, task, *, source_oper=None, target_oper=None
                ):
                    return cls._patched_plan_checkpoint_and_execute(
                        self,
                        task,
                        source_oper=source_oper,
                        target_oper=target_oper,
                    )

                @wraps(cls._original_handle_transfer)
                def patched_handle_transfer(
                    self, task, callback: Optional[Callable] = None
                ) -> Optional[Tuple[bool, str]]:
                    return cls._patched_handle_transfer(self, task, callback)

                TransferChain._plan_checkpoint_and_execute = (
                    patched_plan_checkpoint_and_execute
                )
                TransferChain._TransferChain__handle_transfer = patched_handle_transfer
                cls._enabled = True
                logger.info("【整理接管】TransferChain V3 补丁已启用")

            except Exception as e:
                logger.error(f"【整理接管】启用补丁失败: {e}", exc_info=True)

    @classmethod
    def disable(cls):
        """
        禁用补丁
        """
        with cls._lock:
            if not cls._enabled:
                return

            try:
                from app.chain.transfer import TransferChain

                if cls._original_plan_checkpoint_and_execute:
                    TransferChain._plan_checkpoint_and_execute = (
                        cls._original_plan_checkpoint_and_execute
                    )
                    cls._original_plan_checkpoint_and_execute = None
                if cls._original_handle_transfer:
                    TransferChain._TransferChain__handle_transfer = (
                        cls._original_handle_transfer
                    )
                    cls._original_handle_transfer = None

                cls._task_manager = None
                cls._handler = None
                cls._storage_module = ""
                cls._enabled = False
                logger.info("【整理接管】TransferChain 补丁已禁用")

            except Exception as e:
                logger.error(f"【整理接管】禁用补丁失败: {e}", exc_info=True)

    @classmethod
    def _patched_handle_transfer(
        cls, chain_self, task, callback: Optional[Callable] = None
    ) -> Optional[Tuple[bool, str]]:
        """
        包装 V3 `__handle_transfer`：接管成功时跳过宿主整理完成回调。
        """
        cls._tls.intercepted = None
        cls._tls.callback = callback

        def gated_callback(task_obj, transferinfo) -> Tuple[bool, str]:
            intercepted = getattr(cls._tls, "intercepted", None)
            if intercepted is not None:
                return intercepted
            if callback:
                return callback(task_obj, transferinfo)
            return bool(getattr(transferinfo, "success", False)), (
                getattr(transferinfo, "message", None) or ""
            )

        result = cls._original_handle_transfer(chain_self, task, gated_callback)
        intercepted = getattr(cls._tls, "intercepted", None)
        if intercepted is not None:
            return intercepted
        return result

    @classmethod
    def _patched_plan_checkpoint_and_execute(
        cls, chain_self, task, *, source_oper=None, target_oper=None
    ):
        """
        在 V3 提交整理检查点前拦截 115→115 任务。
        """
        from app.schemas import TransferInfo

        intercepted = cls._try_takeover(chain_self, task)
        if intercepted is not None:
            cls._tls.intercepted = intercepted
            return TransferInfo(
                success=bool(intercepted[0]),
                fileitem=getattr(task, "fileitem", None),
                message=intercepted[1] or "",
            )
        return cls._original_plan_checkpoint_and_execute(
            chain_self, task, source_oper=source_oper, target_oper=target_oper
        )

    @classmethod
    def _try_takeover(cls, chain_self, task) -> Optional[Tuple[bool, str]]:
        """
        尝试接管当前整理任务。

        :return: None 表示回退宿主；否则返回 (成功, 消息)
        """
        from app.db.transferhistory_oper import TransferHistoryOper
        from app.schemas import TransferInfo
        from app.schemas.types import MediaType

        from ..core.config import configer
        from ..helper.transfer.linked_subtitle_audio import (
            is_subtitle_or_audio_file,
        )
        from ..schemas.transfer import TransferTask as PluginTransferTask

        if not cls._enabled or cls._task_manager is None:
            return None

        fileitem = getattr(task, "fileitem", None)
        if fileitem is None:
            return None

        # 目录/蓝光原盘仍走宿主
        if getattr(fileitem, "type", None) == "dir":
            logger.debug(
                f"【整理接管】检测到目录类型任务（可能是蓝光原盘），回退到原方法: {fileitem.path}"
            )
            return None

        source_storage = getattr(fileitem, "storage", None)
        target_storage = getattr(task, "target_storage", None)
        if not cls._should_intercept(source_storage, target_storage):
            return None

        logger.debug(f"【整理接管】检测到 115 → 115 整理任务: {fileitem.name}")

        # 音乐由 MoviePilot 原生整理，插件不接管、不当成影视伴随音轨
        if cls._is_music_task(task):
            logger.info(
                f"【整理接管】识别为音乐，回退 MoviePilot 原生整理: {fileitem.name}"
            )
            return None

        if configer.pan_transfer_linked_subtitle_audio and is_subtitle_or_audio_file(
            fileitem
        ):
            logger.debug(
                f"【整理接管】忽略字幕/音频文件（将跟随主文件一起处理）: {fileitem.name}"
            )
            try:
                chain_self.jobview.finish_task(task)
            except Exception as e:
                logger.debug(f"【整理接管】标记字幕/音频任务完成失败: {e}")
            return True, "已由插件接管（字幕/音频文件，跟随主文件处理）"

        need_rename, need_notify, need_scrape = cls._derive_transfer_flags(task)

        if getattr(task, "preview", False):
            return cls._handle_preview(
                chain_self,
                task,
                getattr(cls._tls, "callback", None),
                need_rename,
                need_notify,
                need_scrape,
            )

        mediainfo = getattr(task, "mediainfo", None)
        meta = getattr(task, "meta", None)
        if (
            mediainfo is not None
            and getattr(mediainfo, "type", None) == MediaType.TV
            and getattr(fileitem, "type", None) == "file"
            and meta is not None
            and meta.begin_episode is None
        ):
            logger.warn(
                f"【整理接管】文件 {fileitem.path} 整理失败：未识别到文件集数"
            )
            fail_msg = "未识别到文件集数"
            src_path = fileitem.path
            transferhis = TransferHistoryOper()
            transferhis.add_fail(
                fileitem=fileitem,
                mode=task.transfer_type or "",
                meta=meta,
                mediainfo=mediainfo,
                transferinfo=TransferInfo(
                    success=False,
                    fileitem=fileitem,
                    message=fail_msg,
                    transfer_type=task.transfer_type,
                    file_list=[src_path],
                    fail_list=[src_path],
                    need_notify=need_notify,
                    need_scrape=need_scrape,
                ),
                downloader=task.downloader,
                download_hash=task.download_hash,
            )
            try:
                chain_self.jobview.remove_task(fileitem)
            except Exception:
                pass
            return False, "未识别到文件集数"

        if meta is not None:
            # 文件结束季为空
            meta.end_season = None
            # 文件总季数为1
            if meta.total_season:
                meta.total_season = 1
            # 文件不可能超过2集
            if meta.total_episode and meta.total_episode > 2:
                meta.total_episode = 1
                meta.end_episode = None

        target_path = cls._compute_target_path(task, need_rename=need_rename)
        if not target_path:
            logger.error(f"【整理接管】计算目标路径失败: {fileitem.path}")
            return None

        transfer_type = task.transfer_type
        if not transfer_type and task.target_directory:
            transfer_type = task.target_directory.transfer_type

        overwrite_mode = None
        if task.target_directory:
            overwrite_mode = task.target_directory.overwrite_mode

        try:
            chain_self.jobview.running_task(task)
        except Exception as e:
            logger.debug(f"【整理接管】标记 running_task 失败: {e}")

        plugin_task = PluginTransferTask(
            fileitem=fileitem,
            target_path=target_path,
            mediainfo=mediainfo,
            meta=meta,
            transfer_type=transfer_type or "move",
            overwrite_mode=overwrite_mode,
            need_rename=need_rename,
            need_notify=need_notify,
            need_scrape=need_scrape,
            scrape=task.scrape,
            manual=task.manual,
            background=task.background,
            username=task.username,
            downloader=task.downloader,
            download_hash=task.download_hash,
        )
        cls._task_manager.add_task(plugin_task)
        logger.info(
            f"【整理接管】任务已加入批量队列: {fileitem.name} -> {target_path}"
        )
        return True, "已由插件接管"

    @classmethod
    def _derive_transfer_flags(cls, task) -> Tuple[bool, bool, bool]:
        """
        与 app.modules.filemanager 中 transfer() 一致，推导 need_rename / need_notify / need_scrape

        :param task: MoviePilot TransferTask
        :return: (need_rename, need_notify, need_scrape)
        """
        if task.target_directory:
            need_rename = bool(task.target_directory.renaming)
            need_notify = bool(task.target_directory.notify)
            if task.scrape is None:
                need_scrape = bool(task.target_directory.scraping)
            else:
                need_scrape = bool(task.scrape)
            return need_rename, need_notify, need_scrape
        if task.target_path:
            need_rename = True
            need_notify = False
            need_scrape = bool(task.scrape) if task.scrape is not None else False
            return need_rename, need_notify, need_scrape
        need_rename = True
        need_notify = True
        need_scrape = bool(task.scrape) if task.scrape is not None else False
        return need_rename, need_notify, need_scrape

    @staticmethod
    def _is_music_task(task) -> bool:
        """识别为音乐时不接管，交给 MoviePilot 原生音乐整理。"""
        from app.schemas.types import MediaType

        mediainfo = getattr(task, "mediainfo", None)
        media_type = getattr(mediainfo, "type", None)
        if media_type == MediaType.MUSIC:
            return True
        if str(getattr(media_type, "value", media_type) or "") == "音乐":
            return True
        meta = getattr(task, "meta", None)
        if meta is None:
            return False
        if getattr(meta, "type", None) == MediaType.MUSIC:
            return True
        return type(meta).__name__ == "MetaMusic"

    @classmethod
    def _should_intercept(cls, source_storage: str, target_storage: str) -> bool:
        """
        判断是否应该拦截

        :param source_storage: 源存储
        :param target_storage: 目标存储
        :return: 是否应该拦截
        """
        return (
            cls._enabled
            and cls._storage_module
            and source_storage == cls._storage_module
            and target_storage == cls._storage_module
        )

    @staticmethod
    def _is_movie_year_conflict(file_meta, media) -> bool:
        """
        判断文件名年份是否与已识别电影年份冲突
        """
        from app.schemas.types import MediaType

        file_year = getattr(file_meta, "year", None)
        media_year = getattr(media, "year", None)
        if not file_meta or not media or not file_year or not media_year:
            return False
        media_type = getattr(media, "type", None)
        if not isinstance(media_type, MediaType):
            try:
                media_type = MediaType(media_type)
            except (TypeError, ValueError):
                return False
        return media_type == MediaType.MOVIE and str(file_year) != str(media_year)

    @classmethod
    def _compute_target_path(cls, task, need_rename: bool = True) -> Optional[Path]:
        """
        与 TransHandler.transfer_media 单文件分支一致的目标路径

        :param task: MoviePilot 的 TransferTask
        :param need_rename: 是否与 MP 目录 renaming 一致
        :return: 目标路径，失败返回 None
        """
        from app.core.config import settings
        from app.modules.filemanager.transhandler import TransHandler
        from app.schemas.types import MediaType

        try:
            handler = TransHandler()

            target_dir = handler.get_dest_dir(
                mediainfo=task.mediainfo,
                target_dir=task.target_directory,
                need_type_folder=task.library_type_folder,
                need_category_folder=task.library_category_folder,
            )

            if not target_dir:
                logger.error("【整理接管】计算目标目录失败")
                return None

            if not need_rename:
                return target_dir / task.fileitem.name

            if task.mediainfo.type == MediaType.TV:
                rename_format = settings.TV_RENAME_FORMAT
            else:
                rename_format = settings.MOVIE_RENAME_FORMAT

            file_ext = Path(task.fileitem.name).suffix

            naming_dict = handler.get_naming_dict(
                meta=task.meta,
                mediainfo=task.mediainfo,
                file_ext=file_ext,
                episodes_info=task.episodes_info,
            )

            # 触发 TransferRenameBuild 事件，允许插件注入命名字段
            try:
                from app.core.event import eventmanager
                from app.schemas import TransferRenameBuildEventData
                from app.schemas.types import ChainEventType

                build_event_data = TransferRenameBuildEventData(
                    rename_dict=naming_dict,
                    meta=task.meta,
                    mediainfo=task.mediainfo,
                    file_ext=file_ext,
                    episodes_info=task.episodes_info,
                )
                build_event = eventmanager.send_event(
                    ChainEventType.TransferRenameBuild, build_event_data
                )
                if build_event and build_event.event_data:
                    naming_dict = build_event.event_data.rename_dict
            except Exception:
                pass

            rename_kwargs = {
                "template_string": rename_format,
                "rename_dict": naming_dict,
                "path": target_dir,
            }
            try:
                sig = signature(handler.get_rename_path)
                has_varkw = any(
                    p.kind == Parameter.VAR_KEYWORD for p in sig.parameters.values()
                )
                if "source_path" in sig.parameters or has_varkw:
                    rename_kwargs["source_path"] = task.fileitem.path
                if "source_item" in sig.parameters or has_varkw:
                    rename_kwargs["source_item"] = task.fileitem
            except (TypeError, ValueError):
                pass
            rename_path = handler.get_rename_path(**rename_kwargs)

            if not rename_path:
                return None

            new_file = (
                Path(rename_path) if not isinstance(rename_path, Path) else rename_path
            )

            from ..helper.transfer.handler import TransferHandler

            ext = TransferHandler._normalize_ext(task.fileitem.extension)
            if not ext and task.fileitem.path:
                ext = TransferHandler._normalize_ext(Path(task.fileitem.path).suffix)
            if not ext:
                return new_file
            if ext in {
                TransferHandler._normalize_ext(ext_item)
                for ext_item in settings.RMT_SUBEXT
            }:
                new_file = TransHandler._TransHandler__rename_subtitles(
                    task.fileitem, new_file
                )

            return new_file

        except Exception as e:
            logger.error(f"【整理接管】计算目标路径失败: {e}", exc_info=True)
            return None

    @classmethod
    def _handle_preview(
        cls,
        chain_self,
        task,
        callback: Optional[Callable],
        need_rename: bool,
        need_notify: bool,
        need_scrape: bool,
    ) -> Optional[Tuple[bool, str]]:
        """
        Preview 模式：只计算目标路径，不执行实际文件操作

        :param chain_self: TransferChain 实例
        :param task: MoviePilot TransferTask
        :param callback: 回调函数（preview 模式下为 _preview_callback）
        :param need_rename: 是否需要重命名
        :param need_notify: 是否需要通知
        :param need_scrape: 是否需要刮削
        :return: 回调结果或 (True, target_path)
        """
        try:
            target_path = cls._compute_target_path(task, need_rename=need_rename)
            if not target_path:
                logger.error(
                    f"【整理接管】Preview 模式计算目标路径失败: {task.fileitem.path}"
                )
                return False, "计算目标路径失败"

            transfer_type = task.transfer_type
            if not transfer_type and task.target_directory:
                transfer_type = task.target_directory.transfer_type

            target_storage = task.target_storage or ""
            from app.schemas import FileItem, TransferInfo

            transferinfo = TransferInfo(
                success=True,
                fileitem=task.fileitem,
                target_item=FileItem(
                    storage=target_storage,
                    path=str(target_path),
                    name=target_path.name,
                    type="file",
                ),
                target_diritem=FileItem(
                    storage=target_storage,
                    path=str(target_path.parent) + "/",
                    name=target_path.parent.name,
                    type="dir",
                ),
                transfer_type=transfer_type or "move",
                file_list=[task.fileitem.path],
                file_list_new=[str(target_path)],
                need_scrape=need_scrape,
                need_notify=need_notify,
            )

            if callback:
                return callback(task, transferinfo)
            return True, str(target_path)
        except Exception as e:
            logger.error(f"【整理接管】Preview 模式异常: {e}", exc_info=True)
            return False, f"Preview 异常: {e}"

    @classmethod
    def _call_original(
        cls, chain_self, task, callback: Optional[Callable]
    ) -> Optional[Tuple[bool, str]]:
        """
        调用原方法

        :param chain_self: TransferChain 实例
        :param task: 任务
        :param callback: 回调
        :return: 原方法的返回值
        """
        if cls._original_handle_transfer:
            return cls._original_handle_transfer(chain_self, task, callback)
        return None

    @classmethod
    def _call_original_transfer_part(
        cls, chain_self, task, callback: Optional[Callable]
    ) -> Optional[Tuple[bool, str]]:
        """
        调用原方法的 transfer 部分

        :param chain_self: TransferChain 实例
        :param task: 任务
        :param callback: 回调
        :return: 返回值
        """
        from app.core.event import eventmanager
        from app.schemas import StorageOperSelectionEventData, TransferInfo
        from app.schemas.types import ChainEventType

        try:
            # 正在处理
            chain_self.jobview.running_task(task)

            # 获取源存储操作对象
            source_oper = None
            source_event_data = StorageOperSelectionEventData(
                storage=task.fileitem.storage
            )
            source_event = eventmanager.send_event(
                ChainEventType.StorageOperSelection, source_event_data
            )
            if source_event and source_event.event_data:
                source_event_data = source_event.event_data
                if source_event_data.storage_oper:
                    source_oper = source_event_data.storage_oper

            # 获取目标存储操作对象
            target_oper = None
            target_event_data = StorageOperSelectionEventData(
                storage=task.target_storage
            )
            target_event = eventmanager.send_event(
                ChainEventType.StorageOperSelection, target_event_data
            )
            if target_event and target_event.event_data:
                target_event_data = target_event.event_data
                if target_event_data.storage_oper:
                    target_oper = target_event_data.storage_oper

            # 执行整理
            transferinfo: TransferInfo = chain_self.transfer(
                fileitem=task.fileitem,
                meta=task.meta,
                mediainfo=task.mediainfo,
                target_directory=task.target_directory,
                target_storage=task.target_storage,
                target_path=task.target_path,
                transfer_type=task.transfer_type,
                episodes_info=task.episodes_info,
                scrape=task.scrape,
                library_type_folder=task.library_type_folder,
                library_category_folder=task.library_category_folder,
                source_oper=source_oper,
                target_oper=target_oper,
                preview=task.preview,
            )

            if not transferinfo:
                logger.error("文件整理模块运行失败")
                return False, "文件整理模块运行失败"

            if callback:
                return callback(task, transferinfo)

            return transferinfo.success, transferinfo.message

        except Exception as e:
            logger.error(f"【整理接管】执行 transfer 失败: {e}", exc_info=True)
            return False, f"整理失败: {e}"
