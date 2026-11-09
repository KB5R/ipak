"""Backend errors, deterministic demo data and the session's Users read cache."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Protocol

from .models import UserDetail, UserList, UserSummary


class BackendError(Exception):
    """A classified failure with an actionable, safe user-facing message."""

    MESSAGES = {
        "unauthenticated": "Unauthenticated. Obtain a Kerberos ticket and retry.",
        "permission-denied": "Permission denied. Check Users read permissions and retry.",
        "connection-error": "Connection failed. Check the server/network and retry.",
        "validation-error": "Validation failed. Check the user login and retry.",
        "not-found": "User no longer exists. Return to Users and refresh.",
    }

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(detail or self.MESSAGES.get(code, "Operation failed. Retry or return."))


class UserBackend(Protocol):
    server: str
    reference_time: datetime

    async def find_users(self, limit: int) -> UserList: ...

    async def show_user(self, login: str) -> UserDetail: ...


class FakeBackend:
    server = "ipa.example.test"
    connection = "Demo · Not connected"
    is_demo = True
    reference_time = datetime(2026, 11, 8, tzinfo=timezone.utc)
    SCENARIOS = (
        "normal", "empty", "slow", "unauthenticated", "permission-denied",
        "connection-error", "validation-error", "detail-error",
    )

    def __init__(self, scenario: str = "normal", *, delay: float = 0.15) -> None:
        if scenario not in self.SCENARIOS:
            raise ValueError(f"Unknown fake scenario: {scenario}")
        self.delay = 2.0 if scenario == "slow" else delay
        self.list_error = scenario if scenario in BackendError.MESSAGES else None
        self.detail_errors: dict[str, str] = (
            {"mpopkov": "connection-error"} if scenario == "detail-error" else {}
        )
        self.find_calls: list[int] = []
        self.show_calls: list[str] = []
        summaries = [
            UserSummary("mpopkov", "Mikhail Popkov", True),
            UserSummary("ivanov", "Ivan Ivanov", True),
            UserSummary("petrov", "Petr Petrov", False),
        ]
        summaries.extend(
            UserSummary(f"user{index:03}", f"Test User {index:03}", index % 5 != 0)
            for index in range(1, 61)
        )
        self.users = () if scenario == "empty" else tuple(summaries)
        self.details = {
            user.login: UserDetail(
                user=user,
                emails=(f"{user.login}@example.test",),
                direct_groups=("ipausers",),
                comment="Test account",
                given_name=user.full_name.split(" ", 1)[0],
                surname=user.full_name.split(" ", 1)[1],
            )
            for user in self.users
        }
        if self.users:
            self.details["mpopkov"] = UserDetail(
                user=summaries[0], emails=("mpopkov@example.test",),
                direct_groups=("admins", "developers"),
                associated_hbac=("ssh_access",), associated_sudo=("operations",),
                comment="Infrastructure engineer",
                password_expires=datetime(2026, 11, 24, tzinfo=timezone.utc),
                given_name="Mikhail", surname="Popkov",
            )
            self.details["ivanov"] = UserDetail(
                user=summaries[1], emails=("ivanov@example.test",),
                direct_groups=("ipausers",), associated_hbac=(), associated_sudo=(),
                password_expires=datetime(2026, 10, 1, tzinfo=timezone.utc),
                given_name="Ivan", surname="Ivanov",
            )

    async def find_users(self, limit: int, query: str = "") -> UserList:
        self.find_calls.append(limit)
        await asyncio.sleep(self.delay)
        if self.list_error:
            raise BackendError(self.list_error)
        users = tuple(user for user in self.users if query.casefold() in
                      f"{user.login} {user.full_name}".casefold())
        return UserList(users[:limit], truncated=len(users) > limit)

    async def show_user(self, login: str) -> UserDetail:
        self.show_calls.append(login)
        await asyncio.sleep(self.delay)
        if login in self.detail_errors:
            raise BackendError(self.detail_errors[login])
        if login not in self.details:
            raise BackendError("not-found")
        return self.details[login]

    @property
    def api(self):
        if not hasattr(self, "_api"):
            from .fake_api import FakeAPI
            self._api = FakeAPI(self)
        return self._api

    async def execute(self, command, *args, **options):
        return await self.api.execute(command, *args, **options)

    async def supported(self, command):
        return await self.api.supported(command)


class UserService:
    """Session cache and bounded reads, independently of widgets."""

    LIST_LIMIT = 48

    def __init__(self, backend: UserBackend) -> None:
        self.backend = backend
        self._list: UserList | None = None
        self._details: dict[str, UserDetail] = {}
        self._generation = 0
        self._detail_requests: dict[str, int] = {}
        self.query = ""

    async def list_users(self, *, refresh: bool = False, query: str = "") -> UserList:
        if self._list is None or refresh or query != self.query:
            result = (await self.backend.find_users(self.LIST_LIMIT, query)
                      if query else await self.backend.find_users(self.LIST_LIMIT))
            self.query = query
            self._list = result
            if refresh:
                self._generation += 1
                self._details.clear()
        return self._list

    async def get_user(self, login: str, *, refresh: bool = False) -> UserDetail:
        if not refresh and login in self._details:
            return self._details[login]
        request = self._detail_requests.get(login, 0) + 1
        self._detail_requests[login] = request
        generation = self._generation
        detail = await self.backend.show_user(login)
        if generation == self._generation and request == self._detail_requests[login]:
            self._details[login] = detail
        return detail

    def invalidate(self) -> None:
        self._generation += 1
        self._list = None
        self._details.clear()
