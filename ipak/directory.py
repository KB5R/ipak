"""Plain directory models and operations shared by Textual views."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .backend import BackendError
from .ipa import boolean, first, values


KEYS = {"user": "uid", "host": "fqdn", "sudocmd": "sudocmd"}
LABELS = {"user": "Users", "host": "Hosts", "group": "User Groups", "hostgroup": "Host Groups",
          "hbacrule": "HBAC", "sudorule": "Sudo", "hbacsvc": "Services",
          "hbacsvcgroup": "Service groups", "sudocmd": "Commands", "sudocmdgroup": "Command groups"}
MEMBERSHIP = {
    "user": ("group", "memberof_group", "memberofindirect_group"),
    "host": ("hostgroup", "memberof_hostgroup", "memberofindirect_hostgroup"),
    "group": ("user", "member_user", "memberindirect_user"),
    "hostgroup": ("host", "member_host", "memberindirect_host"),
}


@dataclass(frozen=True)
class Record:
    kind: str
    key: str
    description: str
    attributes: dict[str, tuple[str, ...]]
    enabled: bool | None = None

    def text(self, name: str, default: str = "Not provided") -> str:
        return ", ".join(self.attributes.get(name, ())) or default


@dataclass(frozen=True)
class RecordList:
    records: tuple[Record, ...]
    truncated: bool = False


@dataclass
class WriteResult:
    target: str
    success: bool
    message: str
    secret: str = field(default="", repr=False)
    verified: bool = True


def record(kind: str, row: dict[str, Any]) -> Record:
    return Record(kind, first(row, KEYS.get(kind, "cn")), first(row, "description"),
                  {key: values(row, key) for key in row if key not in {"userpassword", "randompassword", "krbextradata"}},
                  boolean(row, "ipaenabledflag") if kind in {"hbacrule", "sudorule"} else None)


class DirectoryService:
    LIMIT = 48

    def __init__(self, backend, users) -> None:
        self.backend = backend
        self.users = users
        self._cache: dict[tuple[str, str], Record] = {}
        self._generation = 0
        self._requests = {}

    def invalidate(self) -> None:
        self._cache.clear()
        self._generation += 1
        self.users.invalidate()

    async def find(self, kind: str, query: str = "") -> RecordList:
        response = await self.backend.execute(f"{kind}_find", query or None, sizelimit=self.LIMIT, all=False)
        return RecordList(tuple(record(kind, row) for row in response["result"]), bool(response.get("truncated")))

    async def get(self, kind: str, key: str, *, refresh: bool = False) -> Record:
        cache_key = (kind, key)
        if refresh or cache_key not in self._cache:
            generation = self._generation
            request = self._requests.get(cache_key, 0) + 1
            self._requests[cache_key] = request
            result = record(kind, (await self.backend.execute(f"{kind}_show", key, all=True))["result"])
            if generation == self._generation and request == self._requests[cache_key]:
                self._cache[cache_key] = result
            return result
        return self._cache[cache_key]

    async def exists(self, kind: str, key: str) -> bool:
        try:
            await self.get(kind, key, refresh=True)
            return True
        except BackendError as error:
            if error.code == "not-found":
                return False
            raise

    async def supported(self, command: str) -> frozenset[str]:
        return await self.backend.supported(command)

    async def create(self, kind: str, key: str, attributes: dict) -> WriteResult:
        options = dict(attributes)
        if kind == "user":
            options["random"] = True
        try:
            response = await self.backend.execute(f"{kind}_add", key, **options)
        except BackendError as error:
            return WriteResult(key, False, str(error))
        self.invalidate()
        secret = first(response.get("result", {}), "randompassword")
        try:
            result = await self.backend.execute(f"{kind}_show", key, all=True)
            if kind == "user":
                row = result["result"]
                forced = bool(row.get("krbpasswordexpiration")) and row.get("krbpasswordexpiration") == row.get("krblastpwdchange")
                message = "Created · Password change required" if forced else "Created · Verify password expiry policy"
                return WriteResult(key, True, message, secret, verified=bool(secret) and forced)
            return WriteResult(key, True, "Created")
        except BackendError as error:
            return WriteResult(key, True, f"Created; read-back failed: {error}", secret, verified=False)

    async def modify(self, kind: str, key: str, changes: dict) -> WriteResult:
        if not changes:
            return WriteResult(key, True, "No changes")
        try:
            await self.backend.execute(f"{kind}_mod", key, **changes)
        except BackendError as error:
            return WriteResult(key, False, str(error))
        self.invalidate()
        try:
            await self.get(kind, key, refresh=True)
            return WriteResult(key, True, "Saved")
        except BackendError as error:
            return WriteResult(key, True, f"Saved; read-back failed: {error}", verified=False)

    async def delete(self, kind: str, key: str) -> WriteResult:
        if kind not in {"host", "group", "hostgroup"}:
            raise BackendError("unsupported", "Deletion is not offered for this section.")
        try:
            response = await self.backend.execute(f"{kind}_del", key)
            result = response.get("result", {})
            if isinstance(result, dict) and result.get("failed"):
                return WriteResult(key, False, str(result["failed"]))
            self.invalidate()
            try:
                if await self.exists(kind, key):
                    return WriteResult(key, False, "Object is still present after deletion")
                return WriteResult(key, True, "Deleted")
            except BackendError as error:
                return WriteResult(key, True, f"Deletion requested; read-back failed: {error}", verified=False)
        except BackendError as error:
            return WriteResult(key, False, str(error))

    async def user_action(self, action: str, login: str) -> WriteResult:
        try:
            if action == "reset":
                response = await self.backend.execute("user_mod", login, random=True)
                secret = first(response["result"], "randompassword")
                self.invalidate()
                try:
                    row = (await self.backend.execute("user_show", login, all=True))["result"]
                    forced = bool(row.get("krbpasswordexpiration")) and row.get("krbpasswordexpiration") == row.get("krblastpwdchange")
                    return WriteResult(login, True, "Password reset · Change required" if forced else
                                       "Password reset · Verify expiry policy", secret, bool(secret) and forced)
                except BackendError:
                    return WriteResult(login, True, "Password reset; read-back failed", secret, False)
            if action not in {"enable", "disable"}:
                raise BackendError("unsupported", "Unknown user action")
            current = await self.users.get_user(login, refresh=True)
            desired = action == "enable"
            if current.user.enabled == desired:
                return WriteResult(login, True, f"Already {action}d")
            await self.backend.execute(f"user_{action}", login)
            self.invalidate()
            current = await self.users.get_user(login, refresh=True)
            if current.user.enabled != desired:
                return WriteResult(login, False, "Server state did not match the requested action")
            return WriteResult(login, True, current.user.state)
        except BackendError as error:
            return WriteResult(login, False, str(error))

    async def membership(self, kind: str, owner: str):
        target_kind, direct, indirect = MEMBERSHIP[kind]
        current = await self.get(kind, owner, refresh=True)
        return target_kind, set(current.attributes.get(direct, ())), set(current.attributes.get(indirect, ()))

    async def change_member(self, kind: str, owner: str, target: str, add: bool) -> WriteResult:
        target_kind, direct_field, _ = MEMBERSHIP[kind]
        try:
            current = await self.get(kind, owner, refresh=True)
            if (target in current.attributes.get(direct_field, ())) == add:
                return WriteResult(target, True, "Already in requested state")
            if kind in {"user", "host"}:
                command_kind, command_key, member_kind, member_key = target_kind, target, kind, owner
            else:
                command_kind, command_key, member_kind, member_key = kind, owner, target_kind, target
            response = await self.backend.execute(
                f"{command_kind}_{'add' if add else 'remove'}_member", command_key, **{member_kind: [member_key]})
            self.invalidate()
            current = await self.get(kind, owner, refresh=True)
            if (target in current.attributes.get(direct_field, ())) != add:
                return WriteResult(target, False, str(response.get("failed") or "Membership was not changed"))
            return WriteResult(target, True, "Added" if add else "Removed")
        except BackendError as error:
            return WriteResult(target, False, str(error))
