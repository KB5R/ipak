"""Capture approved rule editors and selectors using demo data only."""

import asyncio
from pathlib import Path

from ipak.app import IPAKApp, UserTable
from ipak.backend import FakeBackend
from ipak.rule_ui import RuleScreen


async def capture():
    output = Path("docs/screenshots/rule-ux/after")
    output.mkdir(parents=True, exist_ok=True)

    def save(app, name):
        path = output / (name + ".svg")
        path.write_text(app.export_screenshot(title="IPAK · " + name), encoding="utf-8")
        print(path)

    for size in ((80, 24), (120, 36)):
        for kind, key in (("hbacrule", "ssh_access"), ("sudorule", "operations")):
            backend = FakeBackend(delay=0)
            if kind == "hbacrule":
                backend.api.records[kind][key].update(usercategory=("all",), hostcategory=("all",),
                                                     memberuser_group=(), memberhost_hostgroup=())
            else:
                backend.api.records[kind][key].update(memberuser_user=("mpopkov",),
                                                     memberhost_host=("web.example.test",),
                                                     memberhost_hostgroup=("webservers",))
            app = IPAKApp(backend)
            async with app.run_test(size=size) as p:
                await p.pause()
                screen = RuleScreen(kind, key)
                app.push_screen(screen)
                await p.pause()
                save(app, f"{kind}-{size[0]}x{size[1]}")
                if size == (80, 24):
                    screen.edit_property("members:memberuser_user")
                    await p.pause()
                    save(app, kind + "-users-selector-80x24")
                    if kind == "sudorule":
                        table = app.screen.query_one(UserTable)
                        table.move_cursor(row=table.get_row_index("mode:all"))
                        await p.press("space")
                        save(app, "sudo-all-conflict-80x24")


if __name__ == "__main__":
    asyncio.run(capture())
