"""Mailbox configuration: the stored record, API input models and presets."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...types import new_id, utcnow

WAKE_MODES = ("off", "triage", "full")
DEFAULT_POLL_SECONDS = 120
MIN_POLL_SECONDS = 30
DEFAULT_BACKFILL_DAYS = 14
MAX_BACKFILL_DAYS = 365

_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_EMAIL = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$")


@dataclass
class Mailbox:
    """One configured mailbox. The password lives only in the encrypted secrets."""

    label: str
    email: str
    username: str
    imap_host: str
    imap_port: int
    imap_security: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    allow_self_signed: bool = False
    folders: list[str] = field(default_factory=lambda: ["INBOX"])
    poll_seconds: int = DEFAULT_POLL_SECONDS
    backfill_days: int = DEFAULT_BACKFILL_DAYS
    wake_mode: str = "triage"
    enabled: bool = True
    save_sent_copy: bool = True
    owner_user_id: str | None = None  # runs act as this Handler user
    notify_user_ids: list[str] = field(default_factory=list)
    sent_folder: str | None = None  # detected at test/first sync
    drafts_folder: str | None = None
    created_by: str | None = None
    id: str = field(default_factory=lambda: new_id("mbx"))
    secret_ref: str | None = None
    status: str = "active"  # "active" | "error" | "auth_failed" | "disabled"
    last_sync_at: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())

    def config(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in _CONFIG_FIELDS}

    def public(self) -> dict[str, Any]:
        """What the API returns: everything except secrets."""
        data = asdict(self)
        data.pop("secret_ref", None)
        data["has_password"] = bool(self.secret_ref)
        return data

    @property
    def notify_targets(self) -> list[str]:
        targets = list(self.notify_user_ids or [])
        if not targets and self.owner_user_id:
            targets = [self.owner_user_id]
        return targets


_ROW_FIELDS = ("id", "secret_ref", "status", "last_sync_at", "last_error", "last_error_at",
               "created_at", "updated_at")
_CONFIG_FIELDS = tuple(f.name for f in fields(Mailbox)
                       if f.name not in _ROW_FIELDS)


def mailbox_from_row(row: dict[str, Any], config: dict[str, Any]) -> Mailbox:
    known = {k: v for k, v in config.items() if k in _CONFIG_FIELDS}
    return Mailbox(**known, **{k: row.get(k) for k in _ROW_FIELDS if row.get(k) is not None})


Security = Literal["tls", "starttls"]
WakeMode = Literal["off", "triage", "full"]


def _check_host(value: str) -> str:
    value = (value or "").strip().lower().rstrip(".")
    if not _HOST.fullmatch(value) or ".." in value:
        raise ValueError("must be a host name such as imap.example.com")
    return value


def _check_folders(value: list[str]) -> list[str]:
    cleaned = []
    for name in value:
        name = (name or "").strip()
        if not name or len(name) > 200 or any(ord(c) < 0x20 for c in name):
            raise ValueError("folder names must be 1-200 printable characters")
        if name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        raise ValueError("at least one folder is required")
    if len(cleaned) > 10:
        raise ValueError("at most 10 folders")
    return cleaned


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("imap_host", "smtp_host", check_fields=False)
    @classmethod
    def _host(cls, value):
        return None if value is None else _check_host(value)

    @field_validator("email", check_fields=False)
    @classmethod
    def _email(cls, value):
        if value is None:
            return None
        value = value.strip()
        if not _EMAIL.fullmatch(value):
            raise ValueError("must be an email address")
        return value

    @field_validator("folders", check_fields=False)
    @classmethod
    def _folders(cls, value):
        return None if value is None else _check_folders(value)

    @field_validator("password", check_fields=False)
    @classmethod
    def _password(cls, value):
        if value is not None and (not value or len(value) > 512 or "\n" in value
                                  or "\r" in value):
            raise ValueError("must be 1-512 characters on one line")
        return value


class MailboxCreate(_Fields):
    label: str = Field("", max_length=100)
    email: str = Field(..., max_length=254)
    username: str | None = Field(None, max_length=254)
    password: str = Field(..., repr=False)
    imap_host: str
    imap_port: int = Field(993, ge=1, le=65535)
    imap_security: Security = "tls"
    smtp_host: str
    smtp_port: int = Field(465, ge=1, le=65535)
    smtp_security: Security = "tls"
    allow_self_signed: bool = False
    folders: list[str] = Field(default_factory=lambda: ["INBOX"])
    poll_seconds: int = Field(DEFAULT_POLL_SECONDS, ge=MIN_POLL_SECONDS, le=86400)
    backfill_days: int = Field(DEFAULT_BACKFILL_DAYS, ge=0, le=MAX_BACKFILL_DAYS)
    wake_mode: WakeMode = "triage"
    enabled: bool = True
    save_sent_copy: bool = True
    owner_user_id: str | None = Field(None, max_length=100)
    notify_user_ids: list[str] = Field(default_factory=list, max_length=20)


class MailboxUpdate(_Fields):
    """Fields left out keep their value; `password` replaces the stored one."""

    label: str | None = Field(None, max_length=100)
    email: str | None = Field(None, max_length=254)
    username: str | None = Field(None, max_length=254)
    password: str | None = Field(None, repr=False)
    imap_host: str | None = None
    imap_port: int | None = Field(None, ge=1, le=65535)
    imap_security: Security | None = None
    smtp_host: str | None = None
    smtp_port: int | None = Field(None, ge=1, le=65535)
    smtp_security: Security | None = None
    allow_self_signed: bool | None = None
    folders: list[str] | None = None
    poll_seconds: int | None = Field(None, ge=MIN_POLL_SECONDS, le=86400)
    backfill_days: int | None = Field(None, ge=0, le=MAX_BACKFILL_DAYS)
    wake_mode: WakeMode | None = None
    enabled: bool | None = None
    save_sent_copy: bool | None = None
    owner_user_id: str | None = Field(None, max_length=100)
    notify_user_ids: list[str] | None = Field(None, max_length=20)


class MailboxTest(_Fields):
    """Test settings before saving (password required). Other form fields are ignored."""

    model_config = ConfigDict(extra="ignore")

    email: str = Field(..., max_length=254)
    username: str | None = Field(None, max_length=254)
    password: str = Field(..., repr=False)
    imap_host: str
    imap_port: int = Field(993, ge=1, le=65535)
    imap_security: Security = "tls"
    smtp_host: str
    smtp_port: int = Field(465, ge=1, le=65535)
    smtp_security: Security = "tls"
    allow_self_signed: bool = False


# Known providers by email domain. Ports: implicit TLS (993/465) by default.
PRESETS: dict[str, dict[str, Any]] = {
    "chengmail.cn": {"imap_host": "pop.chengmail.cn", "smtp_host": "smtp.chengmail.cn",
                     "auth_code": True},
    # Hosted on chengmail.cn (the first real mailbox).
    "yeup-tube.com": {"imap_host": "pop.chengmail.cn", "smtp_host": "smtp.chengmail.cn",
                      "auth_code": True, "provider": "chengmail.cn"},
    "qq.com": {"imap_host": "imap.qq.com", "smtp_host": "smtp.qq.com", "auth_code": True},
    "foxmail.com": {"imap_host": "imap.qq.com", "smtp_host": "smtp.qq.com", "auth_code": True},
    "exmail.qq.com": {"imap_host": "imap.exmail.qq.com", "smtp_host": "smtp.exmail.qq.com",
                      "auth_code": True},
    "163.com": {"imap_host": "imap.163.com", "smtp_host": "smtp.163.com", "auth_code": True},
    "126.com": {"imap_host": "imap.126.com", "smtp_host": "smtp.126.com", "auth_code": True},
    "yeah.net": {"imap_host": "imap.yeah.net", "smtp_host": "smtp.yeah.net", "auth_code": True},
    "sina.com": {"imap_host": "imap.sina.com", "smtp_host": "smtp.sina.com", "auth_code": True},
    "aliyun.com": {"imap_host": "imap.aliyun.com", "smtp_host": "smtp.aliyun.com"},
    "gmail.com": {"imap_host": "imap.gmail.com", "smtp_host": "smtp.gmail.com",
                  "auth_code": True},
    "outlook.com": {"imap_host": "outlook.office365.com", "smtp_host": "smtp.office365.com",
                    "smtp_port": 587, "smtp_security": "starttls"},
    "hotmail.com": {"imap_host": "outlook.office365.com", "smtp_host": "smtp.office365.com",
                    "smtp_port": 587, "smtp_security": "starttls"},
    "yahoo.com": {"imap_host": "imap.mail.yahoo.com", "smtp_host": "smtp.mail.yahoo.com",
                  "auth_code": True},
    "icloud.com": {"imap_host": "imap.mail.me.com", "smtp_host": "smtp.mail.me.com",
                   "smtp_port": 587, "smtp_security": "starttls", "auth_code": True},
}
PRESET_DEFAULTS = {"imap_port": 993, "imap_security": "tls", "smtp_port": 465,
                   "smtp_security": "tls", "auth_code": False}
