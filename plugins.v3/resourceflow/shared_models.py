import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .shared_http import HOST_PATTERN, TOKEN_PATTERN


class ApprovalPeer(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    instance_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    sender: int = Field(gt=0, lt=2**53)
    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)


class SharedSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    host_coordinator: bool = False
    bot_username: str = ""
    group: str = "resources"
    instance_label: str = Field("我的资源刷流", min_length=1, max_length=80)
    bot_token: Optional[str] = Field(None, max_length=200)
    allowed_users: list[int] = Field(default_factory=list, max_length=100)
    transport: Literal["telegram", "https"] = "telegram"
    telegram_listener: bool = True
    https_listener: bool = False
    https_host: str = Field("", max_length=135)
    https_authority_id: str = Field("", pattern=r"^(?:[a-f0-9]{32})?$")
    https_token: Optional[str] = Field(None, max_length=43, repr=False)
    https_peers: list[ApprovalPeer] = Field(default_factory=list, max_length=100)

    @field_validator("https_token")
    @classmethod
    def approval_token(cls, value):
        if value and not TOKEN_PATTERN.fullmatch(value):
            raise ValueError("私网审批密钥格式无效")
        return value

    @field_validator("https_host")
    @classmethod
    def approval_host(cls, value):
        host = value.strip().lower().removeprefix("https://").rstrip("/")
        if host and not HOST_PATTERN.fullmatch(host):
            raise ValueError("私网审批只支持 Tailscale HTTPS 域名的 443 端口")
        return host

    @field_validator("https_peers")
    @classmethod
    def approval_peers(cls, peers):
        if len({peer.instance_id for peer in peers}) != len(peers) or len({peer.token_hash for peer in peers}) != len(peers):
            raise ValueError("不同实例必须使用独立的私网审批密钥")
        return peers

    @field_validator("bot_username", mode="before")
    @classmethod
    def username(cls, value):
        name = str(value or "").strip().lstrip("@")
        if name and (not re.fullmatch(r"[A-Za-z0-9_]{5,32}", name) or not name.lower().endswith("bot")):
            raise ValueError("请输入协调 Bot 的 @用户名")
        return "@" + name if name else ""

    @field_validator("bot_token")
    @classmethod
    def token(cls, value):
        if value is not None and value and not re.fullmatch(r"\d{5,20}:[A-Za-z0-9_-]{20,100}", value):
            raise ValueError("Bot Token 格式无效")
        return value

    @field_validator("group", "instance_label")
    @classmethod
    def text(cls, value):
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("字段不能为空")
        return cleaned

    @field_validator("group")
    @classmethod
    def group_name(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            raise ValueError("共享组只支持英文、数字、横线和下划线")
        return value

    @field_validator("allowed_users")
    @classmethod
    def users(cls, value):
        if any(user <= 0 or user >= 2**53 for user in value):
            raise ValueError("白名单须为 Telegram 个人账号的数字 ID")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def destination_required(self):
        if (self.enabled or self.host_coordinator) and not self.bot_username:
            raise ValueError("请填写协调 Bot 用户名")
        if self.host_coordinator and self.telegram_listener and not self.allowed_users:
            raise ValueError("运行协调端必须设置个人账号白名单")
        if self.host_coordinator and not (self.telegram_listener or self.https_listener):
            raise ValueError("协调端必须启用至少一种审批入口")
        if self.host_coordinator and self.https_listener and not self.https_peers:
            raise ValueError("请先配置允许的私网设备")
        if self.host_coordinator and self.transport == "https" and not self.https_listener:
            raise ValueError("本机使用私网审批前须启用私网审批入口")
        if self.enabled and self.host_coordinator and self.transport == "telegram" and not self.telegram_listener:
            raise ValueError("本机使用 Telegram 审批时不能停用 Telegram 审批入口")
        if self.enabled and self.transport == "https" and not self.host_coordinator and not (
            self.https_host and self.https_authority_id
        ):
            raise ValueError("请填写协调端 Tailscale 域名及共享账本 ID")
        return self
