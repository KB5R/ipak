"""Approved property-overview rule editor with local selectors and outer review."""

from __future__ import annotations

from copy import deepcopy

from textual import on, work
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import OptionList, Static

from .app import ContextFooter, TopBar, UserTable
from .backend import BackendError
from .dialogs import ChoiceDialog, DraftDialog, InputDialog, ResultsScreen, ReviewDialog
from .membership import MembershipScreen
from .rules import CATEGORIES, RuleDraft, RuleService, empty_state, validate


class OptionsScreen(Screen):
    BINDINGS = [("a", "add", "Add option"), ("d", "remove", "Remove option"),
                ("ctrl+s", "accept", "Use options"), ("escape", "back", "Back")]

    def __init__(self, options, callback):
        super().__init__()
        self.initial, self.options, self.callback = set(options), set(options), callback

    def compose(self):
        yield TopBar()
        yield Static("Sudo options / Local draft", classes="heading", markup=False)
        yield OptionList(*sorted(self.options), id="sudo-options", markup=False)
        yield Static("Enter sudoers option strings. The server validates them when the outer rule is saved.", markup=False)
        yield ContextFooter()

    def on_mount(self):
        self.query_one(ContextFooter).update("a Add option  d Remove selected option\nCtrl+S Use options (local)  ? Help  Esc Back")

    def render_options(self):
        self.query_one(OptionList).clear_options().add_options(sorted(self.options))

    def action_add(self):
        def add(value):
            if value and value.strip():
                self.options.add(value.strip())
                self.render_options()
        self.app.push_screen(InputDialog("Sudo option (for example: authenticate)"), add)

    def action_remove(self):
        index = self.query_one(OptionList).highlighted
        if index is not None and self.options:
            self.options.discard(sorted(self.options)[index])
            self.render_options()

    def action_accept(self):
        self.app.pop_screen()
        self.callback(set(self.options))

    def action_back(self):
        if self.options == self.initial:
            self.app.pop_screen()
            return
        def decide(choice):
            if choice == "keep":
                self.action_accept()
            elif choice == "discard":
                self.app.pop_screen()
        self.app.push_screen(DraftDialog(), decide)


class RuleScreen(Screen):
    BINDINGS = [("e", "edit", "Edit property"), ("u", "undo", "Undo"),
                ("r", "refresh", "Refresh"), ("ctrl+s", "review", "Review"), ("escape", "back", "Back")]

    def __init__(self, kind, key="", *, on_saved=None):
        super().__init__()
        self.kind, self.key, self.on_saved = kind, key, on_saved
        self.draft_key = ("rule", kind, key)
        self.draft = None
        self.service = None
        self.supported = set()
        self.member_support = {}
        self.history = []
        self.busy = False
        self.request_id = 0

    def compose(self):
        yield TopBar()
        yield Static(f"{'HBAC' if self.kind == 'hbacrule' else 'Sudo'} / {self.key or 'New rule'}", classes="heading", markup=False)
        yield UserTable(id="rule-properties", cursor_type="row", show_row_labels=False)
        yield Static("Loading rule and server capabilities…", id="rule-status", classes="form-status", markup=False)
        yield ContextFooter()

    def on_mount(self):
        self.query_one(UserTable).add_column("PROPERTY", width=27)
        self.query_one(UserTable).add_column("VALUE", width=42)
        self.service = RuleService(self.app.directory)
        self.draft = self.app.drafts.get(self.draft_key)
        self.load()

    @work(group="load-rule", exclusive=True)
    async def load(self, keep=False):
        self.busy = True
        self.request_id += 1
        request = self.request_id
        try:
            self.supported, self.member_support = await self.service.capabilities(self.kind)
            original = await self.service.read(self.kind, self.key) if self.key else empty_state(self.kind)
            if request != self.request_id:
                return
            if self.draft is None:
                self.draft = RuleDraft(self.kind, self.key, original, deepcopy(original), not bool(self.key))
            elif keep:
                self.draft.original = original
            self.render_rows()
            self.query_one(UserTable).focus()
        except BackendError as error:
            self.query_one("#rule-status", Static).update(f"{error} · r Retry")
        finally:
            self.busy = False

    def check_action(self, action, parameters):
        if self.busy:
            return False
        return self.draft is not None if action in {"edit", "undo", "review"} else True

    def render_rows(self):
        if self.draft is None:
            return
        table = self.query_one(UserTable)
        previous = table.current_login
        table.clear()
        state = self.draft.desired
        table.add_row("Name", escape(self.draft.key or "(required)"), key="name")
        table.add_row("Description", escape(state.description or "Not provided"), key="description")
        table.add_row("State", "Enabled" if state.enabled else "Disabled", key="enabled")
        for category in CATEGORIES[self.kind]:
            mode = state.modes[category.key]
            if category.attribute:
                label = "ALL" if mode == "all" else mode.capitalize()
                if category.attribute not in self.supported:
                    label += " (unsupported)"
                table.add_row(f"{category.label} category", label, key=f"mode:{category.key}")
            for member in category.members:
                selected = sorted(state.members[member.attribute])
                value = ", ".join(selected) or "None selected"
                if not self.member_support.get(member.attribute):
                    value += " (unsupported)"
                table.add_row(member.label, escape(value), key=f"members:{member.attribute}")
        if self.kind == "sudorule":
            table.add_row("Sudo options", escape(", ".join(sorted(state.options))) or "None", key="options")
            table.add_row("Order", state.order or "Unconfigured", key="order")
        if previous and previous in table.rows:
            table.move_cursor(row=table.get_row_index(previous), animate=False)
        broad = [category.label for category in CATEGORIES[self.kind] if state.modes[category.key] == "all"]
        status = "Unsaved draft" if self.draft.dirty else "Saved server state"
        if broad:
            status += " · ALL: " + ", ".join(broad)
        self.query_one("#rule-status", Static).update(status)
        self.query_one(ContextFooter).update("↑↓/jk Property  Enter Edit / Open selector  u Undo\nCtrl+S Review entire rule  r Refresh  ? Help  Esc Back")

    @on(UserTable.OpenRequested)
    def open_property(self, event):
        event.stop()
        if not self.busy:
            self.edit_property(event.login)

    def action_edit(self):
        key = self.query_one(UserTable).current_login
        if key:
            self.edit_property(key)

    def remember(self):
        self.history.append((self.draft.key, deepcopy(self.draft.desired)))

    def edit_property(self, key):
        state = self.draft.desired
        if key in {"name", "description", "order"}:
            if key == "name" and not self.draft.is_new:
                self.query_one("#rule-status", Static).update("Existing rule names are read only.")
                return
            if key == "order" and "sudoorder" not in self.supported:
                return
            value = self.draft.key if key == "name" else getattr(state, key)
            def edited(value):
                if value is not None:
                    self.remember()
                    if key == "name":
                        self.draft.key = value.strip()
                    else:
                        setattr(state, key, value.strip())
                    self.render_rows()
            self.app.push_screen(InputDialog(key.capitalize(), value), edited)
        elif key == "enabled":
            def chosen(choice):
                if choice:
                    self.remember()
                    state.enabled = choice == "Enabled"
                    self.render_rows()
            self.app.push_screen(ChoiceDialog("Desired rule state", ("Disabled", "Enabled")), chosen)
        elif key.startswith("mode:"):
            category = next(category for category in CATEGORIES[self.kind] if category.key == key.split(":", 1)[1])
            if category.attribute not in self.supported:
                return
            def chosen(choice):
                if choice:
                    self.remember()
                    state.modes[category.key] = choice.lower()
                    if choice != "Selected":
                        for member in category.members:
                            state.members[member.attribute].clear()
                    self.render_rows()
            self.app.push_screen(ChoiceDialog(f"{category.label}: choose scope explicitly", ("Unconfigured", "Selected", "ALL")), chosen)
        elif key.startswith("members:"):
            attribute = key.split(":", 1)[1]
            category = next(category for category in CATEGORIES[self.kind] if any(member.attribute == attribute for member in category.members))
            member = next(member for member in category.members if member.attribute == attribute)
            if not self.member_support.get(attribute):
                return
            if state.modes[category.key] == "all":
                self.query_one("#rule-status", Static).update("Change this category from ALL to Selected before choosing members.")
                return
            def chosen(selected):
                self.remember()
                state.members[attribute] = set(selected)
                state.modes[category.key] = "selected" if any(state.members[item.attribute] for item in category.members) else "unconfigured"
                self.render_rows()
            self.app.push_screen(MembershipScreen("rule-draft", f"{self.draft.key or 'New rule'} / {member.label}",
                                                 draft_target=member.kind, initial=state.members[attribute], on_accept=chosen))
        elif key == "options":
            def chosen(options):
                self.remember()
                state.options = options
                self.render_rows()
            self.app.push_screen(OptionsScreen(state.options, chosen))

    def action_undo(self):
        if self.history:
            self.draft.key, self.draft.desired = self.history.pop()
            self.render_rows()

    def action_review(self):
        errors = validate(self.draft)
        if errors:
            self.query_one("#rule-status", Static).update("; ".join(errors))
            return
        if not self.draft.dirty:
            self.query_one("#rule-status", Static).update("No changes to save.")
            return
        state = self.draft.desired
        lines = [f"Rule: {self.draft.key}", "A new rule is created empty and immediately disabled before configuration."
                 if self.draft.is_new else "An enabled rule is disabled during changes. Failures leave it disabled.",
                 f"Final state: {'ENABLED' if state.enabled else 'Disabled'}", f"Description: {state.description}"]
        broad = [category.label for category in CATEGORIES[self.kind] if state.modes[category.key] == "all"]
        if broad:
            lines.append("ALL ACCESS — explicitly selected scopes: " + ", ".join(broad))
        for category in CATEGORIES[self.kind]:
            lines.append(f"\n{category.label}: {state.modes[category.key].upper()}")
            for member in category.members:
                before = self.draft.original.members[member.attribute]
                after = state.members[member.attribute]
                lines.append(f"{member.label}: {', '.join(sorted(after)) or 'None'}")
                lines.extend(f"  Remove {member.label}: {target}" for target in sorted(before - after))
        if self.kind == "sudorule":
            lines.extend((f"Order: {state.order or 'Unconfigured'}", "Sudo options: " + (", ".join(sorted(state.options)) or "None")))
        self.app.push_screen(ReviewDialog("Review entire rule", lines), lambda apply: self.save() if apply else None)

    @work(group="rule-write", exclusive=True)
    async def save(self):
        self.busy = True
        try:
            self.query_one("#rule-status", Static).update("Applying reviewed rule changes…")
            results = await self.service.apply(self.draft)
            if not self.draft.is_new:
                self.key = self.draft.key
            self.history.clear()
            if all(result.success for result in results):
                self.app.drafts.pop(self.draft_key, None)
            self.render_rows()
            self.app.push_screen(ResultsScreen("Rule results", results, on_close=self.render_rows))
        finally:
            self.busy = False

    def action_refresh(self):
        if self.draft and self.draft.dirty and not self.draft.is_new:
            self.app.push_screen(ReviewDialog("Refresh baseline", ["Read the current server state and retain your desired changes for another review."], apply_label="Refresh"),
                                 lambda apply: self.load(keep=True) if apply else None)
        else:
            self.draft = None
            self.load()

    def action_back(self):
        def leave(choice):
            if choice == "stay":
                return
            if choice == "keep":
                self.app.drafts[self.draft_key] = self.draft
            else:
                self.app.drafts.pop(self.draft_key, None)
            self.request_id += 1
            self.app.pop_screen()
            if self.on_saved:
                self.on_saved()
        if self.draft and self.draft.dirty:
            self.app.push_screen(DraftDialog(), leave)
        else:
            leave("discard")
