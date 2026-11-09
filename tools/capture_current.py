"""Export current terminal screens using only deterministic demo data."""

import asyncio
from pathlib import Path

from ipak.app import IPAKApp, Sidebar
from ipak.backend import FakeBackend
from ipak.bulk_ui import BulkScreen
from ipak.membership import MembershipScreen
from ipak.rule_ui import RuleScreen


async def capture():
    output = Path("docs/screenshots/current")
    output.mkdir(parents=True, exist_ok=True)

    def save(app, name):
        path = output / f"{name}.svg"
        path.write_text(app.export_screenshot(title=f"IPAK · {name}"), encoding="utf-8")
        print(path)

    app = IPAKApp(FakeBackend(delay=0))
    async with app.run_test(size=(80, 24)) as p:
        await p.pause()
        await p.press("space", "down", "space")
        save(app, "users-actions-80x24")
        await p.press("x")
        save(app, "user-action-review-80x24")
        await p.press("escape", "enter")
        await p.pause()
        save(app, "user-detail-80x24")

    app = IPAKApp(FakeBackend(delay=0))
    async with app.run_test(size=(80, 24)) as p:
        await p.pause()
        app.push_screen(MembershipScreen("user", "mpopkov"))
        await p.pause()
        await p.press("tab", "space", "a")
        save(app, "memberships-80x24")
        await p.resize_terminal(120, 36)
        await p.pause()
        save(app, "memberships-120x36")

    app = IPAKApp(FakeBackend(delay=0))
    async with app.run_test(size=(80, 24)) as p:
        await p.pause()
        await p.press("tab", "down", "enter")
        await p.pause()
        save(app, "hosts-80x24")
        await p.press("enter")
        await p.pause()
        save(app, "host-detail-80x24")
        await p.press("escape", "escape", "tab", "down", "down", "enter", "2")
        await p.pause()
        save(app, "host-groups-80x24")

    app = IPAKApp(FakeBackend(delay=0))
    async with app.run_test(size=(120, 36)) as p:
        await p.pause()
        screen = BulkScreen("user", auto_editor=False)
        app.push_screen(screen)
        await p.pause()
        screen.load_content("ФИО*;Почта;Логин;Комментарий\nПетров Иван;ivan@example.test;;Support\nСидорова Юлия;yulia@example.test;;Operations\n")
        screen.rows[0].groups = {"developers"}
        screen.rows[1].groups = {"operators"}
        screen.render_rows()
        await p.pause()
        save(app, "bulk-users-120x36")
        await p.press("ctrl+s")
        await p.pause()
        save(app, "bulk-review-120x36")

    for kind, key, size in (("hbacrule", "ssh_access", (80, 24)), ("sudorule", "operations", (120, 36))):
        app = IPAKApp(FakeBackend(delay=0))
        async with app.run_test(size=size) as p:
            await p.pause()
            screen = RuleScreen(kind, key)
            app.push_screen(screen)
            await p.pause()
            save(app, f"{kind}-{size[0]}x{size[1]}")
            if kind == "hbacrule":
                screen.draft.desired.modes["hosts"] = "all"
                screen.draft.desired.members["memberhost_hostgroup"].clear()
                await p.press("ctrl+s")
                save(app, "hbac-all-review-80x24")


if __name__ == "__main__":
    asyncio.run(capture())
