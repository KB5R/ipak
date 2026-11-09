"""Shared list/detail presentation for Hosts, Groups, HBAC and Sudo."""

from __future__ import annotations

from textual import on, work
from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from .app import ContextFooter, Sidebar, TopBar, UserTable
from .backend import BackendError
from .dialogs import InputDialog, ReviewDialog, ScrollBody
from .directory import LABELS


class DirectoryPane(Vertical):
    BINDINGS = [("slash", "filter", "Filter"), ("f2", "server_search", "Server search"),
                ("a", "create", "Add"), ("e", "edit", "Edit"), ("d", "delete", "Delete"),
                ("r", "refresh", "Refresh"), ("escape", "back", "Back"),
                ("1", "group_type('group')", "User Groups"), ("2", "group_type('hostgroup')", "Host Groups")]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.kind = "host"
        self.records = ()
        self.server_query = ""
        self.ready = False
        self.request_id = 0
        self.load_state = "idle"

    def compose(self):
        with Horizontal(id="group-types"):
            yield Button("1 User Groups", id="user-groups")
            yield Button("2 Host Groups", id="host-groups")
        yield Static("", id="directory-title", classes="heading", markup=False)
        yield Input(placeholder="Filter loaded records…", id="directory-filter", compact=True)
        yield Static("", id="directory-status", markup=False)
        yield UserTable(id="directory-table", cursor_type="row", show_row_labels=False)

    def on_mount(self):
        self.query_one("#group-types").display = False
        self.query_one(Input).display = False
        self.query_one(UserTable).add_column("NAME", width=28)
        self.query_one(UserTable).add_column("DESCRIPTION", width=23)

    def open_section(self, section):
        self.kind = {"Hosts": "host", "Groups": "group", "HBAC": "hbacrule", "Sudo": "sudorule"}[section]
        self.query_one("#group-types").display = section == "Groups"
        self.server_query = ""
        self.query_one(Input).value = ""
        self.query_one(Input).display = False
        self.reload()
        self.query_one(UserTable).focus()

    def check_action(self, action, parameters):
        if isinstance(self.app.focused, Input) and action not in {"back", "server_search"}:
            return False
        if action == "group_type":
            return self.screen.section == "Groups"
        if action == "refresh":
            return self.load_state != "loading"
        if action in {"edit", "delete"}:
            if not self.ready or not self.query_one(UserTable).current_login:
                return False
            if action == "delete":
                return self.kind in {"host", "group", "hostgroup"}
        return True

    def reload(self):
        self.request_id += 1
        self.ready = False
        self.load_state = "loading"
        self.query_one("#directory-title", Static).update(f"{LABELS[self.kind]} — Loading…")
        self.query_one("#directory-status", Static).update("Loading…")
        self.load(self.request_id, self.kind, self.server_query)

    @work(group="directory-list", exclusive=True)
    async def load(self, request, kind, query):
        try:
            result = await self.app.directory.find(kind, query)
        except BackendError as error:
            if request == self.request_id:
                self.load_state = "error"
                self.query_one("#directory-status", Static).update(f"{error} · r Retry")
                self.query_one("#directory-title", Static).update(f"{LABELS[kind]} — Load failed")
                self.update_footer()
            return
        if request != self.request_id:
            return
        self.records = result.records
        self.ready = True
        self.load_state = "ready"
        self.query_one("#directory-title", Static).update(f"{LABELS[kind]} ({len(result.records)} loaded{'; more may exist' if result.truncated else ''})")
        self.query_one("#directory-status", Static).update(
            (f"Server search: {query} · " if query else "") + "Local filter · F2 Search server" +
            (" · No results" if not result.records else ""))
        self.render_rows()

    def render_rows(self):
        query = self.query_one(Input).value.casefold().strip()
        table = self.query_one(UserTable)
        previous = table.current_login
        table.clear()
        for item in self.records:
            if query not in f"{item.key} {item.description}".casefold():
                continue
            table.add_row(escape(item.key), escape(item.description), key=item.key)
        if previous and previous in table.rows:
            table.move_cursor(row=table.get_row_index(previous), animate=False)
        if self.ready:
            self.query_one("#directory-status", Static).update(
                (f"Server search: {self.server_query} · " if self.server_query else "") +
                f"{table.row_count} shown · Local filter · F2 Search server" +
                (" · No local matches" if not table.row_count and self.records else " · No results" if not self.records else ""))
        self.update_footer()

    def update_footer(self):
        if not self.is_mounted or not self.display:
            return
        if isinstance(self.app.focused, Sidebar):
            self.screen.update_footer()
            return
        if isinstance(self.app.focused, Input):
            text = "Type to filter loaded records  Enter Results  Esc Clear filter"
        elif self.load_state == "loading":
            text = "Loading…  Tab Sections  Esc Users"
        elif self.load_state == "error":
            text = "r Retry  Tab Sections  Esc Users"
        else:
            delete = "  d Delete" if self.kind in {"host", "group", "hostgroup"} else ""
            text = f"↑↓/jk Move  Enter Open  a Add  e Edit{delete}\n/ Filter  F2 Server search  Tab Focus  r Refresh  ? Help  Esc Users"
        self.screen.query_one(ContextFooter).update(text)

    @on(Input.Changed)
    def filter_changed(self):
        if self.ready:
            self.render_rows()

    @on(Input.Submitted)
    def filter_submitted(self):
        self.query_one(UserTable).focus()

    @on(UserTable.OpenRequested)
    def open_record(self, event):
        event.stop()
        if self.ready:
            if self.kind in {"hbacrule", "sudorule"}:
                from .rule_ui import RuleScreen
                self.app.push_screen(RuleScreen(self.kind, event.login, on_saved=self.reload))
            else:
                self.app.push_screen(RecordDetailScreen(self.kind, event.login, on_saved=self.reload))

    @on(Button.Pressed)
    def group_button(self, event):
        event.stop()
        self.action_group_type("group" if event.button.id == "user-groups" else "hostgroup")

    def action_group_type(self, kind):
        self.kind = kind
        self.server_query = ""
        self.query_one(Input).value = ""
        self.reload()
        self.query_one(UserTable).focus()

    def action_create(self):
        if self.kind in {"hbacrule", "sudorule"}:
            from .rule_ui import RuleScreen
            self.app.push_screen(RuleScreen(self.kind, on_saved=self.reload))
        else:
            from .forms import ObjectForm
            self.app.push_screen(ObjectForm(self.kind, on_saved=self.reload))

    def action_edit(self):
        key = self.query_one(UserTable).current_login
        if key:
            if self.kind in {"hbacrule", "sudorule"}:
                from .rule_ui import RuleScreen
                self.app.push_screen(RuleScreen(self.kind, key, on_saved=self.reload))
            else:
                item = next(item for item in self.records if item.key == key)
                from .forms import ObjectForm
                self.app.push_screen(ObjectForm(self.kind, key, {"description": item.description}, on_saved=self.reload))

    def action_delete(self):
        key = self.query_one(UserTable).current_login
        if key:
            confirm_delete(self, self.kind, key, self.reload)

    def action_filter(self):
        self.query_one(Input).display = True
        self.query_one(Input).focus()

    def action_server_search(self):
        def chosen(query):
            if query is not None:
                self.server_query = query.strip()
                self.reload()
        self.app.push_screen(InputDialog("Search server (blank clears search)", self.server_query), chosen)

    def action_refresh(self):
        self.reload()

    def action_back(self):
        field = self.query_one(Input)
        if field.display:
            field.value = ""
            field.display = False
            self.query_one(UserTable).focus()
        else:
            self.screen.action_back()


def confirm_delete(screen, kind, key, on_close):
    from .actions import ActionResultsScreen
    def confirmed(apply):
        if apply:
            screen.app.push_screen(ActionResultsScreen(f"Delete {LABELS[kind]}", [key],
                                  lambda target: screen.app.directory.delete(kind, target), on_close=on_close))
    screen.app.push_screen(ReviewDialog("Confirm deletion", [f"Delete {kind}: {key}", "This removes the FreeIPA object."], apply_label="Delete"), confirmed)


class RecordDetailScreen(Screen):
    BINDINGS = [("escape", "back", "Back"), ("r", "refresh", "Refresh"),
                ("e", "edit", "Edit"), ("g,m", "members", "Memberships"), ("d", "delete", "Delete")]

    def __init__(self, kind, key, *, on_saved=None):
        super().__init__()
        self.kind, self.key, self.on_saved = kind, key, on_saved
        self.item = None
        self.request_id = 0

    def compose(self):
        yield TopBar()
        yield Static(f"{LABELS[self.kind]} / {self.key}", classes="heading", markup=False)
        with ScrollBody(id="record-body"):
            yield Static("Loading…", id="record-fields", markup=False)
        yield ContextFooter()

    def on_mount(self):
        self.query_one(ScrollBody).focus()
        self.reload()

    def reload(self, refresh=False):
        self.request_id += 1
        self.load(self.request_id, refresh)

    @work(group="record-detail", exclusive=True)
    async def load(self, request, refresh):
        try:
            item = await self.app.directory.get(self.kind, self.key, refresh=refresh)
        except BackendError as error:
            if request == self.request_id:
                self.query_one("#record-fields", Static).update(f"{error}\nr Retry · Esc Back")
            return
        if request != self.request_id:
            return
        self.item = item
        lines = [f"{'FQDN' if self.kind == 'host' else 'Name'}: {self.key}", f"Description: {item.description or 'Not provided'}"]
        if self.kind == "host":
            lines.extend((f"Host groups (direct): {item.text('memberof_hostgroup', 'None reported')}",
                          f"HBAC (associated): {item.text('memberof_hbacrule')}",
                          f"Sudo (associated): {item.text('memberof_sudorule')}",
                          "Associations are not an effective-access evaluation."))
        else:
            field = "user" if self.kind == "group" else "host"
            lines.extend((f"Members (direct): {item.text('member_' + field, 'None reported')}",
                          f"Members (indirect; read only): {item.text('memberindirect_' + field, 'None reported')}"))
        self.query_one("#record-fields", Static).update("\n\n".join(lines))
        self.query_one(ContextFooter).update("e Edit  g Memberships  d Delete  r Refresh  Esc Back\n↑↓/jk Scroll  ? Help")

    def check_action(self, action, parameters):
        return self.item is not None if action in {"edit", "members", "delete"} else True

    def action_edit(self):
        from .forms import ObjectForm
        self.app.push_screen(ObjectForm(self.kind, self.key, {"description": self.item.description}, on_saved=lambda: self.reload(True)))

    def action_members(self):
        from .membership import MembershipScreen
        self.app.push_screen(MembershipScreen(self.kind, self.key, on_saved=lambda: self.reload(True)))

    def action_delete(self):
        def done():
            self.action_back()
        confirm_delete(self, self.kind, self.key, done)

    def action_refresh(self):
        self.reload(True)

    def action_back(self):
        self.request_id += 1
        self.app.pop_screen()
        if self.on_saved:
            self.on_saved()
