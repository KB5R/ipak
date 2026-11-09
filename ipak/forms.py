"""Compact object forms: stage locally, review the exact diff, then apply."""

from __future__ import annotations

import ipaddress

from textual import work
from textual.screen import Screen
from textual.widgets import Input, Static

from .app import ContextFooter, TopBar
from .dialogs import DraftDialog, ResultsScreen, ReviewDialog, ScrollBody


class ObjectForm(Screen):
    BINDINGS = [("ctrl+s", "review", "Review"), ("escape", "back", "Back")]

    def __init__(self, kind: str, key: str = "", initial=None, *, on_saved=None):
        super().__init__()
        self.kind, self.key = kind, key
        self.initial = dict(initial or {})
        self.on_saved = on_saved
        self.busy = False
        self.draft_key = ("form", kind, key)
        self.fields = []
        if not key:
            self.fields.append(("key", "Login" if kind == "user" else "FQDN" if kind == "host" else "Name", True))
        if kind == "user":
            self.fields.extend((("givenname", "Given name", not bool(key)), ("sn", "Surname", True),
                                ("cn", "Full name", False), ("mail", "Email (comma separated)", False),
                                ("carlicense", "Comment", False)))
        else:
            self.fields.append(("description", "Description", False))
            if kind == "host" and not key:
                self.fields.append(("ip_address", "IP address (optional)", False))

    def compose(self):
        yield TopBar()
        yield Static(f"{'Edit' if self.key else 'Create'} {self.kind} {self.key}", classes="heading", markup=False)
        draft = self.app.drafts.get(self.draft_key, self.initial)
        with ScrollBody(classes="form-body"):
            for name, label, required in self.fields:
                yield Static(label + (" *" if required else ""), classes="form-label", markup=False)
                yield Input(str(draft.get(name, "")), id=f"field-{name}", classes="form-input", compact=True)
            if self.kind == "user" and not self.key:
                yield Static("A unique temporary password will be generated.\nThe result screen reveals it only on request.", classes="form-label", markup=False)
        yield Static("", id="form-status", classes="form-status", markup=False)
        yield ContextFooter()

    def on_mount(self):
        self.query(Input).first().focus()
        self.query_one(ContextFooter).update("Tab / Shift+Tab Field  Ctrl+S Review\nEsc Back (unsaved changes are kept or discarded explicitly)")

    def field_values(self):
        return {name: self.query_one(f"#field-{name}", Input).value.strip() for name, _, _ in self.fields}

    def check_action(self, action, parameters):
        return not self.busy

    def action_review(self):
        data = self.field_values()
        for name, label, required in self.fields:
            if required and not data[name]:
                self.query_one("#form-status", Static).update(f"{label} is required.")
                self.query_one(f"#field-{name}").focus()
                return
        if data.get("ip_address"):
            try:
                ipaddress.ip_address(data["ip_address"])
            except ValueError:
                self.query_one("#form-status", Static).update("Invalid IP address.")
                return
        key = self.key or data.pop("key")
        changes = {name: value for name, value in data.items() if (value != str(self.initial.get(name, "")) if self.key else bool(value))}
        if self.kind == "user" and not self.key and not changes.get("cn"):
            changes["cn"] = f"{data['givenname']} {data['sn']}"
        if self.key and not changes:
            self.query_one("#form-status", Static).update("No changes.")
            return
        lines = [f"Target: {self.kind} / {key}"]
        lines.extend(f"{name}: {self.initial.get(name, '(empty)')} → {value or '(clear)'}" for name, value in changes.items())
        if self.kind == "user" and not self.key:
            lines.append("Password: unique temporary password; change required at first login.")
        self.app.push_screen(ReviewDialog("Review changes", lines),
                             lambda apply: self.save(key, changes) if apply else None)

    @work(group="save-form", exclusive=True)
    async def save(self, key, changes):
        self.busy = True
        self.query_one("#form-status", Static).update("Applying reviewed changes…")
        try:
            options = {name: value or None for name, value in changes.items()}
            if "mail" in options and options["mail"]:
                options["mail"] = [item.strip() for item in options["mail"].split(",") if item.strip()]
            service = self.app.directory
            result = await (service.modify(self.kind, key, options) if self.key else service.create(self.kind, key, options))
            if not result.success:
                self.query_one("#form-status", Static).update(result.message)
                return
            self.app.drafts.pop(self.draft_key, None)
            self.app.pop_screen()
            self.app.push_screen(ResultsScreen("Result", [result], on_close=self.on_saved))
        finally:
            self.busy = False

    def action_back(self):
        current = self.field_values()
        dirty = any(value != str(self.initial.get(name, "")) for name, value in current.items())
        if dirty:
            def decide(choice):
                if choice == "stay":
                    return
                if choice == "keep":
                    self.app.drafts[self.draft_key] = current
                else:
                    self.app.drafts.pop(self.draft_key, None)
                self.app.pop_screen()
            self.app.push_screen(DraftDialog(), decide)
        else:
            self.app.drafts.pop(self.draft_key, None)
            self.app.pop_screen()
