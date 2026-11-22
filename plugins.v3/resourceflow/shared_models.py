import re
from typing import Optional

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
    group: str = "resources"
    instance_label: str = Field("我的资源刷流", min_length=1, max_length=80)
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

    @model_validator(mode="before")
    @classmethod
    def ignore_retired_transport_fields(cls, value):
        if isinstance(value, dict):
            retired = {"bot_username", "bot_token", "allowed_users", "transport", "telegram_listener"}
            return {key: item for key, item in value.items() if key not in retired}
        return value
