"""Shared membership editor for users, hosts, user groups and host groups."""

from __future__ import annotations

from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import Input, Static

from .app import ContextFooter, TopBar, UserTable
from .backend import BackendError
from .dialogs import DraftDialog, ResultsScreen, ReviewDialog


class MembershipScreen(Screen):
    BINDINGS = [Binding("tab", "switch_pane", "Pane", priority=True),
                Binding("shift+tab", "switch_pane", "Pane", priority=True),
                ("slash", "search", "Filter"), ("space", "mark", "Mark"),
                ("a", "add", "Add"), ("d", "remove", "Remove"), ("u", "undo", "Undo"),
                ("r", "reload", "Retry"), ("ctrl+s", "review", "Review"), ("escape", "back", "Back")]

    def __init__(self, kind, owner, *, on_saved=None, draft_target=None, initial=None, on_accept=None, locked=None):
        super().__init__()
        self.kind, self.owner, self.on_saved = kind, owner, on_saved
        self.draft_key = ("members", kind, owner)
        self.base, self.indirect, self.added, self.removed = set(), set(), set(), set()
        self.marks = {"members": set(), "available": set()}
        self.available = ()
        self.active = "members"
        self.target_kind = ""
        self.undo = []
        self.busy = False
        self.ready = False
        self.request_id = 0
        self.timer = None
        self.draft_target, self.initial, self.on_accept = draft_target, set(initial or ()), on_accept
        self.locked = set(locked or ())

    def compose(self):
        yield TopBar()
        yield Static(f"{self.owner} / Members", classes="heading", markup=False)
        with Horizontal(id="membership-panes"):
            for pane, title in (("members", "Members (direct / indirect)"), ("available", "Available (server search)")):
                with Vertical(id=f"pane-{pane}", classes="membership-pane"):
                    yield Static(title, classes="heading", markup=False)
                    yield Input(placeholder="Local filter…" if pane == "members" else "Search server…",
                                id=f"search-{pane}", compact=True)
                    yield UserTable(id=f"table-{pane}", cursor_type="row", show_row_labels=False)
        yield Static("Loading memberships…", id="membership-status", classes="form-status", markup=False)
        yield Static("", id="pending", markup=False)
        yield ContextFooter()

    def on_mount(self):
        for pane in self.marks:
            table = self.query_one(f"#table-{pane}", UserTable)
            table.add_column("", width=3)
            table.add_column("NAME", width=26)
            table.add_column("MEMBERSHIP", width=20)
            self.query_one(f"#search-{pane}").display = False
        draft = self.app.drafts.get(self.draft_key, {})
        self.added = set(draft.get("added", ()))
        self.removed = set(draft.get("removed", ()))
        self.adapt_layout()
        self.load_members()

    def on_resize(self):
        if self.is_mounted:
            self.adapt_layout()

    def adapt_layout(self):
        for pane in self.marks:
            self.query_one(f"#pane-{pane}").display = self.size.width >= 120 or pane == self.active

    def check_action(self, action, parameters):
        if self.busy:
            return False
        if action in {"mark", "add", "remove", "undo", "search", "reload"} and isinstance(self.focused, Input):
            return False
        if action in {"mark", "add", "remove", "review", "search"} and not self.ready:
            return False
        if action == "add":
            return self.active == "available"
        if action == "remove":
            return self.active == "members"
        if action == "review":
            return bool(self.added or self.removed) or bool(self.draft_target)
        return True

    @work(group="membership-current", exclusive=True)
    async def load_members(self):
        self.ready = False
        try:
            if self.draft_target:
                self.target_kind, self.base, self.indirect = self.draft_target, set(self.initial), set()
            else:
                self.target_kind, self.base, self.indirect = await self.app.directory.membership(self.kind, self.owner)
        except BackendError as error:
            self.query_one("#membership-status", Static).update(f"{error} · r Retry")
            return
        self.added.difference_update(self.base)
        self.removed.intersection_update(self.base)
        self.ready = True
        self.render_members()
        self.query_one(f"#table-{self.active}").focus()
        self.request_id += 1
        self.load_available(self.request_id, self.query_one("#search-available", Input).value)

    @work(group="membership-search", exclusive=True)
    async def load_available(self, request, query):
        try:
            result = await self.app.directory.find(self.target_kind, query)
        except BackendError as error:
            if request == self.request_id:
                self.query_one("#membership-status", Static).update(f"{error} · r Retry")
            return
        if request != self.request_id:
            return
        self.available = result.records
        self.query_one("#membership-status", Static).update(
            f"{len(result.records)} available records loaded" +
            ("; more may exist — / searches server" if result.truncated else "") +
            ". Indirect memberships are read only.")
        self.render_members()

    @on(Input.Changed)
    def search_changed(self, event):
        if event.input.id == "search-members":
            self.render_members()
        elif self.target_kind:
            self.request_id += 1
            if self.timer:
                self.timer.stop()
            request = self.request_id
            self.timer = self.set_timer(0.3, lambda: self.load_available(request, event.value))

    @on(Input.Submitted)
    def results_focus(self):
        self.query_one(f"#table-{self.active}").focus()

    def on_descendant_focus(self, event):
        if not self.is_mounted or self.focused is None:
            return
        pane = "available" if (self.focused.id or "").endswith("available") else "members"
        self.active = pane
        self.update_footer()

    def render_members(self):
        if not self.is_mounted:
            return
        query = self.query_one("#search-members", Input).value.casefold()
        names = sorted(self.base | self.indirect | self.added)
        rows = {"members": [(name, "Pending add" if name in self.added else "Pending removal" if name in self.removed
                              else "Direct" if name in self.base else "Indirect (read only)")
                             for name in names if query in name.casefold()],
                "available": [(item.key, "Available") for item in self.available
                              if item.key not in (self.base | self.added) - self.removed]}
        for pane, items in rows.items():
            table = self.query_one(f"#table-{pane}", UserTable)
            current = table.current_login
            table.clear()
            for name, state in items:
                marker = "[-]" if pane == "members" and name in self.indirect - self.base - self.added else "[x]" if name in self.marks[pane] else "[ ]"
                table.add_row(escape(marker), escape(name), state, key=name)
            keys = [name for name, _ in items]
            if current in keys:
                table.move_cursor(row=keys.index(current), animate=False)
        self.update_footer()

    def update_footer(self):
        changes = [f"+{name}" for name in sorted(self.added)] + [f"-{name}" for name in sorted(self.removed)]
        preview = " ".join(changes[:4]) + (f" … ({len(changes)} changes)" if len(changes) > 4 else "")
        self.query_one("#pending", Static).update(f"{len(self.marks[self.active])} marked in {self.active} | Pending: " + (preview or "No changes"))
        if isinstance(self.focused, Input):
            text = "Type search  Enter Results  Tab Pane  Esc Close search"
        else:
            staging = "a Stage add" if self.active == "available" else "d Stage removal"
            save = "Use selection (local)" if self.draft_target else "Review"
            text = f"Tab Pane  ↑↓/jk Move  Space Mark  / Search  {staging}\nu Undo  Ctrl+S {save}  ? Help  Esc Back"
        self.query_one(ContextFooter).update(text)

    def action_switch_pane(self):
        self.active = "available" if self.active == "members" else "members"
        self.adapt_layout()
        self.query_one(f"#table-{self.active}").focus()

    def action_search(self):
        field = self.query_one(f"#search-{self.active}", Input)
        field.display = True
        field.focus()

    def action_mark(self):
        key = self.query_one(f"#table-{self.active}", UserTable).current_login
        if not key:
            return
        if self.active == "members" and key in self.indirect - self.base - self.added:
            self.query_one("#membership-status", Static).update("Indirect membership is read only.")
            return
        marks = self.marks[self.active]
        marks.remove(key) if key in marks else marks.add(key)
        self.render_members()

    def targets(self):
        cursor = self.query_one(f"#table-{self.active}", UserTable).current_login
        return set(self.marks[self.active]) or ({cursor} if cursor else set())

    def action_add(self):
        targets = self.targets()
        self.undo.append((set(self.added), set(self.removed)))
        for key in targets:
            self.removed.discard(key)
            if key not in self.base:
                self.added.add(key)
        self.marks["available"].difference_update(targets)
        self.render_members()

    def action_remove(self):
        targets = self.targets()
        self.undo.append((set(self.added), set(self.removed)))
        for key in targets:
            if key in self.locked:
                self.query_one("#membership-status", Static).update("Already applied memberships cannot be removed from this creation draft.")
                continue
            self.added.discard(key)
            if key in self.base:
                self.removed.add(key)
        self.marks["members"].difference_update(targets)
        self.render_members()

    def action_undo(self):
        if self.undo:
            self.added, self.removed = self.undo.pop()
            self.render_members()

    def action_review(self):
        if self.draft_target:
            selected = (self.base | self.added) - self.removed
            self.app.pop_screen()
            if self.on_accept:
                self.on_accept(selected)
            return
        added, removed = tuple(sorted(self.added)), tuple(sorted(self.removed))
        lines = [f"{self.kind}: {self.owner}", *[f"Add: {key}" for key in added], *[f"Remove direct member: {key}" for key in removed]]
        self.app.push_screen(ReviewDialog("Review membership changes", lines),
                             lambda apply: self.apply_changes(added, removed) if apply else None)

    @work(group="membership-write", exclusive=True)
    async def apply_changes(self, added, removed):
        self.busy = True
        results = []
        try:
            for adding, targets in ((True, added), (False, removed)):
                for target in targets:
                    result = await self.app.directory.change_member(self.kind, self.owner, target, adding)
                    results.append(result)
                    if result.success:
                        if adding:
                            self.base.add(target)
                            self.added.discard(target)
                        else:
                            self.base.discard(target)
                            self.removed.discard(target)
                    self.render_members()
            self.undo.clear()
            self.app.drafts.pop(self.draft_key, None)
            self.app.push_screen(ResultsScreen("Membership results", results))
        finally:
            self.busy = False

    def action_reload(self):
        self.load_members()

    def action_back(self):
        field = self.query_one(f"#search-{self.active}", Input)
        if field.display:
            field.value = ""
            field.display = False
            self.query_one(f"#table-{self.active}").focus()
            return
        def leave(choice):
            if choice == "stay":
                return
            if choice == "keep":
                if not self.draft_target:
                    self.app.drafts[self.draft_key] = {"added": set(self.added), "removed": set(self.removed)}
            else:
                self.app.drafts.pop(self.draft_key, None)
            self.request_id += 1
            self.app.pop_screen()
            if self.draft_target and choice == "keep" and self.on_accept:
                self.on_accept((self.base | self.added) - self.removed)
            if self.on_saved:
                self.on_saved()
        if self.added or self.removed:
            self.app.push_screen(DraftDialog(), leave)
        else:
            leave("discard")
