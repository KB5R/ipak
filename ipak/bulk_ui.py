"""CSV → external editor → editable rows → groups → review → apply."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from textual import on, work
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import DataTable, Static

from .app import ContextFooter, TopBar
from .backend import BackendError
from .bulk import BulkCreator, DraftRow, LoginPolicy, merge_editor_rows, normalize_name, parse_csv, serialize_csv, template, validate_rows
from .dialogs import ChoiceDialog, DraftDialog, InputDialog, ResultsScreen, ReviewDialog
from .membership import MembershipScreen


USER_GRID = (("full_name", "FULL NAME"), ("givenname", "GIVEN NAME"), ("sn", "SURNAME"),
             ("login", "LOGIN"), ("mail", "EMAIL"), ("telephonenumber", "PHONE"), ("ou", "DEPARTMENT"),
             ("title", "TITLE"), ("carlicense", "COMMENT"))
HOST_GRID = (("fqdn", "FQDN"), ("ip_address", "IP"), ("description", "DESCRIPTION"))


class DraftTable(DataTable):
    BINDINGS = [("j", "cursor_down", ""), ("k", "cursor_up", "")]


class BulkScreen(Screen):
    BINDINGS = [("enter", "edit", "Edit cell"), ("space", "mark", "Mark"),
                ("g", "groups", "Groups"), ("d", "delete", "Delete draft row"),
                ("u", "undo", "Undo"), ("e", "editor", "CSV editor"), ("a", "add", "Add row"),
                ("ctrl+s", "review", "Review"), ("escape", "back", "Back")]

    def __init__(self, kind: str, *, auto_editor=True):
        super().__init__()
        self.kind, self.auto_editor = kind, auto_editor
        self.columns = USER_GRID if kind == "user" else HOST_GRID
        self.rows = []
        self.marks = set()
        self.deleted = []
        self.busy = False
        self.order = "surname_given"
        self.order_confirmed = not auto_editor or kind != "user"
        self.policy = LoginPolicy()
        self.editor_path = None
        self.editor_failed = False
        self.editor_returncode = None
        self.editor_export_snapshot = None
        self.draft_key = ("bulk", kind)

    def compose(self):
        yield TopBar()
        yield Static(f"Bulk / Create {'users' if self.kind == 'user' else 'hosts'}", classes="heading", markup=False)
        yield DraftTable(id="bulk-table", cursor_type="cell", fixed_columns=1, show_row_labels=False)
        yield Static("", id="bulk-status", classes="form-status", markup=False)
        yield ContextFooter()

    def on_mount(self):
        table = self.query_one(DraftTable)
        table.add_column("", width=3, key="mark")
        for name, label in self.columns:
            table.add_column(label, width=20 if name in {"full_name", "mail", "fqdn"} else 16, key=name)
        table.add_column("GROUPS", width=24, key="groups")
        table.add_column("STATUS", width=24, key="status")
        draft = self.app.drafts.get(self.draft_key)
        if draft:
            self.rows, self.order, self.editor_path = draft["rows"], draft["order"], draft.get("editor_path")
            self.editor_failed = draft.get("editor_failed", False)
            self.editor_returncode = draft.get("editor_returncode")
            self.editor_export_snapshot = draft.get("editor_export_snapshot")
            self.render_rows()
            self.order_confirmed = True
        elif self.auto_editor:
            if self.kind == "user":
                choices = ("Surname Given name [Patronymic]", "Given name Surname")
                def choose(order):
                    if order is None:
                        return
                    self.order = "surname_given" if order == choices[0] else "given_surname"
                    self.order_confirmed = True
                    self.action_editor()
                self.app.push_screen(ChoiceDialog("Explicitly choose the CSV name order", choices), choose)
            else:
                self.action_editor()
        table.focus()
        self.update_footer()

    def check_action(self, action, parameters):
        if self.busy:
            return False
        if action in {"edit", "mark", "groups", "delete", "review"}:
            return bool(self.rows)
        return True

    def update_footer(self):
        self.query_one(ContextFooter).update(
            "←→ Cell  ↑↓/jk Row  Enter Edit  Space Mark  g Groups\n"
            "e CSV editor  a Row  d Delete row  u Undo  Ctrl+S Review  ? Help  Esc Back")

    def load_content(self, content):
        incoming = parse_csv(content, self.kind, self.order, self.policy)
        self.rows = merge_editor_rows(self.rows, incoming, self.kind)
        self.marks.intersection_update(row.id for row in self.rows)
        validate_rows(self.rows, self.kind)
        self.render_rows()

    def render_rows(self):
        table = self.query_one(DraftTable)
        old_row, old_column = table.cursor_row, table.cursor_column
        table.clear()
        for row in self.rows:
            state = "; ".join(row.errors) if row.errors else row.status
            table.add_row(escape("[x]" if row.id in self.marks else "[ ]"),
                          *[escape(row.values.get(name, "")) for name, _ in self.columns],
                          escape(", ".join(sorted(row.groups))), escape(state), key=row.id)
        if self.rows:
            table.move_cursor(row=min(old_row, len(self.rows) - 1), column=old_column, animate=False)
        self.show_row_status()

    @on(DataTable.CellHighlighted, "#bulk-table")
    def highlighted(self):
        self.show_row_status()

    def show_row_status(self):
        if not self.is_mounted:
            return
        row = self.current_row()
        if row:
            detail = "; ".join(row.errors) if row.errors else row.status
            status = f"{len(self.rows)} rows · {len(self.marks)} marked\n{row.key(self.kind) or 'New row'}: {detail}"
        else:
            status = "No draft rows. e Open CSV editor · a Add row. Comment examples are never created."
        if self.editor_failed:
            status += f"\nEditor exit: {self.editor_returncode if self.editor_returncode is not None else 'unavailable'}; CSV retained: {self.editor_path}. e Reopen."
        self.query_one("#bulk-status", Static).update(status)

    def current_row(self):
        index = self.query_one(DraftTable).cursor_row
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def targets(self):
        return [row for row in self.rows if row.id in self.marks] if self.marks else [self.current_row()] if self.current_row() else []

    @on(DataTable.CellSelected, "#bulk-table")
    def cell_selected(self, event):
        event.stop()
        self.action_edit()

    def action_edit(self):
        row = self.current_row()
        if row is None:
            return
        column = self.query_one(DraftTable).cursor_column
        if column == 0:
            self.action_mark()
            return
        if column == len(self.columns) + 1:
            self.action_groups()
            return
        if column > len(self.columns):
            return
        if row.created:
            self.query_one("#bulk-status", Static).update("Already created. Fields are locked to prevent duplicate creation; failed memberships can be retried.")
            return
        name, label = self.columns[column - 1]
        def edit(value):
            if value is None:
                return
            row.values[name] = value.strip()
            row.errors = [error for error in row.errors if error.startswith("Line ")]
            if name == "full_name":
                normalize_name(row, self.order, self.policy)
            if name == "login":
                row.generated_login = not bool(value.strip())
            if self.kind == "user" and name in {"givenname", "sn", "login"} and (row.generated_login or not row.values.get("login")):
                try:
                    row.values["login"] = self.policy.generate(row.values.get("givenname", ""), row.values.get("sn", ""))
                    row.generated_login = True
                except ValueError:
                    pass
            validate_rows(self.rows, self.kind)
            self.render_rows()
        self.app.push_screen(InputDialog(label, row.values.get(name, "")), edit)

    def action_mark(self):
        row = self.current_row()
        if row:
            self.marks.remove(row.id) if row.id in self.marks else self.marks.add(row.id)
            self.render_rows()

    def action_add(self):
        self.rows.append(DraftRow({}))
        validate_rows(self.rows, self.kind)
        self.render_rows()
        self.query_one(DraftTable).move_cursor(row=len(self.rows) - 1, column=1, animate=False)

    def action_delete(self):
        ids = {row.id for row in self.targets()}
        self.deleted.append([(index, row) for index, row in enumerate(self.rows) if row.id in ids])
        self.rows = [row for row in self.rows if row.id not in ids]
        self.marks.difference_update(ids)
        self.render_rows()

    def action_undo(self):
        if self.deleted:
            for index, row in self.deleted.pop():
                self.rows.insert(index, row)
            validate_rows(self.rows, self.kind)
            self.render_rows()

    def action_groups(self):
        targets = self.targets()
        common = set.intersection(*(set(row.groups) for row in targets)) if targets else set()
        locked = set.union(*(set(row.groups_done) for row in targets)) & common if targets else set()
        def chosen(selected):
            for row in targets:
                row.groups = ((row.groups | (selected - common)) - (common - selected)) | row.groups_done
            self.render_rows()
        self.app.push_screen(MembershipScreen("draft", f"Groups for {len(targets)} rows", draft_target="group" if self.kind == "user" else "hostgroup",
                                             initial=common, locked=locked, on_accept=chosen))

    @work(group="csv-editor", exclusive=True)
    async def action_editor(self):
        self.busy = True
        try:
            if not self.order_confirmed:
                choices = ("Surname Given name [Patronymic]", "Given name Surname")
                def choose(order):
                    if order:
                        self.order = "surname_given" if order == choices[0] else "given_surname"
                        self.order_confirmed = True
                        self.action_editor()
                self.app.push_screen(ChoiceDialog("Explicitly choose the CSV name order", choices), choose)
                return
            current = serialize_csv(self.rows, self.kind) if self.rows else template(self.kind, self.order)
            if self.editor_failed and self.editor_export_snapshot is not None and current != self.editor_export_snapshot:
                choices = ("Use retained file (replace table on import)", "Export table (replace retained file)")
                def choose(source):
                    if source:
                        self.editor_failed = source == choices[0]
                        self.editor_export_snapshot = current
                        self.action_editor()
                self.app.push_screen(ChoiceDialog("Both table and retained CSV changed", choices), choose)
                return
            if self.editor_path is None:
                descriptor, filename = tempfile.mkstemp(prefix="ipak-", suffix=".csv")
                os.close(descriptor)
                self.editor_path = Path(filename)
            if not self.editor_failed:
                self.editor_path.write_text(current, encoding="utf-8")
                self.editor_export_snapshot = current
            command = shlex.split(os.environ.get("EDITOR") or "vim")
            if not command:
                raise ValueError("Set EDITOR to an executable command")
            self.editor_returncode = None
            with self.app.suspend():
                completed = await asyncio.to_thread(subprocess.run, [*command, str(self.editor_path)], check=False)
            self.editor_returncode = completed.returncode
            if completed.returncode:
                self.editor_failed = True
                self.query_one("#bulk-status", Static).update(f"Editor returned {completed.returncode}. Draft retained: {self.editor_path}\ne Reopen retained file; no creation occurred.")
                return
            content = self.editor_path.read_text(encoding="utf-8-sig")
            self.load_content(content)
            self.editor_failed = False
            self.show_row_status()
        except (OSError, ValueError, UnicodeError) as error:
            self.editor_failed = True
            self.query_one("#bulk-status", Static).update(f"{error}\nDraft retained: {self.editor_path}")
        finally:
            self.busy = False
            self.query_one(DraftTable).focus()

    @work(group="bulk-preflight", exclusive=True)
    async def action_review(self):
        self.busy = True
        creator = BulkCreator(self.app.directory, self.kind)
        try:
            valid = await creator.preflight(self.rows, self.progress)
            self.render_rows()
            if not valid:
                self.query_one("#bulk-status", Static).update("Preflight failed. Correct the marked row errors; nothing was created.")
                return
            pending = [row for row in self.rows if not row.created or row.groups - row.groups_done]
            if not pending:
                self.query_one("#bulk-status", Static).update("All rows are already complete. No writes to repeat.")
                return
            lines = [f"{len(pending)} rows to apply"]
            if self.kind == "user":
                lines.append("New users receive unique temporary passwords and must change them at login.")
            for row in pending:
                lines.append(f"\n{row.key(self.kind)} — {'Create' if not row.created else 'Retry pending memberships only'}")
                lines.extend(f"  {label}: {row.values.get(name, '')}" for name, label in self.columns if row.values.get(name))
                lines.append("  Add to groups: " + (", ".join(sorted(row.groups - row.groups_done)) or "None"))
            self.app.push_screen(ReviewDialog("Review bulk creation", lines),
                                 lambda apply: self.apply_rows(creator, pending) if apply else None)
        except BackendError as error:
            self.query_one("#bulk-status", Static).update(str(error))
        finally:
            self.busy = False

    def progress(self, index, count, row):
        self.query_one("#bulk-status", Static).update(f"Processing {index}/{count}: {row.key(self.kind)}")

    @work(group="bulk-write", exclusive=True)
    async def apply_rows(self, creator, rows):
        self.busy = True
        try:
            results = await creator.apply(rows, self.progress)
            self.render_rows()
            self.app.push_screen(ResultsScreen("Bulk results", results, on_close=self.render_rows))
        finally:
            self.busy = False

    def action_back(self):
        def leave(choice):
            if choice == "stay":
                return
            if choice == "keep":
                self.app.drafts[self.draft_key] = {"rows": self.rows, "order": self.order, "editor_path": self.editor_path,
                                                  "editor_failed": self.editor_failed,
                                                  "editor_returncode": self.editor_returncode,
                                                  "editor_export_snapshot": self.editor_export_snapshot}
            else:
                self.app.drafts.pop(self.draft_key, None)
                if self.editor_path:
                    self.editor_path.unlink(missing_ok=True)
            self.app.pop_screen()
        if self.rows or self.editor_failed:
            self.app.push_screen(DraftDialog(), leave)
        else:
            leave("discard")
