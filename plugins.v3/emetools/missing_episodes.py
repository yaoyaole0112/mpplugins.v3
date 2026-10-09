"""Independent Emby/TMDB missing-episode detector for ME tools."""

import concurrent.futures
import re
import threading
from collections import defaultdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

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
    "auto_cancel_enabled": False,
    "auto_cancel_mode": "ended_or_aired",
    "server_names": [], "library_names": [], "skip_series_ids": [],
    "episode_overrides": [],
    "auto_episode_correction": False,
}


def normalize_cancel_config(config):
    updated = dict(config)
    ended = bool(updated.pop("auto_cancel_completed", False))
    aired = bool(updated.pop("auto_cancel_aired_season", False))
    if "auto_cancel_enabled" not in updated:
        updated["auto_cancel_enabled"] = ended or aired
    if "auto_cancel_mode" not in updated:
        updated["auto_cancel_mode"] = (
            "ended" if ended and not aired else "aired" if aired and not ended else "ended_or_aired"
        )
    return updated


class MissingEpisodeDetector:
    """Runs locally under the ME plugin identity and owns its scan data."""

    plugin_name = "ME工具 缺集检测"
    _DATA_KEY = "missing_episodes"
    _TIME_KEY = "last_scan_time"
    _CANCELLED_KEY = "missing_cancelled_subscriptions"

    def __init__(self, owner, config):
        self.owner = owner
        self._mediaserver_helper = MediaServerHelper()
        self._subscribe_chain = SubscribeChain()
        self._scan_lock = threading.Lock()
        self._is_scanning = False
        self._results = []
        self._cancelled_subscriptions = []
        self._last_scan_time = "从未扫描"
        self.configure(config)
        self._load_saved_data()

    def configure(self, config):
        config = normalize_cancel_config(config)
        self._enabled = bool(config.get("enabled", False))
        self._cron = str(config.get("cron") or DEFAULT_MISSING["cron"])
        self._only_existing_seasons = bool(config.get("only_existing_seasons", True))
        self._missing_action = str(config.get("missing_action") or MissingAction.ONLY_HISTORY.value)
        self._ignore_season_zero = bool(config.get("ignore_season_zero", True))
        self._ignore_future = bool(config.get("ignore_future", True))
        cancel_enabled = bool(config["auto_cancel_enabled"])
        cancel_mode = config["auto_cancel_mode"]
        self._auto_cancel_completed = cancel_enabled and cancel_mode in {"ended", "ended_or_aired"}
        self._auto_cancel_aired_season = cancel_enabled and cancel_mode in {"aired", "ended_or_aired"}
        self._server_names = self._parse_names(config.get("server_names"))
        self._library_names = self._parse_names(config.get("library_names"))
        self._skip_series_ids = set(self._parse_names(config.get("skip_series_ids")))
        self._episode_overrides = self._parse_episode_overrides(config.get("episode_overrides"))
        self._auto_episode_correction = bool(config.get("auto_episode_correction", False))

    @staticmethod
    def _parse_names(value):
        if isinstance(value, list):
            values = value
        else:
            values = str(value or "").replace("\n", ",").split(",")
        return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))

    @staticmethod
    def _parse_episode_overrides(value):
        result = {}
        if not isinstance(value, list):
            return result
        for item in value:
            if not isinstance(item, dict):
                continue
            tmdb_id = str(item.get("tmdb_id") or "").strip()
            try:
                season = int(item.get("season"))
                total = int(item.get("total_episodes"))
            except (TypeError, ValueError):
                continue
            if tmdb_id.isdecimal() and 0 <= season <= 99 and 1 <= total <= 999:
                result[(tmdb_id, season)] = {"tmdb_id": tmdb_id, "season": season,
                                             "total_episodes": total}
        return result

    def _episode_override(self, tmdb_id, season_number):
        return getattr(self, "_episode_overrides", {}).get((str(tmdb_id), int(season_number)))

    @staticmethod
    def _douban_title_match(results, name, year, season_number):
        def normalize(value):
            value = re.sub(r"第\s*[一二三四五六七八九十百\d]+\s*季|第\s*[一二三四五六七八九十百\d]+\s*期|\bS\s*\d{1,2}\b|\bSeason\s*\d{1,2}\b", "", str(value or ""), flags=re.I)
            return re.sub(r"[^\w\u3400-\u9fff]", "", value).casefold()

        def season_of(value):
            title = str(value or "")
            match = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*(?:季|期)|\bS\s*(\d{1,2})\b|\bSeason\s*(\d{1,2})\b", title, re.I)
            if not match:
                return 1
            raw = next((value for value in match.groups() if value), "")
            if raw.isdecimal():
                return int(raw)
            digits = {character: index + 1 for index, character in enumerate("一二三四五六七八九")}
            if "十" in raw:
                tens, ones = raw.split("十", 1)
                return (digits.get(tens, 1) if tens else 1) * 10 + digits.get(ones, 0)
            return digits.get(raw, 0)

        title = normalize(name)
        matches = []
        for item in results:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            item_title = normalize(item.get("title"))
            item_season = season_of(item.get("title"))
            if season_number > 1 and item_season == 1 and item_title == f"{title}{season_number}":
                item_title = title
                item_season = season_number
            if (item_title == title and item_season == season_number
                    and (not year or not item.get("year") or str(item["year"])[:4] == year)):
                matches.append(item)
        return matches[0] if len(matches) == 1 else None


    @staticmethod
    def _explicit_episode_count(value):
        match = re.fullmatch(r"\s*(\d{1,3})\s*(?:集)?\s*", str(value or ""))
        if not match:
            return None
        count = int(match.group(1))
        return count if 1 <= count <= 999 else None

    def _douban_suggest_match(self, name, year, season_number):
        queries = [name] if season_number == 1 else [f"{name} 第{season_number}季", f"{name}{season_number}"]
        for query in queries:
            payload = self._request_json("https://movie.douban.com/j/subject_suggest?q=" + quote(query))
            match = self._douban_title_match(
                payload if isinstance(payload, list) else [], name, year, season_number
            )
            if match:
                return match
        return None

    def _douban_subject_episode_total(self, subject_id):
        """读取豆瓣页面“集数”。摘要接口才包含该字段，条目 JSON 经常只有标题。"""
        abstract = self._request_json(
            f"https://movie.douban.com/j/subject_abstract?subject_id={subject_id}"
        )
        subject = abstract.get("subject") if isinstance(abstract, dict) else None
        if isinstance(subject, dict):
            count = self._explicit_episode_count(subject.get("episodes_count"))
            is_movie = (subject.get("is_tv") is False
                        and str(subject.get("subtype") or "").upper() == "MOVIE")
            if count and not is_movie:
                return count
        subject = self._request_json(f"https://movie.douban.com/j/subject/{subject_id}")
        if isinstance(subject, dict):
            for field in ("episodes_count", "episodes_count_str", "episode_count", "total_episodes"):
                count = self._explicit_episode_count(subject.get(field))
                if count:
                    return count
        payload = self._request_json(f"https://movie.douban.com/j/tv/series/{subject_id}")
        episodes = payload.get("episodes") if isinstance(payload, dict) else None
        numbers = {int(item.get("episode")) for item in episodes or []
                   if isinstance(item, dict) and str(item.get("episode") or "").isdecimal()
                   and int(item["episode"]) > 0}
        if numbers and numbers == set(range(1, max(numbers) + 1)):
            return max(numbers)
        return None

    def _douban_episode_total(self, series, season_number, season_year=None):
        """读取豆瓣公开分集数量；无法可靠匹配时返回 None。"""
        if season_number <= 0:
            return None
        provider_ids = series.get("ProviderIds") or {}
        subject_id = ""
        if season_number == 1:
            subject_id = str(provider_ids.get("Douban") or provider_ids.get("douban") or
                             provider_ids.get("DOUBAN") or "")
        name = str(series.get("Name") or "")
        base_name = re.sub(
            r"\s*(?:第\s*[一二三四五六七八九十百\d]+\s*(?:季|期)|S\s*\d{1,2}|Season\s*\d{1,2})\s*$",
            "", name, flags=re.I,
        ).strip()
        year = str(season_year or series.get("ProductionYear") or "")[:4]
        try:
            checked = set()
            if subject_id.isdecimal():
                checked.add(subject_id)
                total = self._douban_subject_episode_total(subject_id)
                if total:
                    return total
            match = self._douban_suggest_match(base_name, year, season_number)
            if not match:
                return None
            matched_id = str(match.get("id") or "")
            if matched_id.isdecimal() and matched_id not in checked:
                total = self._douban_subject_episode_total(matched_id)
                if total:
                    return total
            return self._explicit_episode_count(match.get("episode"))
        except Exception as error:  # noqa: BLE001 - 豆瓣不可用时回退 TMDB
            logger.debug(f"【{self.plugin_name}】豆瓣集数读取失败：{type(error).__name__}")
            return None

    def _douban_subject_broadcast_status(self, subject_id):
        """用豆瓣“更新至/完结”判断在播状态；电影或无资料时返回 None。"""
        payload = self._request_json(
            f"https://m.douban.com/rexxar/api/v2/tv/{subject_id}?for_mobile=1",
            headers={"Referer": "https://m.douban.com/"},
        )
        if not isinstance(payload, dict) or payload.get("is_tv") is False:
            return None
        info = str(payload.get("episodes_info") or "")
        total = self._explicit_episode_count(payload.get("episodes_count"))
        if "完结" in info:
            return "完结"
        updated = re.search(r"更新至\s*(\d+)\s*集", info)
        if updated:
            current = int(updated.group(1))
            if total and current >= total:
                return "完结"
            return "在播"
        if payload.get("is_released") is False or str(payload.get("pre_release_desc") or "").strip():
            return "待播"
        if total and payload.get("is_released") is True and not info:
            return "完结"
        return None

    def _douban_broadcast_status(self, series, season_number, season_year=None):
        if season_number <= 0:
            return None
        provider_ids = series.get("ProviderIds") or {}
        subject_id = ""
        if season_number == 1:
            subject_id = str(provider_ids.get("Douban") or provider_ids.get("douban") or
                             provider_ids.get("DOUBAN") or "")
        name = str(series.get("Name") or "")
        base_name = re.sub(
            r"\s*(?:第\s*[一二三四五六七八九十百\d]+\s*(?:季|期)|S\s*\d{1,2}|Season\s*\d{1,2})\s*$",
            "", name, flags=re.I,
        ).strip()
        year = str(season_year or series.get("ProductionYear") or "")[:4]
        try:
            if subject_id.isdecimal():
                status = self._douban_subject_broadcast_status(subject_id)
                if status:
                    return status
            match = self._douban_suggest_match(base_name, year, season_number)
            matched_id = str((match or {}).get("id") or "")
            if matched_id.isdecimal() and matched_id != subject_id:
                return self._douban_subject_broadcast_status(matched_id)
        except Exception as error:  # noqa: BLE001 - 豆瓣不可用时继续使用 TMDB
            logger.debug(f"【{self.plugin_name}】豆瓣在播状态读取失败：{type(error).__name__}")
        return None

    @staticmethod
    def _broadcast_status(details, season_number, episodes, today, expected_total):
        """按本季播出日期判断待播、在播或完结；日期不足时返回 None。"""
        try:
            current = datetime.strptime(str(today)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None
        aired = future = undated = 0
        for episode in episodes or []:
            if not isinstance(episode, dict):
                continue
            raw = str(episode.get("air_date") or "")
            if not raw:
                undated += 1
                continue
            try:
                air_date = datetime.strptime(raw[:10], "%Y-%m-%d").date()
            except ValueError:
                undated += 1
                continue
            if air_date > current:
                future += 1
            else:
                aired += 1
        if aired or future:
            if aired == 0:
                return "待播"
            if future or undated or (expected_total and aired < expected_total):
                return "在播"
            return "完结"
        series_status = str((details or {}).get("status") or "")
        next_episode = (details or {}).get("next_episode_to_air") or {}
        if isinstance(next_episode, dict) and next_episode.get("season_number") == season_number:
            return "待播"
        if series_status in {"Ended", "Canceled"}:
            return "完结"
        return None

    def _season_airing_status(self, series, season_number, details, episodes, today, expected_total, season_year):
        status = self._broadcast_status(details, season_number, episodes, today, expected_total)
        if status:
            return status
        return self._douban_broadcast_status(series, season_number, season_year) or "未知"

    def backfill_airing_status(self):
        """旧扫描结果没有在播状态时，按已保存的 TMDB 季度补齐，避免页面全部显示未知。"""
        if getattr(self, "_is_scanning", False):
            return
        pending = [item for item in self._results if isinstance(item, dict) and not item.get("AiringStatus")]
        if not pending:
            return
        tmdb_key = str(getattr(settings, "TMDB_API_KEY", "") or "")
        if not tmdb_key:
            return
        domain = str(getattr(settings, "TMDB_API_DOMAIN", "") or "api.themoviedb.org")
        today = datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d")
        filled = 0
        for item in pending:
            status = self._saved_airing_status(item, tmdb_key, domain, today)
            item["AiringStatus"] = status if status in {"待播", "在播", "完结"} else "未知"
            if item["AiringStatus"] != "未知":
                filled += 1
        self.save_data(self._DATA_KEY, self._results)
        logger.info(f"【{self.plugin_name}】已为 {filled}/{len(pending)} 条旧缺集记录补齐在播状态")

    def _saved_airing_status(self, item, tmdb_key, domain, today):
        tmdb_id = str(item.get("TmdbId") or "")
        try:
            season_number = int(item.get("SeasonNum"))
            expected_total = int(item.get("TotalEpisodes") or 0)
        except (TypeError, ValueError):
            return None
        if not tmdb_id.isdecimal() or season_number < 0:
            return None
        details = self._request_json(
            f"https://{domain}/3/tv/{tmdb_id}?language=zh-CN&api_key={tmdb_key}"
        ) or {}
        season = self._request_json(
            f"https://{domain}/3/tv/{tmdb_id}/season/{season_number}?language=zh-CN&api_key={tmdb_key}"
        ) or {}
        episodes = [episode for episode in season.get("episodes") or [] if isinstance(episode, dict)]
        if expected_total:
            episodes = [episode for episode in episodes
                        if int(episode.get("episode_number") or 0) <= expected_total]
        year = str((next((row.get("air_date") for row in details.get("seasons") or []
                          if row.get("season_number") == season_number), "") or item.get("Year") or ""))[:4]
        series = {"Name": item.get("SeriesName") or details.get("name") or "",
                  "ProductionYear": item.get("Year") or year}
        return self._season_airing_status(series, season_number, details, episodes, today, expected_total, year)

    def _auto_episode_total(self, series, season_number, tmdb_total, details=None, season_year=None):
        if not getattr(self, "_auto_episode_correction", False):
            return tmdb_total, "TMDB"
        genres = (details or {}).get("genres") or []
        if any(isinstance(genre, dict) and
               (genre.get("id") == 16 or str(genre.get("name") or "").casefold() == "animation")
               for genre in genres):
            return tmdb_total, "TMDB（动画类型不自动修正）"
        douban_total = self._douban_episode_total(series, season_number, season_year)
        if douban_total:
            if douban_total < tmdb_total:
                logger.info(
                    f"【{self.plugin_name}】{series.get('Name') or '未知剧集'} S{season_number:02d} "
                    f"集数自动修正：TMDB {tmdb_total} → 豆瓣 {douban_total}"
                )
                return douban_total, "豆瓣匹配（下修）"
            if douban_total == tmdb_total:
                return tmdb_total, "TMDB/豆瓣一致"
            logger.info(
                f"【{self.plugin_name}】{series.get('Name') or '未知剧集'} S{season_number:02d} "
                f"豆瓣 {douban_total} 集多于 TMDB {tmdb_total} 集；自动模式不向上增加期望集数"
            )
            return tmdb_total, "TMDB（豆瓣集数较高，未上修）"
        logger.info(
            f"【{self.plugin_name}】{series.get('Name') or '未知剧集'} S{season_number:02d} "
            "没有可靠豆瓣集数，回退 TMDB；媒体库集号只用于缺集比对，不推断全集数"
        )
        return tmdb_total, "TMDB（豆瓣无可靠数据）"

    def _load_saved_data(self):
        results = self.owner.get_data(self._DATA_KEY)
        timestamp = self.owner.get_data(self._TIME_KEY)
        cancelled = self.owner.get_data(self._CANCELLED_KEY)
        self._results = results if isinstance(results, list) else []
        self._cancelled_subscriptions = cancelled if isinstance(cancelled, list) else []
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

    def _cancel_completed_subscriptions(
        self, completed: Set[Tuple[str, int, str]]
    ) -> List[Dict[str, Any]]:
        """取消满足已启用规则且 Emby 对应季度完整的订阅。"""
        logger.info(f"【{self.plugin_name}】自动取消核对：检测范围内有 {len(completed)} 个符合取消规则且完整的季度")
        if not self._subscribe_chain or not completed:
            if not completed:
                logger.info(f"【{self.plugin_name}】未发现取消候选，请核对媒体库范围、跳过剧集、已启用的取消规则及本地分集编号")
            return []
        completed_index = {(tmdb_id, season): title for tmdb_id, season, title in completed}
        cancelled: List[Dict[str, Any]] = []
        try:
            subscribes = self._subscribe_chain.subscription_repository.list()
        except Exception as error:  # noqa: BLE001 - 读取失败不影响缺集结果
            logger.error(f"【{self.plugin_name}】读取待核对订阅失败：{error}")
            return []
        television_count = matched_count = failed_count = 0
        source_skipped = season_skipped = unmatched_count = 0
        for subscribe in subscribes or []:
            if getattr(subscribe, "type", None) != MediaType.TV.value:
                continue
            television_count += 1
            if getattr(subscribe, "media_source", None) != MediaSource.TMDB:
                source_skipped += 1
                continue
            tmdb_id = str(getattr(subscribe, "media_id", "") or "")
            try:
                season = int(getattr(subscribe, "season", None))
            except (TypeError, ValueError):
                season_skipped += 1
                continue
            title = completed_index.get((tmdb_id, season))
            subscribe_id = getattr(subscribe, "id", None)
            if not title or not subscribe_id:
                unmatched_count += 1
                continue
            matched_count += 1
            try:
                if self._subscribe_chain._delete_subscription(int(subscribe_id)):
                    cancelled.append({
                        "id": int(subscribe_id), "name": title,
                        "tmdb_id": tmdb_id, "season": season,
                    })
                    logger.info(
                        f"【{self.plugin_name}】{title} S{season:02d} 满足取消规则且本地完整，"
                        f"自动取消订阅 {subscribe_id}"
                    )
                else:
                    failed_count += 1
                    logger.warning(f"【{self.plugin_name}】自动取消 {title} S{season:02d} 未成功，订阅 {subscribe_id} 删除接口返回失败")
            except Exception as error:  # noqa: BLE001 - 单条失败不影响其他订阅
                failed_count += 1
                logger.error(
                    f"【{self.plugin_name}】自动取消 {title} S{season:02d} 订阅失败：{error}"
                )
        logger.info(
            f"【{self.plugin_name}】自动取消核对：电视剧订阅 {television_count} 个，"
            f"非 TMDB 来源 {source_skipped} 个，无有效季度 {season_skipped} 个，"
            f"未匹配完整完结季度 {unmatched_count} 个，匹配 {matched_count} 个，"
            f"成功 {len(cancelled)} 个，失败 {failed_count} 个"
        )
        return cancelled

    @staticmethod
    def _aired_season_reason(details, season_number, episode_count, episodes, today):
        if season_number <= 0:
            return "季度模式不处理特别篇"
        if type(episode_count) is not int or episode_count <= 0:
            return "季度总集数无效"
        if len(episodes) != episode_count:
            return "季度分集数量与总集数不一致"
        numbers = [episode.get("episode_number") for episode in episodes]
        if any(type(number) is not int for number in numbers) or set(numbers) != set(range(1, episode_count + 1)):
            return "季度集号不连续或重复"
        next_episode = details.get("next_episode_to_air")
        if next_episode:
            if not isinstance(next_episode, dict) or next_episode.get("season_number") == season_number or not isinstance(next_episode.get("season_number"), int):
                return "TMDB 仍有本季待播集或待播季度不明确"
        try:
            dates = [datetime.strptime(episode.get("air_date") or "", "%Y-%m-%d").date() for episode in episodes]
            elapsed = (datetime.strptime(today, "%Y-%m-%d").date() - max(dates)).days
        except (TypeError, ValueError):
            return "存在未知或无效播出日期"
        if elapsed < 7:
            return "末集尚未播出或播出未满 7 天"
        return ""

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
    def _request_json(url: str, headers=None) -> Any:
        """请求 JSON，失败时返回空值并记录调试日志。"""
        try:
            client = RequestUtils(headers=headers) if headers else RequestUtils()
            response = client.get_res(url)
            if response and response.status_code == 200:
                payload = response.json()
                return payload if isinstance(payload, (dict, list)) else None
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
    ) -> Tuple[List[Dict[str, Any]], Set[Tuple[str, int, str]]]:
        """将一部 Emby 剧集与 TMDB 季集信息进行比对。"""
        series_id = str(series.get("Id") or "")
        provider_ids = series.get("ProviderIds") or {}
        tmdb_id = provider_ids.get("Tmdb") or provider_ids.get("tmdb") or provider_ids.get("TMDB")
        if not series_id or not tmdb_id:
            return [], set()
        if str(tmdb_id) in self._skip_series_ids:
            logger.debug(
                f"【{self.plugin_name}】{series.get('Name') or tmdb_id} 已配置跳过检测"
            )
            return [], set()

        details = self._request_json(
            f"https://{tmdb_domain}/3/tv/{tmdb_id}?language=zh-CN&api_key={tmdb_key}"
        )
        if not details:
            return [], set()

        local_inventory = inventory.get(series_id, {})
        results: List[Dict[str, Any]] = []
        completed: Set[Tuple[str, int, str]] = set()
        series_title = str(series.get("Name") or details.get("name") or "未知剧集")
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

            override = self._episode_override(tmdb_id, season_number)
            tmdb_total = int(season.get("episode_count") or 0)
            season_year = str(season.get("air_date") or details.get("first_air_date") or
                              series.get("ProductionYear") or "")[:4]
            episodes = []
            for episode in season_details.get("episodes") or []:
                if not isinstance(episode, dict):
                    continue
                try:
                    episode_number = int(episode.get("episode_number") or 0)
                except (TypeError, ValueError):
                    continue
                if episode_number <= tmdb_total:
                    episodes.append(episode)

            expected_total, total_source = (
                (override["total_episodes"], "手动修正") if override else
                self._auto_episode_total(series, season_number, tmdb_total, details, season_year)
            )
            episodes = [episode for episode in episodes
                        if int(episode.get("episode_number") or 0) <= expected_total]

            missing: Set[int] = set()
            expected: Set[int] = set()
            aired_total = 0
            for episode in episodes:
                episode_number = episode.get("episode_number")
                air_date = episode.get("air_date")
                try:
                    episode_number = int(episode_number)
                except (TypeError, ValueError):
                    continue
                if episode_number > 0:
                    expected.add(episode_number)
                if self._ignore_future and (not air_date or air_date > today):
                    continue
                aired_total += 1
                if episode_number not in local_episodes:
                    missing.add(episode_number)
            strict_completed = self._auto_cancel_completed and details.get("status") == "Ended"
            aired_reason = self._aired_season_reason(
                details, season_number, expected_total,
                episodes, today,
            ) if self._auto_cancel_aired_season else "季度模式未开启"
            if ((strict_completed or (self._auto_cancel_aired_season and not aired_reason))
                    and expected and len(expected) == expected_total
                    and expected.issubset(local_episodes)):
                completed.add((str(tmdb_id), season_number, series_title))
                logger.info(
                    f"【{self.plugin_name}】{series_title} S{season_number:02d} 取消候选："
                    f"{'整剧 Ended 且本地完整' if strict_completed else '本季全部播出满 7 天且本地完整'}"
                )
            elif self._auto_cancel_completed or self._auto_cancel_aired_season:
                logger.debug(
                    f"【{self.plugin_name}】{series_title} S{season_number:02d} 保留订阅："
                    f"TMDB 状态 {details.get('status') or '未知'}，"
                    f"季度集数 {expected_total}，有效分集 {len(expected)}，"
                    f"本地缺少 {self._format_episode_ranges(expected - local_episodes) or '无'}，"
                    f"季度判定：{aired_reason or '已播出满 7 天'}"
                )
            if not missing:
                continue
            results.append(
                {
                    "ServerName": server_name,
                    "LibraryName": library_name,
                    "SeriesName": series_title,
                    "Year": str(series.get("ProductionYear") or (details.get("first_air_date") or "")[:4]),
                    "TmdbId": str(tmdb_id),
                    "SeriesId": series_id,
                    "SeasonNum": season_number,
                    "SeasonFormatted": "SP" if season_number == 0 else f"S{season_number}",
                    "MissingEpisodeNumbers": sorted(missing),
                    "MissingEpisodes": self._format_episode_ranges(missing),
                    "TotalEpisodes": expected_total or aired_total,
                    "TotalEpisodesSource": total_source,
                    "AiringStatus": self._season_airing_status(
                        series, season_number, details, episodes, today, expected_total, season_year
                    ),
                    "ActionResult": "待处理",
                }
            )
        return results, completed

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
    ) -> Tuple[List[Dict[str, Any]], Set[Tuple[str, int, str]]]:
        """扫描一个 Emby 媒体库并返回缺失季集。"""
        library_id = str(library.get("Id") or "")
        library_name = str(library.get("Name") or library_id)
        if not library_id:
            return [], set()
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
        completed: Set[Tuple[str, int, str]] = set()
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
                    series_results, series_completed = future.result()
                    results.extend(series_results)
                    completed.update(series_completed)
                except Exception as error:  # noqa: BLE001 - 单剧失败不影响其他剧集
                    logger.error(f"【{self.plugin_name}】单部剧集检测失败：{error}")
        return results, completed

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

    def verify_inventory(self, record: Dict[str, Any]) -> List[int]:
        """只读复查目标分集，失败必须报错；不新增或取消任何订阅。"""
        from urllib.parse import urlencode

        if not self._scan_lock.acquire(blocking=False):
            raise ValueError("缺集检测正在扫描，请稍后复查")
        try:
            if not self._mediaserver_helper:
                raise ValueError("Emby 服务未初始化")
            services = self._mediaserver_helper.get_services(type_filter="emby") or {}
            values = services.values() if isinstance(services, dict) else services
            matches = [service for service in values if str(service.config.name) == record["ServerName"]]
            if len(matches) != 1:
                raise ValueError("无法唯一匹配原 Emby 服务器")
            service = matches[0]
            config = service.config.config or {}
            host, api_key = str(config.get("host") or "").rstrip("/"), str(config.get("apikey") or "")
            user_id = str(service.instance.get_user() or "")
            if not host or not api_key or not user_id:
                raise ValueError("Emby 连接配置不完整")
            views = self._request_json(f"{host}/emby/Users/{user_id}/Views?{urlencode({'api_key': api_key})}")
            if not views or not isinstance(views.get("Items"), list):
                raise ValueError("无法读取 Emby 媒体库")
            libraries = [item for item in views["Items"] if item.get("Name") == record["LibraryName"]]
            if len(libraries) != 1:
                raise ValueError("无法唯一匹配原媒体库")

            def read_items(kind, fields, series_id=""):
                items = []
                while True:
                    query = {"api_key": api_key, "ParentId": libraries[0]["Id"], "Recursive": "true",
                             "IncludeItemTypes": kind, "Fields": fields, "StartIndex": len(items), "Limit": 200}
                    if series_id:
                        query["SeriesId"] = series_id
                    payload = self._request_json(f"{host}/emby/Users/{user_id}/Items?{urlencode(query)}")
                    if not payload or not isinstance(payload.get("Items"), list) or type(payload.get("TotalRecordCount")) is not int:
                        raise ValueError("Emby 分页查询失败或返回不完整，不能判定补全")
                    page, total = payload["Items"], payload["TotalRecordCount"]
                    items.extend(page)
                    if len(items) >= total:
                        return items
                    if not page or len(items) > 100000:
                        raise ValueError("Emby 分页查询未完成")

            series = [item for item in read_items("Series", "ProviderIds")
                      if str(next((value for key, value in (item.get("ProviderIds") or {}).items()
                                   if key.lower() == "tmdb"), "")) == str(record["TmdbId"])]
            if record.get("SeriesId"):
                series = [item for item in series if item.get("Id") == record["SeriesId"]]
            if len(series) != 1:
                raise ValueError("原剧集不存在或无法唯一匹配，不能判定补全")
            present = set()
            for episode in read_items("Episode", "IndexNumberEnd,LocationType", series[0]["Id"]):
                if episode.get("LocationType") == "Virtual" or episode.get("SeriesId") != series[0]["Id"]:
                    continue
                try:
                    if int(episode.get("ParentIndexNumber")) != int(record["SeasonNum"]):
                        continue
                    first = int(episode["IndexNumber"])
                    last = int(episode.get("IndexNumberEnd") or first)
                    if 0 < first <= last <= 9999:
                        present.update(range(first, last + 1))
                except (TypeError, ValueError, KeyError):
                    continue
            remaining = sorted(set(record["MissingEpisodeNumbers"]) - present)
            key_fields = ("ServerName", "LibraryName", "TmdbId", "SeasonNum")
            for item in list(self._results):
                if all(str(item.get(key)) == str(record.get(key)) for key in key_fields):
                    current = set(item["MissingEpisodeNumbers"]) - present
                    if current:
                        item["MissingEpisodeNumbers"] = sorted(current)
                        item["MissingEpisodes"] = self._format_episode_ranges(current)
                    else:
                        self._results.remove(item)
            self.save_data(self._DATA_KEY, self._results)
            return remaining
        finally:
            self._scan_lock.release()

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
            completed: Set[Tuple[str, int, str]] = set()

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
                    f"【{self.plugin_name}】{server_name} 将扫描 {len(libraries)} 个媒体库："
                    f"{'、'.join(str(library.get('Name') or library.get('Id')) for library in libraries)}；"
                    f"整剧取消{'已开启' if self._auto_cancel_completed else '未开启'}，"
                    f"季度取消{'已开启' if self._auto_cancel_aired_season else '未开启'}"
                )
                for library in libraries:
                    library_results, library_completed = self._scan_library(
                        host,
                        api_key,
                        user_id,
                        server_name,
                        library,
                        tmdb_key,
                        tmdb_domain,
                        today,
                    )
                    results.extend(library_results)
                    completed.update(library_completed)

            self._cancelled_subscriptions = (
                self._cancel_completed_subscriptions(completed)
                if self._auto_cancel_completed or self._auto_cancel_aired_season else []
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
            self.save_data(self._CANCELLED_KEY, self._cancelled_subscriptions)
            elapsed = (datetime.now() - started_at).total_seconds()
            logger.info(
                f"【{self.plugin_name}】扫描完成，发现 {len(results)} 条缺失季记录，"
                f"自动取消 {len(self._cancelled_subscriptions)} 个完整完结订阅，耗时 {elapsed:.1f} 秒"
            )
        except Exception as error:  # noqa: BLE001 - 扫描任务必须自行收口
            logger.error(f"【{self.plugin_name}】扫描失败：{error}", exc_info=True)
        finally:
            self._is_scanning = False
            self._scan_lock.release()
