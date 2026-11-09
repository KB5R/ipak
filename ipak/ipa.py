"""Local system ipalib adapter. All IPA work runs on one dedicated thread.

ipalib's RPC context is thread-local: initialize and call it on that same thread.
No credentials, TLS overrides, or external HTTP client are used here.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from typing import Any, Mapping

from .backend import BackendError
from .models import UserDetail, UserList, UserSummary


_API_INITIALIZATION = threading.Lock()


def values(row: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = row.get(key)
    if value is None:
        return ()
    return tuple(str(item) for item in value) if isinstance(value, (list, tuple)) else (str(value),)


def first(row: Mapping[str, Any], key: str, default: str = "") -> str:
    return next(iter(values(row, key)), default)


def boolean(row: Mapping[str, Any], key: str) -> bool | None:
    value = row.get(key)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if str(value).lower() in {"true", "1"}:
        return True
    if str(value).lower() in {"false", "0"}:
        return False
    return None


def expiry(row: Mapping[str, Any]) -> datetime | None:
    value = row.get("krbpasswordexpiration")
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    if value:
        try:
            return datetime.strptime(str(value), "%Y%m%d%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def user_summary(row: Mapping[str, Any]) -> UserSummary:
    locked = boolean(row, "nsaccountlock")
    return UserSummary(first(row, "uid"), first(row, "cn") or
                       f"{first(row, 'givenname')} {first(row, 'sn')}".strip(),
                       None if locked is None else not locked)


def user_detail(row: Mapping[str, Any]) -> UserDetail:
    return UserDetail(
        user=user_summary(row), emails=values(row, "mail"),
        direct_groups=values(row, "memberof_group"),
        associated_hbac=values(row, "memberof_hbacrule") if "memberof_hbacrule" in row else None,
        associated_sudo=values(row, "memberof_sudorule") if "memberof_sudorule" in row else None,
        comment="; ".join(values(row, "carlicense")), password_expires=expiry(row),
        given_name=first(row, "givenname"), surname=first(row, "sn"),
        indirect_groups=values(row, "memberofindirect_group"),
    )


class IPABackend:
    is_demo = False

    def __init__(self, api_factory=None) -> None:
        self.server = "Local FreeIPA"
        self.connection = "Not connected"
        self._api_factory = api_factory
        self._api = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ipalib")

    @property
    def reference_time(self) -> datetime:
        return datetime.now(timezone.utc)

    def _initialize(self):
        if self._api is None:
            if self._api_factory is None:
                try:
                    from ipalib import api
                except ImportError as error:
                    raise BackendError("runtime", "System ipalib is unavailable. Run on the FreeIPA server with its system Python.") from error
                # Client plugin registration consults the module-level api.env.
                # Bootstrap it once; RPC connections themselves are thread-local.
                with _API_INITIALIZATION:
                    if not api.isdone("bootstrap"):
                        api.bootstrap(context="cli", debug=False)
                    if not api.isdone("finalize"):
                        api.finalize()
            else:
                api = self._api_factory()
                api.bootstrap(context="cli", debug=False)
                api.finalize()
            self._api = api
            self.server = str(api.env.server)
        if not self._api.Backend.rpcclient.isconnected():
            self.connection = "Connecting…"
            self._api.Backend.rpcclient.connect()
        return self._api

    def _execute(self, command: str, args: tuple, options: dict):
        try:
            api = self._initialize()
            if command not in api.Command:
                raise BackendError("unsupported", f"Server does not support {command}.")
            method = api.Command[command]
            accepted = {parameter.name for parameter in method.options()}
            unsupported = set(options).difference(accepted)
            if unsupported:
                raise BackendError("unsupported", "Unsupported server fields: " + ", ".join(sorted(unsupported)))
            result = method(*args, **options)
            self.connection = "Connected"
            return result
        except BackendError:
            raise
        except Exception as error:
            name = type(error).__name__
            if name in {"ACIError", "AuthorizationError"}:
                code = "permission-denied"
            elif name in {"KerberosError", "CCacheError", "KrbPrincipalExpired", "KrbTicketExpired",
                          "PasswordExpired", "AuthenticationError", "SessionError"}:
                code = "unauthenticated"
                self.connection = "Authentication required"
            elif name in {"NetworkError", "HTTPError", "SSLError", "ConnectionError", "TimeoutError", "OSError"}:
                code = "connection-error"
                self.connection = "Connection failed"
            elif name == "NotFound":
                code = "not-found"
            elif name in {"DuplicateEntry", "ValidationError", "RequirementError", "ConversionError", "EmptyModlist"}:
                code = "validation-error"
            else:
                code = "server-error"
            if code in {"unauthenticated", "connection-error"} and self._api is not None:
                try:
                    self._api.Backend.rpcclient.disconnect()
                except Exception:
                    pass
            sensitive = command == "passwd" or bool(options.get("random")) or "userpassword" in options
            detail = "" if sensitive or code in {"unauthenticated", "connection-error"} else str(error)
            raise BackendError(code, detail) from None

    async def execute(self, command: str, *args, **options):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(self._execute, command, args, options))

    async def supported(self, command: str) -> frozenset[str]:
        def inspect():
            api = self._initialize()
            if command not in api.Command:
                return frozenset()
            return frozenset(parameter.name for parameter in api.Command[command].options())
        try:
            return await asyncio.get_running_loop().run_in_executor(self._executor, inspect)
        except BackendError:
            raise
        except Exception:
            raise BackendError("connection-error") from None

    async def find_users(self, limit: int, query: str = "") -> UserList:
        result = await self.execute("user_find", query or None, sizelimit=limit, all=False)
        return UserList(tuple(user_summary(row) for row in result["result"]), bool(result.get("truncated")))

    async def show_user(self, login: str) -> UserDetail:
        return user_detail((await self.execute("user_show", login, all=True))["result"])

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
