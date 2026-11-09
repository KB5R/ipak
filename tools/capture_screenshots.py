"""Export real Textual frames as SVG; run from the repository root.

No snapshot plugin, browser, image library, or other dev dependency is required.
"""

import asyncio
from pathlib import Path

from ipak.app import IPAKApp
from ipak.backend import FakeBackend


async def capture() -> None:
    output = Path("docs/screenshots")
    output.mkdir(parents=True, exist_ok=True)

    def save(app, name):
        path = output / f"{name}.svg"
        path.write_text(app.export_screenshot(title=f"IPAK M1 · {name}"), encoding="utf-8")
        print(path)

    app = IPAKApp(FakeBackend(delay=0))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        save(app, "users-80x24")
        await pilot.press("space", "down", "space", "slash", *"petrov")
        save(app, "filter-selection-80x24")
        await pilot.press("escape", "home", "up", "up", "enter")
        await pilot.pause()
        save(app, "user-detail-80x24")
        await pilot.press("escape", "c")
        await pilot.resize_terminal(120, 36)
        await pilot.pause()
        save(app, "users-120x36")

    for scenario in ("empty", "connection-error", "slow"):
        app = IPAKApp(FakeBackend(scenario, delay=0))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            save(app, f"{scenario}-80x24")


if __name__ == "__main__":
    asyncio.run(capture())
