"""Plain data presented by the UI; no Textual or FreeIPA objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class UserSummary:
    login: str
    full_name: str
    enabled: bool | None

    @property
    def state(self) -> str:
        return "Unknown" if self.enabled is None else "Enabled" if self.enabled else "Disabled"


@dataclass(frozen=True)
class UserList:
    users: tuple[UserSummary, ...]
    truncated: bool = False


@dataclass(frozen=True)
class UserDetail:
    user: UserSummary
    emails: tuple[str, ...] = ()
    direct_groups: tuple[str, ...] = ()
    associated_hbac: tuple[str, ...] | None = None
    associated_sudo: tuple[str, ...] | None = None
    comment: str = ""
    password_expires: datetime | None = None
    given_name: str = ""
    surname: str = ""
    indirect_groups: tuple[str, ...] = ()

    def password_description(self, now: datetime) -> str:
        if self.password_expires is None:
            return "Unknown"
        expiry = self.password_expires.astimezone(timezone.utc)
        status = "Expired" if expiry <= now else "Valid"
        return f"{status} · Expires: {expiry:%Y-%m-%d %H:%M} UTC"
