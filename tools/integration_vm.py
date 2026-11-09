"""Opt-in integration check on an explicitly named test FreeIPA server.

Creates only unique ipakcheck* objects and removes them in a finally block.
Passwords are neither printed nor written to disk. Run using system Python's venv.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from uuid import uuid4

from ipak.app import IPAKApp
from ipak.backend import BackendError, UserService
from ipak.directory import DirectoryService
from ipak.ipa import IPABackend
from ipak.rules import RuleDraft, RuleService, empty_state


def require(result, label):
    results = result if isinstance(result, list) else [result]
    failures = [f"{item.target}: {item.message}" for item in results if not item.success or not item.verified]
    if failures:
        raise AssertionError(label + ": " + "; ".join(failures))
    print(label + ": OK", flush=True)


async def run(expected_server, allow_writes):
    backend = IPABackend()
    directory = DirectoryService(backend, UserService(backend))
    planned = []
    try:
        users = await backend.find_users(48)
        if backend.server != expected_server:
            raise RuntimeError(f"Connected server {backend.server!r} does not match explicitly named test server")
        print(f"Read-only bounded users: {len(users.users)}; truncated={users.truncated}", flush=True)
        if users.users:
            await backend.show_user(users.users[0].login)
        for kind in ("host", "group", "hostgroup", "hbacrule", "sudorule"):
            await directory.find(kind)
        print("Read-only directory lists and user detail: OK", flush=True)
        if not allow_writes:
            return
        prefix = "ipakcheck" + uuid4().hex[:8]
        print("Owned test prefix: " + prefix, flush=True)
        group, hostgroup, login = prefix + "-users", prefix + "-hosts", prefix + ".user"
        host = prefix + "." + str(backend._api.env.domain)
        command = "/usr/bin/" + prefix

        async def reserve(kind, key):
            if await directory.exists(kind, key):
                raise RuntimeError("Test target already exists; refusing to modify it")
            planned.append((kind, key))

        for kind, key, attrs in (("group", group, {"description": "IPAK integration fixture"}),
                                 ("hostgroup", hostgroup, {"description": "IPAK integration fixture"}),
                                 ("user", login, {"givenname": "IPAK", "sn": "Integration", "cn": "IPAK Integration",
                                                  "mail": f"{prefix}@example.test", "carlicense": "Temporary integration fixture"}),
                                 ("host", host, {"description": "IPAK integration fixture", "ip_address": "10.254.254.253"})):
            await reserve(kind, key)
            if kind == "host":
                rejected = await directory.create(kind, key, {"ip_address": "192.0.2.123"})
                assert not rejected.success and not await directory.exists(kind, key)
                print("Reserved IP validation surfaced without creating a host: OK", flush=True)
            result = await directory.create(kind, key, attrs)
            require(result, f"Create {kind}")
            if kind == "user":
                assert result.secret
                result.secret = ""
        require(await directory.modify("user", login, {"carlicense": "Changed integration comment"}), "Modify user")
        require(await directory.user_action("disable", login), "Disable user")
        require(await directory.user_action("enable", login), "Enable user")
        result = await directory.user_action("reset", login)
        require(result, "Reset unique temporary password / require change")
        assert result.secret
        result.secret = ""

        response = await backend.execute("group_add_member", group, user=[login, prefix + ".missing"])
        assert response["completed"] == 1 and response["failed"], "Expected a genuine partial membership result"
        directory.invalidate()
        print("Server partial membership result: OK", flush=True)
        require(await directory.change_member("user", login, group, False), "Remove direct user membership")
        require(await directory.change_member("group", group, login, True), "Add user from group editor")
        require(await directory.change_member("host", host, hostgroup, True), "Add host to hostgroup")
        require(await directory.change_member("hostgroup", hostgroup, host, False), "Remove host from hostgroup editor")
        require(await directory.change_member("hostgroup", hostgroup, host, True), "Restore host fixture membership")

        await reserve("sudocmd", command)
        await backend.execute("sudocmd_add", command)
        rule_service = RuleService(directory)
        services = await directory.find("hbacsvc")
        assert services.records, "No registered HBAC services available on the test server"
        for kind in ("hbacrule", "sudorule"):
            key = prefix + ("-hbac" if kind == "hbacrule" else "-sudo")
            await reserve(kind, key)
            original = empty_state(kind)
            desired = deepcopy(original)
            desired.description = "IPAK integration fixture"
            desired.enabled = True
            desired.modes.update(users="selected", hosts="selected")
            desired.members["memberuser_group"] = {group}
            desired.members["memberhost_hostgroup"] = {hostgroup}
            if kind == "hbacrule":
                desired.modes["services"] = "selected"
                desired.members["memberservice_hbacsvc"] = {services.records[0].key}
            else:
                desired.modes["commands"] = "selected"
                desired.members["memberallowcmd_sudocmd"] = {command}
                desired.options = {"authenticate"}
                desired.order = str(500000 + int(prefix[-6:], 16))
            draft = RuleDraft(kind, key, original, desired)
            require(await rule_service.apply(draft), f"Create, configure and verify restricted {kind}")
            desired.enabled = False
            desired.description = "Updated integration fixture"
            require(await rule_service.apply(draft), f"Edit and disable {kind}")

        # Textual's live read-only screens on the same system interpreter / credentials.
        app = IPAKApp(IPABackend())
        async with app.run_test(size=(80, 24)) as pilot:
            for _ in range(50):
                if app.screen.load_state != "loading":
                    break
                await pilot.pause(0.1)
            assert app.screen.load_state == "ready"
            await pilot.press("enter")
            for _ in range(50):
                if app.screen.load_state != "loading":
                    break
                await pilot.pause(0.1)
            assert app.screen.load_state == "ready"
            print("Live Textual Users list/detail 80x24: OK", flush=True)
    finally:
        cleanup_errors = []
        for kind, key in reversed(planned):
            try:
                await backend.execute(f"{kind}_del", key, **({"updatedns": True} if kind == "host" else {}))
            except BackendError as error:
                if error.code != "not-found":
                    cleanup_errors.append(f"{kind} {key}: {error}")
        for kind, key in planned:
            try:
                if await directory.exists(kind, key):
                    cleanup_errors.append(f"Still present: {kind} {key}")
            except BackendError as error:
                cleanup_errors.append(f"Cleanup verification {kind} {key}: {error}")
        backend.close()
        if cleanup_errors:
            raise RuntimeError("Cleanup incomplete: " + "; ".join(cleanup_errors))
        if planned:
            print(f"Cleanup verified: {len(planned)} owned objects removed", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-server", required=True)
    parser.add_argument("--allow-writes", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(run(arguments.expected_server, arguments.allow_writes))
