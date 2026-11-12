import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from .channel_dedup import normalize_title

CHANNEL_DEDUP_ATTRIBUTES = {"resolution", "effects", "codec", "source", "fps", "size"}


def _normalize_optional_positive_number(value):
    """把历史配置中的空值和 0 统一转换为未设置。"""
    if value is None or value == "":
        return None
    try:
        if float(value) == 0:
            return None
    except (TypeError, ValueError):
        return value
    return value


class BrushTaskPayload(BaseModel):
    """
    刷流任务新增与更新请求模型
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=80)
    enabled: bool = True
    notify: bool = True
    site_id: int = Field(..., gt=0)
    downloader: str = Field(..., min_length=1, max_length=80)
    brush_interval: int = Field(10, ge=1, le=1440)
    check_interval: int = Field(5, ge=1, le=1440)
    cron: Optional[str] = None
    active_time_range: Optional[str] = None
    site_ratio_control: bool = False
    site_ratio_target: Optional[float] = Field(None, gt=0)
    disksize: Optional[float] = Field(None, gt=0)
    maxupspeed: Optional[float] = Field(None, gt=0)
    maxdlspeed: Optional[float] = Field(None, gt=0)
    maxdlcount: Optional[int] = Field(None, gt=0)
    freeleech: Literal["", "free", "2xfree"] = "free"
    hr: Literal["yes", "no"] = "yes"
    include: Optional[str] = None
    exclude: Optional[str] = None
    size: Optional[str] = None
    seeder: Optional[str] = None
    timezone_offset: float = 0
    pubtime: Optional[str] = None
    seed_time: Optional[float] = Field(None, gt=0)
    hr_seed_time: Optional[float] = Field(None, gt=0)
    seed_ratio: Optional[float] = Field(None, gt=0)
    seed_size: Optional[float] = Field(None, gt=0)
    download_time: Optional[float] = Field(None, gt=0)
    seed_avgspeed: Optional[float] = Field(None, gt=0)
    seed_inactivetime: Optional[float] = Field(None, gt=0)
    delete_size_range: Optional[str] = None
    up_speed: Optional[float] = Field(None, gt=0)
    dl_speed: Optional[float] = Field(None, gt=0)
    auto_archive_days: Optional[float] = Field(None, gt=0)
    save_path: Optional[str] = None
    delete_except_tags: Optional[str] = None
    except_subscribe: bool = True
    proxy_delete: bool = False
    del_no_free: bool = False
    qb_category: Optional[str] = None
    site_hr_active: bool = False
    recent_episodes_only: bool = False
    blacklist_site_ids: list[int] = Field(default_factory=list, max_length=100)
    download_types: list[Literal["movie", "tv", "variety", "anime", "sports", "documentary", "mv", "music"]] = Field(default_factory=list)
    site_skip_tips: bool = False
    rss_support: bool = False
    tag: Optional[str] = Field(None, validation_alias=AliasChoices("tag", "downloader_tag"))

    @field_validator(
        "disksize",
        "maxupspeed",
        "maxdlspeed",
        "maxdlcount",
        "site_ratio_target",
        "seed_time",
        "hr_seed_time",
        "seed_ratio",
        "seed_size",
        "download_time",
        "seed_avgspeed",
        "seed_inactivetime",
        "up_speed",
        "dl_speed",
        "auto_archive_days",
        mode="before",
    )
    @classmethod
    def normalize_optional_positive_number(cls, value):
        """兼容旧版用 0 表示未配置的可选正数配置。"""
        return _normalize_optional_positive_number(value)

    @field_validator("name", "downloader")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """清理必填文本并拒绝纯空白内容"""
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("字段不能为空")
        return cleaned

    @field_validator(
        "cron",
        "active_time_range",
        "include",
        "exclude",
        "size",
        "seeder",
        "pubtime",
        "delete_size_range",
        "save_path",
        "delete_except_tags",
        "qb_category",
        "tag",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value):
        """把空白可选文本统一转换为 None"""
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @field_validator("tag")
    @classmethod
    def validate_tag(cls, value: Optional[str]) -> Optional[str]:
        """qBittorrent 标签不允许包含逗号"""
        if value and "," in value:
            raise ValueError("下载器标签不能包含逗号")
        return value

    @field_validator("blacklist_site_ids")
    @classmethod
    def normalize_blacklist_site_ids(cls, value: list[int]) -> list[int]:
        """去重并保持所选站点顺序。"""
        if any(site_id <= 0 for site_id in value):
            raise ValueError("种子黑名单站点必须是有效站点")
        return list(dict.fromkeys(value))

    @field_validator("size", "seeder", "pubtime", "delete_size_range")
    @classmethod
    def validate_number_range(cls, value: Optional[str]) -> Optional[str]:
        """校验单值或数字范围配置"""
        if value and not re.fullmatch(r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?", value):
            raise ValueError("请输入数字或数字范围，例如 10 或 10-80")
        return value

    @field_validator("active_time_range")
    @classmethod
    def validate_active_time_range(cls, value: Optional[str]) -> Optional[str]:
        """校验每日开启时间段格式并允许跨越午夜"""
        if not value:
            return None
        if not re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", value):
            raise ValueError("开启时间段格式应为 HH:MM-HH:MM")
        start, end = value.split("-", 1)
        datetime.strptime(start, "%H:%M")
        datetime.strptime(end, "%H:%M")
        return value

    @field_validator("include", "exclude")
    @classmethod
    def validate_regex(cls, value: Optional[str]) -> Optional[str]:
        """提前校验选种正则表达式"""
        if value:
            re.compile(value)
        return value

    @model_validator(mode="after")
    def validate_site_ratio_control(self):
        """启用站点分享率控制时要求同时配置有效目标值。"""
        if self.site_ratio_control and self.site_ratio_target is None:
            raise ValueError("启用站点分享率控制时必须设置目标分享率")
        return self


class BrushTaskStatePayload(BaseModel):
    """
    刷流任务启停请求模型
    """

    enabled: bool


class BrushFlowSettingsPayload(BaseModel):
    """
    刷流插件全局设置请求模型
    """

    enabled: bool = True
    show_sidebar_nav: bool = True
    global_disksize: Optional[float] = Field(None, gt=0)
    global_maxdlcount: Optional[int] = Field(None, gt=0)
    global_maxupspeed: Optional[float] = Field(None, gt=0)
    global_maxdlspeed: Optional[float] = Field(None, gt=0)
    global_proxy_delete: bool = False
    global_delete_size_range: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def clear_global_delete_size_range_when_disabled(cls, data):
        """关闭全局动态删种时忽略隐藏阈值，确保无效草稿不会阻止保存。"""
        if not isinstance(data, dict):
            return data
        enabled = data.get("global_proxy_delete", False)
        if enabled not in (False, None, 0, "", "0", "false", "False"):
            return data
        normalized = dict(data)
        normalized["global_delete_size_range"] = None
        return normalized

    @field_validator(
        "global_disksize",
        "global_maxdlcount",
        "global_maxupspeed",
        "global_maxdlspeed",
        mode="before",
    )
    @classmethod
    def normalize_optional_positive_number(cls, value):
        """兼容全局限额中用于表示不限的空值和 0。"""
        return _normalize_optional_positive_number(value)

    @field_validator("global_delete_size_range", mode="before")
    @classmethod
    def normalize_global_delete_size_range(cls, value):
        """清理全局动态删种阈值中的空白值。"""
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @field_validator("global_delete_size_range")
    @classmethod
    def validate_global_delete_size_range(cls, value: Optional[str]) -> Optional[str]:
        """校验全局动态删种的单值或区间阈值。"""
        if value and not re.fullmatch(r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?", value):
            raise ValueError("请输入数字或数字范围，例如 100 或 50-100")
        if value:
            limits = [float(item) for item in value.split("-")]
            if any(item <= 0 for item in limits) or (len(limits) > 1 and limits[0] >= limits[1]):
                raise ValueError("动态删种区间下限必须小于上限")
        return value

    @model_validator(mode="after")
    def validate_global_dynamic_delete(self):
        """启用全局动态删种时要求配置有效体积阈值。"""
        if self.global_proxy_delete and not self.global_delete_size_range:
            raise ValueError("启用全局动态删种时必须设置动态删种阈值")
        return self


class ChannelDedupSettingsPayload(BaseModel):
    """频道重复资源过滤配置。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    channels: list[str] = Field(default_factory=list, max_length=50)
    cancel_downloading: bool = False
    attributes: list[str] = Field(default_factory=lambda: ["resolution", "effects", "codec", "source", "fps"])
    size_tolerance_pct: float = Field(5, ge=0, le=20)
    unknown_policy: Literal["exclude", "allow"] = "exclude"
    identity_aliases: dict[str, int] = Field(default_factory=dict)

    @field_validator("identity_aliases", mode="before")
    @classmethod
    def validate_identity_aliases(cls, value):
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            pairs = []
            for line in value.splitlines():
                if not line.strip():
                    continue
                match = re.fullmatch(r"\s*(.+?)\s*[=＝]\s*(\d+)\s*", line)
                if not match:
                    raise ValueError("别名绑定格式为：剧名别名 = TMDB剧集ID，每行一项")
                pairs.append((match.group(1), match.group(2)))
        elif isinstance(value, dict):
            pairs = list(value.items())
        else:
            raise ValueError("别名绑定必须是文本或名称到TMDB剧集ID的字典")
        if len(pairs) > 200:
            raise ValueError("最多可配置200个剧名别名")
        result, normalized = {}, {}
        for raw_name, raw_id in pairs:
            name = str(raw_name).strip()
            key = normalize_title(name)
            if not key or len(name) > 200 or isinstance(raw_id, bool) or not re.fullmatch(r"[1-9]\d*", str(raw_id)):
                raise ValueError("别名不能为空且最多200字，TMDB剧集ID必须是正整数")
            tmdb_id = int(raw_id)
            if key in normalized and normalized[key] != tmdb_id:
                raise ValueError("同一个规范化剧名别名不能绑定到不同TMDB剧集ID")
            normalized[key] = tmdb_id
            result[name] = tmdb_id
        return result

    @field_validator("channels", mode="before")
    @classmethod
    def normalize_channels(cls, value):
        if isinstance(value, str):
            value = re.split(r"[,\n\r]+", value)
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValueError("频道列表必须是文本或列表")
        channels = []
        for channel in value:
            cleaned = re.sub(r"^https?://(?:www\.)?(?:t\.me|telegram\.me)/", "", str(channel or "").strip(), flags=re.I)
            cleaned = cleaned.strip().strip("/")
            if not cleaned:
                continue
            if cleaned.startswith("+"):
                raise ValueError("仅支持公开频道用户名或当前账号有权限访问的频道 ID")
            normalized = cleaned if cleaned.startswith("-") or cleaned.startswith("@") else f"@{cleaned}"
            if not re.fullmatch(r"(?:@[A-Za-z][A-Za-z0-9_]{3,31}|-[1-9]\d{1,18})", normalized):
                raise ValueError("请输入有效频道用户名、频道链接或负数频道 ID，不支持邀请链接和消息链接")
            channels.append(normalized.lower())
        if len(set(channels)) > 50:
            raise ValueError("最多可监听 50 个频道")
        return list(dict.fromkeys(channels))

    @field_validator("attributes")
    @classmethod
    def validate_attributes(cls, value):
        unknown = set(value) - CHANNEL_DEDUP_ATTRIBUTES
        if unknown:
            raise ValueError("存在不支持的规格项")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_enabled_channels(self):
        if self.enabled and not self.channels:
            raise ValueError("开启频道监听时请至少添加一个频道")
        return self
