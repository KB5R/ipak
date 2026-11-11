"""Opt-in live Users UI regression on a named test server; owned fixtures only."""

import argparse
import asyncio
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

from ipak.app import IPAKApp, UserTable
from ipak.backend import UserService
from ipak.directory import DirectoryService
from ipak.ipa import IPABackend
from ipak.password_file import PasswordFile


async def run(expected_server, allow_writes):
    if not allow_writes:
        raise RuntimeError("This test requires --allow-writes on a designated test instance")
    backend = IPABackend()
    service = DirectoryService(backend, UserService(backend))
    owned = []
    try:
        await backend.find_users(1)
        if backend.server != expected_server:
            raise RuntimeError("Connected server differs from --expected-server")
        prefix = "ipakux" + uuid4().hex[:8]
        for suffix in ("one", "two"):
            login = prefix + "." + suffix
            if await service.exists("user", login):
                raise RuntimeError("Fixture already exists; stopping")
            owned.append(login)
            result = await service.create("user", login, {"givenname": "IPAK", "sn": "UI Test"})
            assert result.success and result.verified
            result.secret = ""
        print("Created two owned test users", flush=True)
        with tempfile.TemporaryDirectory(prefix="ipak-ux-test-") as directory:
            output = PasswordFile(Path(directory) / "yyy")
            with patch("ipak.actions.PasswordFile", return_value=output):
                app = IPAKApp(IPABackend())
                async with app.run_test(size=(80, 24)) as p:
                    async def settle():
                        await app.workers.wait_for_complete()
                        await p.pause()
                    await settle()
                    home = app.screen
                    home.server_query = prefix
                    home.start_load(refresh=True)
                    await settle()
                    assert set(home.visible_logins) == set(owned)
                    await p.press("space", "down", "space", "x")
                    await p.click("#apply")
                    await settle()
                    assert len(app.screen.results) == 2 and all(r.success for r in app.screen.results)
                    await p.press("escape")
                    await settle()
                    assert all(home.query_one("#users", UserTable).get_row(login)[-1] == "Disabled" for login in owned)
                    print("Multi-user Disable: server success and refreshed TUI states OK", flush=True)
                    await p.press("p")
                    await p.click("#apply")
                    await settle()
                    assert all(r.success and r.verified for r in app.screen.results)
                    rows = [json.loads(line) for line in output.path.read_text().splitlines()]
                    assert {row["login"] for row in rows} == set(owned)
                    assert len({row["temporary_password"] for row in rows}) == 2
                    assert output.path.stat().st_mode & 0o777 == 0o600
                    print("Reset: unique passwords saved to owner-only file; forced change verified", flush=True)
                    await p.press("escape")
                    await settle()
                    login = home.query_one("#users", UserTable).current_login
                    await p.press("enter")
                    await settle()
                    await p.press("a", "enter")
                    await p.click("#apply")
                    await settle()
                    assert app.screen.results[0].success
                    await p.press("escape")
                    await settle()
                    await p.press("escape")
                    await settle()
                    assert home.query_one("#users", UserTable).get_row(login)[-1] == "Enabled"
                    print("Single-user action in card: returning list shows refreshed state OK", flush=True)
    finally:
        try:
            for login in reversed(owned):
                if await service.exists("user", login):
                    await backend.execute("user_del", login)
                assert not await service.exists("user", login)
            print(f"Cleanup verified: {len(owned)} owned users removed; temporary password file removed", flush=True)
        finally:
            backend.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-server", required=True)
    parser.add_argument("--allow-writes", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.expected_server, args.allow_writes))
