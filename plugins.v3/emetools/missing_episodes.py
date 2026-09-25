"""Independent Emby/TMDB missing-episode detector for ME tools."""

import concurrent.futures
import threading
from collections import defaultdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

import pytz
from pypinyin import lazy_pinyin

from app.chain.subscribe import SubscribeChain
from app.schemas.types import MediaSource, MediaType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.services import MediaServerHelper


class MissingAction(str, Enum):
    ONLY_HISTORY = "仅检查记录"
    ADD_SUBSCRIBE = "添加到订阅"
    MARK_EXIST = "标记为存在"


DEFAULT_MISSING = {
    "enabled": False, "cron": "35 3 * * *", "only_existing_seasons": True,
    "missing_action": MissingAction.ONLY_HISTORY.value,
    "ignore_season_zero": True, "ignore_future": True,
    "server_names": [], "library_names": [], "skip_series_ids": [],
}


class MissingEpisodeDetector:
    """Runs locally under the ME plugin identity and owns its scan data."""

    plugin_name = "ME工具 缺集检测"
    _DATA_KEY = "missing_episodes"
    _TIME_KEY = "last_scan_time"

    def __init__(self, owner, config):
        self.owner = owner
        self._mediaserver_helper = MediaServerHelper()
        self._subscribe_chain = SubscribeChain()
        self._scan_lock = threading.Lock()
        self._is_scanning = False
        self._results = []
        self._last_scan_time = "从未扫描"
        self.configure(config)
        self._load_saved_data()

    def configure(self, config):
        self._enabled = bool(config.get("enabled", False))
        self._cron = str(config.get("cron") or DEFAULT_MISSING["cron"])
        self._only_existing_seasons = bool(config.get("only_existing_seasons", True))
        self._missing_action = str(config.get("missing_action") or MissingAction.ONLY_HISTORY.value)
        self._ignore_season_zero = bool(config.get("ignore_season_zero", True))
        self._ignore_future = bool(config.get("ignore_future", True))
        self._server_names = self._parse_names(config.get("server_names"))
        self._library_names = self._parse_names(config.get("library_names"))
        self._skip_series_ids = set(self._parse_names(config.get("skip_series_ids")))

    @staticmethod
    def _parse_names(value):
        if isinstance(value, list):
            values = value
        else:
            values = str(value or "").replace("\n", ",").split(",")
        return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))

    def _load_saved_data(self):
        results = self.owner.get_data(self._DATA_KEY)
        timestamp = self.owner.get_data(self._TIME_KEY)
        self._results = results if isinstance(results, list) else []
        self._last_scan_time = str(timestamp) if timestamp else "从未扫描"

    def save_data(self, key, value):
        self.owner.save_data(key, value)
    def _get_form_options(
        self,
    ) -> Tuple[
        List[Dict[str, str]],
        List[Dict[str, str]],
        List[Dict[str, str]],
    ]:
        """读取 Emby 服务器、电视剧媒体库和剧集，生成多选项。"""
        server_names = set(self._server_names)
        library_names = set(self._library_names)
        series_options: Dict[str, str] = {
            tmdb_id: f"TMDB {tmdb_id}（已保存）"
            for tmdb_id in self._skip_series_ids
        }
        if self._mediaserver_helper:
            try:
                services = self._mediaserver_helper.get_services(type_filter="emby") or {}
                service_values = (
                    services.values() if isinstance(services, dict) else services
                )
                for service in service_values:
                    server_name = str(service.config.name or "").strip()
                    if server_name:
                        server_names.add(server_name)
                    if self._server_names and server_name not in self._server_names:
                        continue
                    try:
                        libraries = service.instance.get_librarys(hidden=False) or []
                    except Exception as error:  # noqa: BLE001 - 单个服务器失败不影响表单
                        logger.warning(
                            f"【{self.plugin_name}】读取 {server_name} 媒体库失败：{error}"
                        )
                        continue
                    for library in libraries:
                        if getattr(library, "type", None) != MediaType.TV.value:
                            continue
                        library_name = str(getattr(library, "name", "") or "").strip()
                        if library_name:
                            library_names.add(library_name)
                        if self._library_names and library_name not in self._library_names:
                            continue
                        try:
                            series = service.instance.get_items(library.id) or []
                            for item in series:
                                if getattr(item, "item_type", None) not in {
                                    "Series",
                                    "show",
                                }:
                                    continue
                                media_source = getattr(item, "media_source", None)
                                if media_source != MediaSource.TMDB:
                                    continue
                                tmdb_id = str(getattr(item, "media_id", "") or "").strip()
                                if not tmdb_id:
                                    continue
                                title = str(
                                    getattr(item, "title", None)
                                    or getattr(item, "original_title", None)
                                    or f"TMDB {tmdb_id}"
                                )
                                year = str(getattr(item, "year", "") or "").strip()
                                display_title = f"{title} ({year})" if year else title
                                series_options[tmdb_id] = (
                                    f"{display_title} · {server_name} / {library_name}"
                                )
                        except Exception as error:  # noqa: BLE001 - 单库失败不影响其他选项
                            logger.warning(
                                f"【{self.plugin_name}】读取 {server_name} / "
                                f"{library_name} 剧集失败：{error}"
                            )
            except Exception as error:  # noqa: BLE001 - 表单仍需展示已保存选项
                logger.warning(f"【{self.plugin_name}】读取 Emby 选项失败：{error}")

        return (
            [{"title": name, "value": name} for name in sorted(server_names)],
            [{"title": name, "value": name} for name in sorted(library_names)],
            [
                {"title": title, "value": tmdb_id}
                for tmdb_id, title in sorted(
                    series_options.items(),
                    key=lambda item: (
                        "".join(lazy_pinyin(item[1].split(" · ", 1)[0])).casefold(),
                        item[1].casefold(),
                        item[0],
                    ),
                )
            ],
        )

    def _cancel_skipped_subscriptions(self) -> None:
        """取消跳过剧集对应的全部 MoviePilot 订阅。"""
        if not self._subscribe_chain:
            return
        for tmdb_id in sorted(self._skip_series_ids):
            try:
                subscribes = self._subscribe_chain.subscription_repository.list()
                deleted = 0
                for subscribe in subscribes or []:
                    if getattr(subscribe, "type", None) != MediaType.TV.value:
                        continue
                    if getattr(subscribe, "media_source", None) != MediaSource.TMDB:
                        continue
                    if str(getattr(subscribe, "media_id", "") or "") != tmdb_id:
                        continue
                    subscribe_id = getattr(subscribe, "id", None)
                    if subscribe_id and self._subscribe_chain._delete_subscription(
                        int(subscribe_id)
                    ):
                        deleted += 1
                if deleted:
                    logger.info(
                        f"【{self.plugin_name}】TMDB {tmdb_id} 已取消 {deleted} 个季度订阅"
                    )
            except Exception as error:  # noqa: BLE001 - 单剧失败不影响其他跳过项
                logger.error(
                    f"【{self.plugin_name}】取消 TMDB {tmdb_id} 订阅失败：{error}"
                )

    @staticmethod
    def _format_episode_ranges(episodes: Set[int]) -> str:
        """把集号集合压缩成连续区间。"""
        if not episodes:
            return ""
        values = sorted(episodes)
        ranges: List[str] = []
        start = previous = values[0]
        for episode in values[1:]:
            if episode == previous + 1:
                previous = episode
                continue
            ranges.append(str(start) if start == previous else f"{start}-{previous}")
            start = previous = episode
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        return "、".join(ranges)

    @staticmethod
    def _request_json(url: str) -> Optional[Dict[str, Any]]:
        """请求 JSON，失败时返回空值并记录调试日志。"""
        try:
            response = RequestUtils().get_res(url)
            if response and response.status_code == 200:
                payload = response.json()
                return payload if isinstance(payload, dict) else None
        except Exception as error:  # noqa: BLE001 - 外部服务错误不能中断整次扫描
            logger.debug(f"请求失败 {url.split('?')[0]}：{error}")
        return None

    def _process_series(
        self,
        series: Dict[str, Any],
        inventory: Dict[str, Dict[int, Set[int]]],
        tmdb_key: str,
        tmdb_domain: str,
        today: str,
        server_name: str,
        library_name: str,
    ) -> List[Dict[str, Any]]:
        """将一部 Emby 剧集与 TMDB 季集信息进行比对。"""
        series_id = str(series.get("Id") or "")
        provider_ids = series.get("ProviderIds") or {}
        tmdb_id = provider_ids.get("Tmdb") or provider_ids.get("tmdb") or provider_ids.get("TMDB")
        if not series_id or not tmdb_id:
            return []
        if str(tmdb_id) in self._skip_series_ids:
            logger.debug(
                f"【{self.plugin_name}】{series.get('Name') or tmdb_id} 已配置跳过检测"
            )
            return []

        details = self._request_json(
            f"https://{tmdb_domain}/3/tv/{tmdb_id}?language=zh-CN&api_key={tmdb_key}"
        )
        if not details:
            return []

        local_inventory = inventory.get(series_id, {})
        results: List[Dict[str, Any]] = []
        for season in details.get("seasons") or []:
            season_number = season.get("season_number")
            if season_number is None or not season.get("episode_count"):
                continue
            try:
                season_number = int(season_number)
            except (TypeError, ValueError):
                continue
            if self._ignore_season_zero and season_number == 0:
                continue
            if self._only_existing_seasons and season_number not in local_inventory:
                continue

            local_episodes = local_inventory.get(season_number, set())
            season_details = self._request_json(
                f"https://{tmdb_domain}/3/tv/{tmdb_id}/season/{season_number}?language=zh-CN&api_key={tmdb_key}"
            )
            if not season_details:
                continue

            missing: Set[int] = set()
            aired_total = 0
            for episode in season_details.get("episodes") or []:
                episode_number = episode.get("episode_number")
                air_date = episode.get("air_date")
                if self._ignore_future and (not air_date or air_date > today):
                    continue
                try:
                    episode_number = int(episode_number)
                except (TypeError, ValueError):
                    continue
                aired_total += 1
                if episode_number not in local_episodes:
                    missing.add(episode_number)
            if not missing:
                continue
            results.append(
                {
                    "ServerName": server_name,
                    "LibraryName": library_name,
                    "SeriesName": series.get("Name") or details.get("name") or "未知剧集",
                    "Year": str(series.get("ProductionYear") or (details.get("first_air_date") or "")[:4]),
                    "TmdbId": str(tmdb_id),
                    "SeasonNum": season_number,
                    "SeasonFormatted": "SP" if season_number == 0 else f"S{season_number}",
                    "MissingEpisodeNumbers": sorted(missing),
                    "MissingEpisodes": self._format_episode_ranges(missing),
                    "TotalEpisodes": int(season.get("episode_count") or aired_total),
                    "ActionResult": "待处理",
                }
            )
        return results

    def _selected_libraries(
        self, host: str, api_key: str, user_id: str
    ) -> List[Dict[str, Any]]:
        """获取符合白名单的 Emby 电视剧媒体库。"""
        payload = self._request_json(
            f"{host}/emby/Users/{user_id}/Views?api_key={api_key}"
        )
        libraries = payload.get("Items", []) if payload else []
        selected = []
        for library in libraries:
            name = str(library.get("Name") or "")
            collection_type = str(library.get("CollectionType") or "").lower()
            if collection_type not in {"tvshows", "mixed"}:
                continue
            if self._library_names and name not in self._library_names:
                continue
            selected.append(library)
        return selected

    def _scan_library(
        self,
        host: str,
        api_key: str,
        user_id: str,
        server_name: str,
        library: Dict[str, Any],
        tmdb_key: str,
        tmdb_domain: str,
        today: str,
    ) -> List[Dict[str, Any]]:
        """扫描一个 Emby 媒体库并返回缺失季集。"""
        library_id = str(library.get("Id") or "")
        library_name = str(library.get("Name") or library_id)
        if not library_id:
            return []
        common = f"ParentId={library_id}&Recursive=true&api_key={api_key}"
        series_payload = self._request_json(
            f"{host}/emby/Users/{user_id}/Items?{common}&IncludeItemTypes=Series&Fields=ProviderIds,ProductionYear"
        )
        episode_payload = self._request_json(
            f"{host}/emby/Users/{user_id}/Items?{common}&IncludeItemTypes=Episode&Fields=IndexNumberEnd,LocationType"
        )
        series_items = series_payload.get("Items", []) if series_payload else []
        episode_items = episode_payload.get("Items", []) if episode_payload else []

        inventory: Dict[str, Dict[int, Set[int]]] = defaultdict(lambda: defaultdict(set))
        for episode in episode_items:
            if episode.get("LocationType") == "Virtual":
                continue
            try:
                season_number = int(episode.get("ParentIndexNumber"))
                first = int(episode.get("IndexNumber"))
                last = int(episode.get("IndexNumberEnd") or first)
            except (TypeError, ValueError):
                continue
            series_id = str(episode.get("SeriesId") or "")
            if series_id:
                inventory[series_id][season_number].update(range(first, last + 1))

        results: List[Dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(
                    self._process_series,
                    series,
                    inventory,
                    tmdb_key,
                    tmdb_domain,
                    today,
                    server_name,
                    library_name,
                )
                for series in series_items
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    results.extend(future.result())
                except Exception as error:  # noqa: BLE001 - 单剧失败不影响其他剧集
                    logger.error(f"【{self.plugin_name}】单部剧集检测失败：{error}")
        return results

    def _handle_missing(self, result: Dict[str, Any]) -> None:
        """按配置处理一条缺失季记录。"""
        if self._missing_action == MissingAction.ONLY_HISTORY.value:
            result["ActionResult"] = MissingAction.ONLY_HISTORY.value
            return
        if self._missing_action == MissingAction.MARK_EXIST.value:
            result["ActionResult"] = MissingAction.MARK_EXIST.value
            return
        if not self._subscribe_chain:
            result["ActionResult"] = "订阅失败：订阅服务未初始化"
            return
        try:
            subscribe_id, message = self._subscribe_chain.add(
                title=str(result.get("SeriesName") or ""),
                year=str(result.get("Year") or ""),
                mtype=MediaType.TV,
                media_source=MediaSource.TMDB,
                media_id=str(result.get("TmdbId") or ""),
                season=int(result.get("SeasonNum")),
                exist_ok=True,
                username=self.plugin_name,
                total_episode=int(result.get("TotalEpisodes") or 0),
            )
            result["ActionResult"] = "已添加订阅" if subscribe_id else f"订阅失败：{message}"
        except Exception as error:  # noqa: BLE001 - 记录订阅失败并继续处理其他季
            result["ActionResult"] = f"订阅失败：{error}"
            logger.error(
                f"【{self.plugin_name}】订阅 {result.get('SeriesName')} "
                f"{result.get('SeasonFormatted')} 失败：{error}"
            )

    def scan_missing_episodes(self) -> None:
        """扫描配置范围内的 Emby 媒体库并处理缺失季集。"""
        if not self._scan_lock.acquire(blocking=False):
            logger.warning(f"【{self.plugin_name}】上次扫描尚未结束，跳过本次执行")
            return
        self._is_scanning = True
        started_at = datetime.now()
        try:
            tmdb_key = str(getattr(settings, "TMDB_API_KEY", "") or "")
            if not tmdb_key:
                logger.error(f"【{self.plugin_name}】系统未配置 TMDB API Key")
                return
            if not self._mediaserver_helper:
                logger.error(f"【{self.plugin_name}】媒体服务器服务未初始化")
                return

            name_filters = self._server_names or None
            services = self._mediaserver_helper.get_services(
                name_filters=name_filters, type_filter="emby"
            )
            if not services:
                logger.error(f"【{self.plugin_name}】未找到符合条件的 Emby 服务器")
                return

            tmdb_domain = str(
                getattr(settings, "TMDB_API_DOMAIN", "api.themoviedb.org")
                or "api.themoviedb.org"
            )
            tmdb_domain = tmdb_domain.replace("https://", "").replace("http://", "").strip("/")
            today = datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d")
            results: List[Dict[str, Any]] = []

            service_values = services.values() if isinstance(services, dict) else services
            for service in service_values:
                server_name = str(service.config.name)
                raw_config = service.config.config or {}
                host = str(raw_config.get("host") or "").rstrip("/")
                api_key = str(raw_config.get("apikey") or "")
                user_id = str(service.instance.get_user() or "")
                if not host or not api_key or not user_id:
                    logger.error(f"【{self.plugin_name}】{server_name} 的连接信息不完整")
                    continue
                libraries = self._selected_libraries(host, api_key, user_id)
                logger.info(
                    f"【{self.plugin_name}】{server_name} 将扫描 {len(libraries)} 个媒体库"
                )
                for library in libraries:
                    results.extend(
                        self._scan_library(
                            host,
                            api_key,
                            user_id,
                            server_name,
                            library,
                            tmdb_key,
                            tmdb_domain,
                            today,
                        )
                    )

            results.sort(
                key=lambda item: (
                    str(item.get("ServerName")),
                    str(item.get("LibraryName")),
                    str(item.get("SeriesName")),
                    int(item.get("SeasonNum") or 0),
                )
            )
            for result in results:
                self._handle_missing(result)

            self._results = results
            self._last_scan_time = datetime.now(tz=pytz.timezone(settings.TZ)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            self.save_data(self._DATA_KEY, results)
            self.save_data(self._TIME_KEY, self._last_scan_time)
            elapsed = (datetime.now() - started_at).total_seconds()
            logger.info(
                f"【{self.plugin_name}】扫描完成，发现 {len(results)} 条缺失季记录，"
                f"耗时 {elapsed:.1f} 秒"
            )
        except Exception as error:  # noqa: BLE001 - 扫描任务必须自行收口
            logger.error(f"【{self.plugin_name}】扫描失败：{error}", exc_info=True)
        finally:
            self._is_scanning = False
            self._scan_lock.release()
