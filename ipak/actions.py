"""User actions and bounded, explicit server-backed pickers."""

from __future__ import annotations

from textual import on, work
from textual.containers import Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from .app import UserTable
from .backend import BackendError
from .dialogs import ResultsScreen, ReviewDialog
from .directory import LABELS, WriteResult


class PickerDialog(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, kind: str, title: str = ""):
        super().__init__()
        self.kind, self.title_text = kind, title or f"Choose {LABELS[kind]}"
        self.request_id = 0
        self.ready = False
        self.timer = None

    def compose(self):
        with Vertical(classes="dialog picker-dialog"):
            yield Static(self.title_text, classes="heading", markup=False)
            yield Input(placeholder="Search server…", id="picker-search", compact=True)
            yield Static("Loading…", id="picker-status", markup=False)
            yield UserTable(id="picker-table", cursor_type="row", show_row_labels=False)
            yield Static("Tab Focus · Enter Choose · Esc Cancel", markup=False)

    def on_mount(self):
        self.query_one(UserTable).add_column("NAME", width=24)
        self.query_one(UserTable).add_column("DESCRIPTION", width=32)
        self.load(self.request_id, "")

    @on(Input.Changed, "#picker-search")
    def search_changed(self, event):
        self.request_id += 1
        self.ready = False
        if self.timer:
            self.timer.stop()
        request = self.request_id
        self.timer = self.set_timer(0.3, lambda: self.load(request, event.value))

    @work(group="picker-search", exclusive=True)
    async def load(self, request, query):
        self.query_one("#picker-status", Static).update("Loading server matches…")
        try:
            result = await self.app.directory.find(self.kind, query)
        except BackendError as error:
            if request == self.request_id:
                self.query_one("#picker-status", Static).update(str(error) + " · Edit search to retry.")
            return
        if request != self.request_id or not self.is_mounted:
            return
        table = self.query_one(UserTable)
        table.clear()
        for item in result.records:
            table.add_row(escape(item.key), escape(item.description), key=item.key)
        self.ready = True
        self.query_one("#picker-status", Static).update(
            f"{len(result.records)} loaded" + ("; more may exist — refine server search" if result.truncated else "")
            if result.records else "No results")

    @on(Input.Submitted)
    def focus_results(self):
        self.query_one(UserTable).focus()

    @on(UserTable.OpenRequested)
    def choose(self, event):
        event.stop()
        if self.ready:
            self.request_id += 1
            self.dismiss(event.login)

    def action_cancel(self):
        self.request_id += 1
        self.dismiss(None)


class ActionResultsScreen(ResultsScreen):
    def __init__(self, title, targets, operation, *, on_close=None):
        super().__init__(title, [], retry=operation, on_close=on_close)
        self.targets, self.operation = tuple(targets), operation

    def on_mount(self):
        super().on_mount()
        self.busy = True
        self.apply_targets()

    @work(group="reviewed-actions", exclusive=True)
    async def apply_targets(self):
        self.busy = True
        try:
            for index, target in enumerate(self.targets):
                self.query_one("#shortcuts", Static).update(f"Applying {index + 1}/{len(self.targets)}…\nChanges already completed will not be retried.")
                try:
                    result = await self.operation(target)
                except BackendError as error:
                    result = WriteResult(target, False, str(error))
                self.results.append(result)
                self.render_results()
        finally:
            self.busy = False
            self.render_results()


def user_action(screen, action: str, targets, on_close):
    targets = tuple(sorted(set(targets)))
    if not targets:
        return
    titles = {"enable": "Enable users", "disable": "Disable users", "reset": "Reset passwords", "group": "Add to group"}
    # The callback below is also used after the asynchronous group selection.
    # It captures immutable targets, never the later cursor position or marks.
    def review(group):
        lines = [f"{len(targets)} selected", *targets]
        if group:
            lines.append(f"Group: {group}")
        if action == "reset":
            lines.append("Generate a different temporary password for every user. Require password change at login.")
        operation = (lambda target: screen.app.directory.change_member("user", target, group, True)) if group else (
            lambda target: screen.app.directory.user_action(action, target))
        def confirmed(apply):
            if apply:
                screen.app.push_screen(ActionResultsScreen(titles[action], targets, operation, on_close=on_close))
        screen.app.push_screen(ReviewDialog(titles[action], lines), confirmed)

    if action == "group":
        screen.app.push_screen(PickerDialog("group", "Add selected users to group"),
                               lambda group: review(group) if group else None)
    else:
        review(None)
