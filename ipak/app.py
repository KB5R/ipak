"""Application shell, contextual navigation, Users list and user card."""

from __future__ import annotations

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.markup import escape
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Input, OptionList, Static

from .backend import BackendError, FakeBackend, UserService
from .models import UserDetail, UserList


SECTIONS = ("Users", "Hosts", "Groups", "HBAC", "Sudo", "Bulk Operations")

CONTEXT_HELP = {
    "Hosts": "↑↓ / j k Move · Enter Open host\n/ Local filter · F2 Search server · r Refresh\na Create · e Edit description · d Review deletion\nTab Sections · Esc Users · q Quit",
    "Groups": "1 User Groups · 2 Host Groups\n↑↓ / j k Move · Enter Open group\n/ Local filter · F2 Search server · r Refresh\na Create · e Edit description · d Review deletion\nOpen a group and press g to manage direct members.\nTab Sections · Esc Users · q Quit",
    "HBAC": "↑↓ / j k Move · Enter Open rule\n/ Local filter · F2 Search server · r Refresh\na New rule · e Edit rule\nRule edits stay local until Ctrl+S → Apply.\nTab Sections · Esc Users · q Quit",
    "Sudo": "↑↓ / j k Move · Enter Open rule\n/ Local filter · F2 Search server · r Refresh\na New rule · e Edit rule\nRule edits stay local until Ctrl+S → Apply.\nTab Sections · Esc Users · q Quit",
    "Bulk Operations": "↑↓ / j k Move · Enter Choose operation\nCreate users / hosts opens a CSV in $EDITOR (Vim fallback).\nEditor exit never applies changes.\nEnable, disable and password resets use Users marks.\nManage memberships opens the shared membership editor.\nTab Sections · Esc Users · q Quit",
    "Memberships": "Tab / Shift+Tab Switch pane\n↑↓ / j k Move · Space Mark only · / Search\na Stage additions from Available\nd Stage removals from Members · u Undo\nCtrl+S Review changes; in a nested selector, return locally\nEsc Stay / Discard / Return and keep draft\n\nAvailable searches the server; Members filters locally.\nEach pane keeps its own marks, including hidden rows.\nIndirect memberships are read only.",
    "Bulk draft": "Arrows Move cell · Enter Edit cell · Space Mark row\na Add row · d Delete draft rows · u Undo\ng Assign groups to marked rows, or the current row\ne Reopen $EDITOR · Ctrl+S Validate and review\nEsc Stay / Discard / Return and keep draft\n\nScroll horizontally for additional fields.\nApplied rows are retained for safe retry of failures.\nPasswords appear only in the current results screen.",
    "Rule editor": "↑↓ / j k Move property · Enter Edit / Open selector\nu Undo · r Refresh · Ctrl+S Review the entire rule\nEsc Stay / Discard / Return and keep draft\n\nSelectors only change the local draft.\nUnconfigured, Selected and ALL are distinct modes.\nALL requires explicit choice and appears in final review.\nAn enabled rule is disabled while its changes are applied.\nFailures remain visible and leave the rule disabled.",
    "Sudo options": "↑↓ Move · a Add option · d Remove selected option\nCtrl+S Return options to the local rule draft\nEsc Stay / Discard / Return and keep changes\n\nEnter sudoers option strings; the server validates them\nonly when the entire rule is reviewed and applied.",
    "Object detail": "↑↓ / j k Scroll · r Refresh\ne Edit description · g Manage direct memberships\nd Review object deletion · Esc Return\n\nIndirect memberships are read only.\nHBAC / Sudo associations do not evaluate effective access.",
    "Operation results": "↑↓ / j k Scroll results · Esc Return\nr Retry failed targets when offered\np Reveal / Hide temporary passwords when present\n\nSuccessful targets are not repeated during retry.\nClosing this screen clears its temporary passwords.",
}


class TopBar(Horizontal):
    def compose(self) -> ComposeResult:
        yield Static(f"IPAK  ·  {self.app.users.backend.server}", id="server", markup=False)
        yield Static(self.app.users.backend.connection, id="connection", markup=False)

    def on_mount(self) -> None:
        self.set_interval(0.5, self.update_status)

    def update_status(self) -> None:
        self.query_one("#server", Static).update(f"IPAK  ·  {self.app.users.backend.server}")
        self.query_one("#connection", Static).update(self.app.users.backend.connection)


class ContextFooter(Static):
    def __init__(self) -> None:
        super().__init__("", id="shortcuts", markup=False)


class Sidebar(OptionList):
    BINDINGS = [("j", "cursor_down", ""), ("k", "cursor_up", "")]


class BulkMenu(OptionList):
    BINDINGS = [("j", "cursor_down", ""), ("k", "cursor_up", "")]


class StateMessage(Static):
    can_focus = True


class UserTable(DataTable):
    BINDINGS = [("j", "cursor_down", ""), ("k", "cursor_up", "")]

    class OpenRequested(Message):
        def __init__(self, login: str) -> None:
            super().__init__()
            self.login = login

    @property
    def current_login(self) -> str | None:
        if not self.row_count:
            return None
        return self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value

    def action_select_cursor(self) -> None:
        # Mouse selection and cursor movement do not request a user detail.
        if self.current_login is not None:
            self.post_message(self.OpenRequested(self.current_login))


class HelpScreen(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, context: str) -> None:
        super().__init__()
        self.context = context

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Static(f"Help / {self.context}", classes="heading", markup=False)
            if self.context in CONTEXT_HELP:
                text = CONTEXT_HELP[self.context]
            elif self.context == "User detail":
                text = (
                    "↑↓ / j k   Scroll the card\n"
                    "r          Refresh this user\n"
                    "Esc        Return to the same list position\n\n"
                    "Groups are direct memberships.\n"
                    "HBAC / Sudo are reported associations,\n"
                    "not an effective-access evaluation.\n\n"
                    "e Edit fields · g Groups · a Actions\n"
                    "Changes are reviewed before applying."
                )
            else:
                text = (
                    "↑↓ / j k   Move within the focused list\n"
                    "Enter      Open user / switch section\n"
                    "Tab        Switch focus; Shift+Tab goes back\n"
                    "/          Filter loaded users locally\n"
                    "Space      Mark / unmark the focused user\n"
                    "c          Clear marks (including hidden rows)\n"
                    "r          Refresh / retry Users\n"
                    "Esc        Clear filter / cancel load / return\n"
                    "q          Quit only at the top level\n\n"
                    "Typing in Filter never triggers shortcuts.\n"
                    "Marks persist through filtering.\n"
                    "Selected users: u Enable · x Disable\n"
                    "p Reset password · g Add to group\n"
                    "a Create · e Edit · F2 Server search"
                )
            yield Static(text, markup=False)
            yield Static("Esc Close", classes="help-close", markup=False)


class HomeScreen(Screen[None]):
    BINDINGS = [
        ("slash", "filter", "Filter"), ("space", "mark", "Mark"),
        ("c", "clear_marks", "Clear marks"), ("r", "refresh", "Refresh"),
        ("escape", "back", "Back"), ("question_mark", "help", "Help"),
        ("q", "app.quit", "Quit"),
        ("u", "users_action('enable')", "Enable"), ("x", "users_action('disable')", "Disable"),
        ("p", "users_action('reset')", "Reset password"), ("g", "users_action('group')", "Add to group"),
        ("a", "create_user", "Add user"), ("e", "edit_user", "Edit"), ("f2", "server_search", "Server search"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.section = "Users"
        self.result: UserList | None = None
        self.marked: set[str] = set()
        self.visible_logins: list[str] = []
        self.load_state = "loading"
        self.request_id = 0
        self.list_worker = None
        self.server_query = ""

    def compose(self) -> ComposeResult:
        from .directory_ui import DirectoryPane
        yield TopBar()
        with Horizontal(id="workspace"):
            yield Sidebar(*SECTIONS, id="sections", markup=False)
            with Vertical(id="main"):
                with Vertical(id="users-pane"):
                    yield Static("Users", id="list-title", classes="heading", markup=False)
                    yield Input(placeholder="Filter loaded users…", id="filter", compact=True)
                    yield Static("", id="selection", markup=False)
                    yield Static("", id="scope", markup=False)
                    yield StateMessage("Loading users…  Esc Cancel", id="list-state", markup=False)
                    yield UserTable(id="users", cursor_type="row", show_row_labels=False)
                yield DirectoryPane(id="directory-pane")
                yield StateMessage("", id="section-placeholder", markup=False)
                yield BulkMenu("Create users", "Create hosts", "Disable / enable users",
                               "Reset passwords", "Manage memberships", id="bulk-menu", markup=False)
        yield ContextFooter()

    def on_mount(self) -> None:
        self.query_one("#sections", Sidebar).highlighted = 0
        self.query_one("#filter").display = False
        self.query_one("#selection").display = False
        self.query_one("#section-placeholder").display = False
        self.query_one("#section-placeholder").can_focus = False
        self.query_one("#bulk-menu").display = False
        self.query_one("#directory-pane").display = False
        table = self.query_one(UserTable)
        for label, width, key in (("", 3, "mark"), ("LOGIN", 12, "login"),
                                  ("NAME", 20, "name"), ("STATE", 8, "state")):
            table.add_column(label, width=width, key=key)
        self.start_load()

    def on_descendant_focus(self) -> None:
        if self.is_mounted:
            self.update_selection()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        typing = isinstance(self.focused, Input)
        if action in {"filter", "mark", "clear_marks", "refresh", "help", "users_action", "create_user", "edit_user"} and typing:
            return False
        if action == "users_action":
            return self.section == "Users" and bool(self.marked) and self.load_state == "ready"
        if action in {"create_user", "server_search"}:
            return self.section == "Users" and self.load_state != "loading"
        if action == "edit_user":
            return self.section == "Users" and isinstance(self.focused, UserTable) and bool(self.visible_logins) and self.load_state == "ready"
        if action == "filter":
            return self.section == "Users" and self.result is not None and self.load_state == "ready"
        if action == "mark":
            return (self.section == "Users" and isinstance(self.focused, UserTable)
                    and bool(self.visible_logins) and self.load_state == "ready")
        if action == "clear_marks":
            return self.section == "Users" and bool(self.marked)
        if action == "refresh":
            return self.section == "Users" and self.load_state != "loading"
        return True

    def update_footer(self) -> None:
        if not self.is_mounted:
            return
        if isinstance(self.focused, Input):
            text = "Type to filter loaded users  Enter Results  Esc Clear filter\nTab / Shift+Tab Focus"
        elif isinstance(self.focused, Sidebar):
            text = "↑↓/jk Move  Enter Open section  Tab View  Esc Return\n? Help  q Quit"
        elif self.section in {"Hosts", "Groups", "HBAC", "Sudo"}:
            self.query_one("#directory-pane").update_footer()
            return
        elif self.section != "Users":
            text = "↑↓/jk Move  Enter Open  Tab Sections  Esc Users\n? Help  q Quit"
        elif self.load_state == "loading":
            text = "Loading users…  Esc Cancel  Tab Sections\n? Help  q Quit"
        elif self.load_state in {"error", "cancelled"}:
            text = "r Retry  Tab Sections  Esc Return\n? Help  q Quit"
        elif not self.visible_logins:
            clear = "  c Clear selected" if self.marked else ""
            text = f"/ Filter  r Refresh  Tab Sections{clear}\nEsc Return  ? Help  q Quit"
        elif self.marked:
            text = "u Enable  x Disable  p Reset password  g Add to group  c Clear\nSpace Mark  Enter Open  / Filter  Tab Sections  ? Help  q Quit"
        else:
            text = "↑↓/jk Move  Enter Open  Space Mark  a Add  e Edit\n/ Filter  F2 Server search  Tab Sections  r Refresh  ? Help  q Quit"
        self.query_one(ContextFooter).update(text)
        self.refresh_bindings()

    def start_load(self, *, refresh: bool = False) -> None:
        self.request_id += 1
        self.load_state = "loading"
        self.query_one("#list-state", Static).update("Loading users…  Esc Cancel")
        self.query_one("#list-state").display = True
        self.query_one(UserTable).display = False
        self.query_one("#list-title", Static).update("Users — Loading…")
        self.query_one("#scope").display = False
        if self.section == "Users":
            self.query_one("#list-state").focus()
        self.list_worker = self.load_users(self.request_id, refresh)
        self.update_footer()

    @work(group="users-list", exclusive=True)
    async def load_users(self, request_id: int, refresh: bool) -> None:
        try:
            result = await self.app.users.list_users(refresh=refresh, query=self.server_query)
        except Exception as error:
            if request_id != self.request_id:
                return
            self.load_state = "error"
            self.query_one("#list-title", Static).update("Users — Load failed")
            message = str(error) if isinstance(error, BackendError) else "Unexpected load failure. Retry."
            self.query_one("#list-state", Static).update(f"{message}\nr Retry")
            self.update_footer()
            return
        if request_id != self.request_id:
            return
        self.result = result
        self.load_state = "ready"
        if not self.server_query and not result.truncated:
            self.marked.intersection_update(user.login for user in result.users)
        self.render_users()
        # A background response must not steal focus from navigation or search.
        if self.section == "Users" and self.focused is self.query_one("#list-state"):
            self.focus_results()

    def render_users(self) -> None:
        if self.result is None:
            return
        table = self.query_one(UserTable)
        previous = table.current_login
        old_row = table.cursor_row
        query = self.query_one("#filter", Input).value.casefold().strip()
        users = [user for user in self.result.users
                 if query in f"{user.login} {user.full_name} {user.state}".casefold()]
        self.visible_logins = [user.login for user in users]
        table.clear()
        for user in users:
            table.add_row(escape("[x]" if user.login in self.marked else "[ ]"),
                          escape(user.login), escape(user.full_name), user.state, key=user.login)
        if users:
            row = self.visible_logins.index(previous) if previous in self.visible_logins else min(old_row, len(users) - 1)
            table.move_cursor(row=row, animate=False)
        count = len(self.result.users)
        suffix = "; more may exist" if self.result.truncated else ""
        self.query_one("#list-title", Static).update(f"Users ({count} loaded{suffix})")
        scope = f"{len(users)} shown · Local filter"
        if self.server_query:
            scope += f" on server search: {self.server_query}"
        if self.result.truncated:
            scope += " only; F2 searches the server."
        self.query_one("#scope", Static).update(scope)
        self.query_one("#scope").display = True
        self.query_one("#list-state").display = not users
        self.query_one("#list-state", Static).update(
            "No users match the local filter. Esc Clear filter."
            if self.result.users else "No users returned. r Refresh."
        )
        table.display = bool(users)
        self.update_selection()
        self.update_footer()

    def update_selection(self) -> None:
        strip = self.query_one("#selection", Static)
        strip.display = bool(self.marked)
        hidden = len(self.marked.difference(self.visible_logins))
        action = "" if isinstance(self.focused, Input) else "\nu Enable | x Disable | p Reset password | g Add to group | c Clear"
        strip.update(f"{len(self.marked)} selected ({hidden} hidden){action}")
        self.update_footer()

    def focus_results(self) -> None:
        target = UserTable if self.visible_logins and self.load_state == "ready" else "#list-state"
        self.query_one(target).focus()

    @on(Input.Changed, "#filter")
    def filter_changed(self) -> None:
        if self.load_state == "ready":
            self.render_users()

    @on(Input.Submitted, "#filter")
    def filter_submitted(self) -> None:
        self.focus_results()

    @on(UserTable.OpenRequested)
    def open_user(self, message: UserTable.OpenRequested) -> None:
        if self.section == "Users" and self.load_state == "ready":
            self.app.push_screen(UserDetailScreen(message.login))

    @on(OptionList.OptionSelected, "#sections")
    def switch_section(self, message: OptionList.OptionSelected) -> None:
        section = SECTIONS[message.option_index]
        if section != self.section:
            self.marked.clear()
            self.query_one("#filter", Input).value = ""
            self.query_one("#filter").display = False
        self.section = section
        self.query_one("#users-pane").display = section == "Users"
        placeholder = self.query_one("#section-placeholder", Static)
        placeholder.display = section == "Bulk Operations"
        self.query_one("#bulk-menu").display = section == "Bulk Operations"
        directory = self.query_one("#directory-pane")
        directory.display = section in {"Hosts", "Groups", "HBAC", "Sudo"}
        if section == "Users":
            self.focus_results() if self.load_state == "ready" else self.query_one("#list-state").focus()
        elif directory.display:
            directory.open_section(section)
        else:
            placeholder.update("Bulk Operations")
            self.query_one("#bulk-menu").focus()
        self.update_selection()

    def action_filter(self) -> None:
        field = self.query_one("#filter", Input)
        field.display = True
        field.focus()

    def action_mark(self) -> None:
        table = self.query_one(UserTable)
        login = table.current_login
        if login is None:
            return
        if login in self.marked:
            self.marked.remove(login)
        else:
            self.marked.add(login)
        table.update_cell(login, "mark", escape("[x]" if login in self.marked else "[ ]"))
        self.update_selection()

    def action_clear_marks(self) -> None:
        self.marked.clear()
        if self.load_state == "ready":
            self.render_users()
        else:
            self.update_selection()

    def action_refresh(self) -> None:
        self.start_load(refresh=True)

    def action_back(self) -> None:
        field = self.query_one("#filter", Input)
        if self.section != "Users":
            sidebar = self.query_one(Sidebar)
            sidebar.highlighted = 0
            sidebar.action_select()
        elif field.display:
            field.value = ""
            field.display = False
            if self.load_state == "ready":
                self.render_users()
            self.focus_results()
        elif self.load_state == "loading":
            self.request_id += 1
            if self.list_worker:
                self.list_worker.cancel()
            self.load_state = "cancelled"
            self.query_one("#list-title", Static).update("Users — Load cancelled")
            self.query_one("#list-state", Static).update("Load cancelled. r Retry.")
            self.query_one("#list-state").focus()
        else:
            self.focus_results()
        self.update_footer()

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen(self.section))

    def action_users_action(self, action):
        from .actions import user_action
        user_action(self, action, self.marked, lambda: self.start_load(refresh=True))

    def action_create_user(self):
        from .forms import ObjectForm
        self.app.push_screen(ObjectForm("user", on_saved=lambda: self.start_load(refresh=True)))

    @work(group="open-user-editor", exclusive=True)
    async def action_edit_user(self):
        login = self.query_one(UserTable).current_login
        if login:
            try:
                detail = await self.app.users.get_user(login)
            except BackendError as error:
                self.notify(str(error), severity="error")
                return
            from .forms import ObjectForm
            self.app.push_screen(ObjectForm("user", login, {
                "givenname": detail.given_name, "sn": detail.surname, "cn": detail.user.full_name,
                "mail": ", ".join(detail.emails), "carlicense": detail.comment,
            }, on_saved=lambda: self.start_load(refresh=True)))

    def action_server_search(self):
        from .dialogs import InputDialog
        def chosen(query):
            if query is not None:
                self.server_query = query.strip()
                self.start_load(refresh=True)
        self.app.push_screen(InputDialog("Server-wide search (blank restores the bounded directory list)", self.server_query), chosen)

    @on(OptionList.OptionSelected, "#bulk-menu")
    def bulk_operation(self, event):
        event.stop()
        if event.option_index < 2:
            from .bulk_ui import BulkScreen
            self.app.push_screen(BulkScreen("user" if event.option_index == 0 else "host"))
        elif event.option_index in {2, 3}:
            self.action_back()
            message = "Space marks users; u Enable / x Disable." if event.option_index == 2 else "Space marks users; p Reset password."
            self.notify(message, timeout=8)
        else:
            from .actions import PickerDialog
            from .dialogs import ChoiceDialog
            from .membership import MembershipScreen
            choices = {"User → user groups": "user", "Host → host groups": "host",
                       "User group → users": "group", "Host group → hosts": "hostgroup"}
            def selected(choice):
                if choice:
                    kind = choices[choice]
                    self.app.push_screen(PickerDialog(kind), lambda key: self.app.push_screen(MembershipScreen(kind, key)) if key else None)
            self.app.push_screen(ChoiceDialog("Membership owner", tuple(choices)), selected)


class DetailBody(VerticalScroll):
    can_focus = True
    BINDINGS = [("j", "scroll_down", ""), ("k", "scroll_up", "")]


class UserDetailScreen(Screen[None]):
    BINDINGS = [("escape", "back", "Back"), ("r", "refresh", "Refresh"),
                ("question_mark", "help", "Help"), ("e", "edit", "Edit"),
                ("g", "groups", "Groups"), ("a", "actions", "Actions")]
    FIELDS = (
        ("name", "Full name"), ("email", "Email"), ("groups", "Groups (direct)"),
        ("hbac", "HBAC (associated)"), ("sudo", "Sudo (associated)"),
        ("comment", "Comment"), ("password", "Password"),
    )

    def __init__(self, login: str) -> None:
        super().__init__()
        self.login = login
        self.request_id = 0
        self.accept_responses = True
        self.actions_completed = False
        self.load_state = "loading"
        self.detail = None

    def compose(self) -> ComposeResult:
        yield TopBar()
        yield Static(f"Users / {self.login}", id="detail-title", classes="heading", markup=False)
        with DetailBody(id="detail-body"):
            yield Static("Loading user…  Esc Back", id="detail-state", markup=False)
            with Vertical(id="detail-fields"):
                for key, label in self.FIELDS:
                    with Horizontal(classes="field"):
                        yield Static(label, classes="field-label", markup=False)
                        yield Static("", id=f"value-{key}", classes="field-value", markup=False)
                yield Static("Associations are not an effective-access evaluation.", classes="detail-note", markup=False)
                label = "Demo data · As of" if self.app.users.backend.is_demo else "Password status as of"
                yield Static(f"{label} {self.app.users.backend.reference_time:%Y-%m-%d} UTC",
                             classes="detail-note", markup=False)
        yield ContextFooter()

    def on_mount(self) -> None:
        self.query_one(DetailBody).focus()
        self.start_load()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"edit", "groups", "actions"}:
            return self.load_state == "ready"
        return self.load_state != "loading" if action == "refresh" else True

    def start_load(self, *, refresh: bool = False) -> None:
        self.request_id += 1
        self.load_state = "loading"
        self.query_one("#detail-state", Static).update("Loading user…  Esc Back")
        self.query_one("#detail-state").display = True
        self.query_one("#detail-fields").display = False
        self.query_one(ContextFooter).update("Loading user…  Esc Back\n? Help")
        self.load_user(self.request_id, refresh)

    @work(group="user-detail", exclusive=True)
    async def load_user(self, request_id: int, refresh: bool) -> None:
        try:
            detail = await self.app.users.get_user(self.login, refresh=refresh)
        except Exception as error:
            if not self.accept_responses or request_id != self.request_id:
                return
            self.load_state = "error"
            message = str(error) if isinstance(error, BackendError) else "Unexpected load failure. Retry."
            self.query_one("#detail-state", Static).update(message)
            self.query_one(ContextFooter).update("r Retry  Esc Back\n? Help")
            self.refresh_bindings()
            return
        if not self.accept_responses or request_id != self.request_id:
            return
        self.load_state = "ready"
        self.show_detail(detail)

    def show_detail(self, detail: UserDetail) -> None:
        self.detail = detail
        def associations(value: tuple[str, ...] | None) -> str:
            return "Not provided" if value is None else ", ".join(value) or "None reported"

        values = {
            "name": detail.user.full_name,
            "email": ", ".join(detail.emails) or "Not provided",
            "groups": ", ".join(detail.direct_groups) or "None reported",
            "hbac": associations(detail.associated_hbac),
            "sudo": associations(detail.associated_sudo),
            "comment": detail.comment or "Not provided",
            "password": detail.password_description(self.app.users.backend.reference_time),
        }
        self.query_one("#detail-title", Static).update(f"Users / {self.login}    {detail.user.state}")
        for key, value in values.items():
            self.query_one(f"#value-{key}", Static).update(value)
        self.query_one("#detail-state").display = False
        self.query_one("#detail-fields").display = True
        self.query_one(ContextFooter).update("e Edit  g Groups  a Actions  Esc Back\n↑↓/jk Scroll  r Refresh  ? Help")
        self.refresh_bindings()

    def action_refresh(self) -> None:
        self.start_load(refresh=True)

    def action_back(self) -> None:
        self.accept_responses = False
        self.request_id += 1
        self.workers.cancel_node(self)
        self.app.pop_screen()
        if self.actions_completed and isinstance(self.app.screen, HomeScreen):
            self.app.screen.start_load(refresh=True)

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen("User detail"))

    def action_edit(self):
        from .forms import ObjectForm
        detail = self.detail
        self.app.push_screen(ObjectForm("user", self.login, {
            "givenname": detail.given_name, "sn": detail.surname, "cn": detail.user.full_name,
            "mail": ", ".join(detail.emails), "carlicense": detail.comment,
        }, on_saved=lambda: self.start_load(refresh=True)))

    def action_groups(self):
        from .membership import MembershipScreen
        self.app.push_screen(MembershipScreen("user", self.login, on_saved=lambda: self.start_load(refresh=True)))

    def action_actions(self):
        from .dialogs import ChoiceDialog
        from .actions import user_action
        choices = {"Enable": "enable", "Disable": "disable", "Reset password": "reset", "Add to group": "group"}
        self.app.push_screen(ChoiceDialog("User actions", tuple(choices)),
                             lambda choice: user_action(self, choices[choice], [self.login],
                                                        self.actions_finished) if choice else None)

    def actions_finished(self):
        self.actions_completed = True
        self.start_load(refresh=True)


class IPAKApp(App[None]):
    TITLE = "IPAK"
    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
                Binding("question_mark", "context_help", "Help", show=False)]

    def __init__(self, backend=None) -> None:
        super().__init__()
        self.users = UserService(backend if backend is not None else FakeBackend())
        from .directory import DirectoryService
        self.directory = DirectoryService(self.users.backend, self.users)
        self.drafts = {}
        self.theme = "textual-dark"
        self.animation_level = "none"

    def on_unmount(self) -> None:
        if hasattr(self.users.backend, "close"):
            self.users.backend.close()

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"quit", "help_quit"}:
            return isinstance(self.screen, HomeScreen) and not isinstance(self.focused, Input)
        if action == "context_help":
            return not isinstance(self.focused, Input) and not isinstance(self.screen, ModalScreen)
        return True

    def action_context_help(self):
        contexts = {"MembershipScreen": "Memberships", "BulkScreen": "Bulk draft",
                    "RuleScreen": "Rule editor", "OptionsScreen": "Sudo options",
                    "RecordDetailScreen": "Object detail", "ResultsScreen": "Operation results",
                    "ActionResultsScreen": "Operation results"}
        context = contexts.get(type(self.screen).__name__)
        if context:
            self.push_screen(HelpScreen(context))

    async def action_quit(self):
        if not self.drafts:
            self.exit()
            return
        from .dialogs import ReviewDialog
        lines = ["These session drafts will be discarded:"]
        lines.extend(" / ".join(str(part) for part in key) for key in self.drafts)
        def confirmed(discard):
            if discard:
                for draft in self.drafts.values():
                    if isinstance(draft, dict) and draft.get("editor_path"):
                        draft["editor_path"].unlink(missing_ok=True)
                self.drafts.clear()
                self.exit()
        self.push_screen(ReviewDialog("Unsaved session drafts", lines, apply_label="Discard and quit"), confirmed)
