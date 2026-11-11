"""Single-list rule field selection; category modes never become member names."""

from __future__ import annotations

from copy import deepcopy

from textual import on, work
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import Input, Static

from .app import ContextFooter, TopBar, UserTable
from .backend import BackendError
from .dialogs import ReviewDialog


PRIMARY_FIELDS = {
    "users": "memberuser_user", "hosts": "memberhost_host",
    "services": "memberservice_hbacsvc", "commands": "memberallowcmd_sudocmd",
    "deny": "memberdenycmd_sudocmd", "runas_users": "ipasudorunas_user",
    "runas_groups": "ipasudorunasgroup_group",
}


def field_label(member):
    return {"memberuser_user": "Users", "memberhost_host": "Hosts"}.get(member.attribute, member.label)


def ordered_fields(category):
    return sorted(category.members, key=lambda member: member.attribute != PRIMARY_FIELDS[category.key])


class RuleFieldSelector(Screen):
    DEFAULT_CSS = """
    RuleFieldSelector #rule-choices { height: 1fr; }
    RuleFieldSelector #rule-search { height: 1; border: none; }
    RuleFieldSelector #rule-choice-status { height: auto; max-height: 4; padding: 0 1; }
    """
    BINDINGS = [("space", "mark", "Mark"), ("slash", "search", "Search"),
                ("u", "undo", "Undo"), ("r", "retry", "Retry"), ("escape", "cancel", "Cancel")]

    def __init__(self, category, member, state, *, allow_all, on_accept, editable=True, category_editable=True):
        super().__init__()
        self.category, self.member = category, member
        self.mode = state.modes[category.key]
        self.members = {item.attribute: set(state.members[item.attribute]) for item in category.members}
        self.initial = (self.mode, deepcopy(self.members))
        self.allow_all, self.on_accept, self.editable = allow_all, on_accept, editable
        self.category_editable = category_editable
        self.available = ()
        self.request_id = 0
        self.timer = None
        self.search_query = ""
        self.load_message = "Loading…"
        self.history = []

    def compose(self):
        yield TopBar()
        yield Static(f"Edit / {field_label(self.member)}", classes="heading", markup=False)
        yield Input(placeholder="Search server…", id="rule-search", compact=True)
        yield UserTable(id="rule-choices", cursor_type="row", show_row_labels=False)
        yield Static("", id="rule-choice-status", markup=False)
        yield ContextFooter()

    def on_mount(self):
        self.query_one(Input).display = False
        table = self.query_one(UserTable)
        table.add_column("", width=3)
        table.add_column(field_label(self.member).upper(), width=60)
        table.focus()
        self.render_choices()
        self.start_search("")

    def check_action(self, action, parameters):
        return not isinstance(self.focused, Input) if action in {"mark", "search", "undo", "retry"} else True

    def start_search(self, query):
        self.search_query = query
        self.request_id += 1
        self.available = ()
        self.load_message = "Loading server matches…"
        self.render_choices()
        self.load(self.request_id, query)

    @work(group="rule-field-search", exclusive=True)
    async def load(self, request, query):
        try:
            result = await self.app.directory.find(self.member.kind, query)
        except BackendError as error:
            if request == self.request_id and self.is_mounted:
                self.load_message = f"{error} · r Retry"
                self.render_choices()
            return
        if request != self.request_id or not self.is_mounted:
            return
        self.available = result.records
        self.load_message = f"{len(result.records)} server matches" if result.records else "No server matches"
        if result.truncated:
            self.load_message += "; more may exist — / Search server"
        self.render_choices()

    @on(Input.Changed)
    def search_changed(self, event):
        self.request_id += 1
        if self.timer:
            self.timer.stop()
        self.search_query = event.value
        self.available = ()
        self.load_message = "Waiting for server search…"
        self.render_choices()
        self.timer = self.set_timer(0.3, lambda: self.start_search(event.value))

    @on(Input.Submitted)
    def focus_results(self):
        self.query_one(UserTable).focus()

    def render_choices(self):
        table = self.query_one(UserTable)
        previous = table.current_login
        table.clear()
        if self.allow_all:
            table.add_row(escape("[x]" if self.mode == "all" else "[ ]"), "ALL", key="mode:all")
        selected = self.members[self.member.attribute]
        names = {item.key for item in self.available} | {name for name in selected if self.search_query.casefold() in name.casefold()}
        for name in sorted(names):
            table.add_row(escape("[x]" if name in selected else "[ ]"), escape(name), key=f"member:{name}")
        if previous in table.rows:
            table.move_cursor(row=table.get_row_index(previous), animate=False)
        hidden = len(selected - names)
        scope = f"ALL {self.category.label} active" if self.mode == "all" else f"{len(selected)} selected ({hidden} hidden)"
        self.query_one("#rule-choice-status", Static).update(f"{scope} · {self.load_message}")
        self.query_one(ContextFooter).update("/ Search  ↑↓/jk Move  Space Mark  u Undo  r Retry\nEnter Apply to draft  Esc Cancel (no rule changes)")

    def remember(self):
        self.history.append((self.mode, deepcopy(self.members)))

    def action_mark(self):
        key = self.query_one(UserTable).current_login
        if key == "mode:all":
            if self.mode == "all":
                self.remember()
                self.mode = "unconfigured"
                self.render_choices()
                return
            removals = [f"{field_label(member)}: {name}" for member in self.category.members
                        for name in sorted(self.members[member.attribute])]
            def choose(confirmed):
                if confirmed:
                    self.remember()
                    self.mode = "all"
                    for selected in self.members.values():
                        selected.clear()
                    self.render_choices()
            if removals:
                self.app.push_screen(ReviewDialog(f"Use ALL {self.category.label}",
                                     ["These explicit selections will be removed from the draft:", *removals,
                                      "The server is unchanged until rule-level Review and Apply."], apply_label="Use ALL"), choose)
            else:
                choose(True)
            return
        if not key or not key.startswith("member:"):
            return
        if not self.editable:
            self.query_one("#rule-choice-status", Static).update("Server does not support editing this member field.")
            return
        if self.mode == "all" and not self.category_editable:
            self.query_one("#rule-choice-status", Static).update("Server does not support changing this ALL setting.")
            return
        name = key[len("member:"):]
        def choose(confirmed):
            if confirmed:
                self.remember()
                selected = self.members[self.member.attribute]
                selected.remove(name) if name in selected else selected.add(name)
                self.mode = "selected" if any(self.members.values()) else "unconfigured"
                self.render_choices()
        if self.mode == "all":
            self.app.push_screen(ReviewDialog(f"Replace ALL {self.category.label}",
                                 [f"Turn off ALL {self.category.label} in the draft.",
                                  f"Select {field_label(self.member)}: {name}",
                                  "Other fields in this category remain separately editable."], apply_label="Use selected"), choose)
        else:
            choose(True)

    @on(UserTable.OpenRequested)
    def accept(self, event):
        event.stop()
        self.apply_selection()

    # Enter must also work when the available set is empty.
    def on_key(self, event):
        if event.key == "enter" and self.focused is self.query_one(UserTable) and not self.query_one(UserTable).row_count:
            event.prevent_default()
            event.stop()
            self.apply_selection()

    def apply_selection(self):
        self.request_id += 1
        if self.timer:
            self.timer.stop()
        self.app.pop_screen()
        if (self.mode, self.members) != self.initial:
            self.on_accept(self.mode, deepcopy(self.members))

    def on_unmount(self):
        self.request_id += 1
        if self.timer:
            self.timer.stop()

    def action_search(self):
        self.query_one(Input).display = True
        self.query_one(Input).focus()

    def action_retry(self):
        self.start_search(self.search_query)

    def action_undo(self):
        if self.history:
            self.mode, self.members = self.history.pop()
            self.render_choices()

    def action_cancel(self):
        if isinstance(self.focused, Input):
            self.query_one(Input).value = ""
            self.query_one(Input).display = False
            self.query_one(UserTable).focus()
            return
        self.request_id += 1
        if self.timer:
            self.timer.stop()
        self.app.pop_screen()
