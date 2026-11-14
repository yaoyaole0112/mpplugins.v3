import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SharedSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host_coordinator: bool = False
    bot_username: str = ""
    group: str = "resources"
    instance_label: str = Field("我的资源刷流", min_length=1, max_length=80)
    bot_token: Optional[str] = Field(None, max_length=200)
    allowed_users: list[int] = Field(default_factory=list, max_length=100)

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
        if self.host_coordinator and not self.allowed_users:
            raise ValueError("运行协调端必须设置个人账号白名单")
        return self
