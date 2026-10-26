"""Validated notification configuration; credentials are never part of config JSON."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


def validate_target(kind: str, target: str) -> str:
    if not isinstance(target, str) or not target or len(target) > 254 or any(c.isspace() for c in target):
        raise ValueError("收件目标不能为空、包含空白或超过 254 字符")
    if kind == "email":
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", target):
            raise ValueError("请填写单个有效邮箱地址")
    elif kind == "wecom_aibot":
        if not re.fullmatch(r"[A-Za-z0-9_@.:-]+", target):
            raise ValueError("请填写有效的 userid 或 chatid")
    else:
        raise ValueError("不支持的通知渠道")
    return target


class EmailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=587, ge=1, le=65535)
    tls: Literal["starttls", "ssl"] = "starttls"
    username: str = Field(default="", max_length=254)
    from_address: str
    max_file_size_mb: int = Field(default=20, ge=1, le=50)

    @field_validator("host", "username")
    @classmethod
    def no_control_chars(cls, value: str) -> str:
        if any(ord(c) < 32 or c.isspace() for c in value):
            raise ValueError("不能包含空白或控制字符")
        return value

    @field_validator("from_address")
    @classmethod
    def email_address(cls, value: str) -> str:
        return validate_target("email", value)


class BotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_id: str = Field(min_length=1, max_length=255)
    max_file_size_mb: int = Field(default=20, ge=1, le=50)

    @field_validator("bot_id")
    @classmethod
    def bot_identifier(cls, value: str) -> str:
        return validate_target("wecom_aibot", value)


class ChannelWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["email", "wecom_aibot"]
    enabled: bool = False
    config: dict
    secret: SecretStr | None = None

    @model_validator(mode="after")
    def validate_config(self):
        schema = EmailConfig if self.kind == "email" else BotConfig
        self.config = schema.model_validate(self.config).model_dump()
        if not self.name.strip():
            raise ValueError("渠道名称不能为空")
        return self


class TestSendRequest(BaseModel):
    target: str = Field(min_length=1, max_length=254)


class DiscoverRequest(BaseModel):
    seconds: int = Field(default=15, ge=1, le=30)
