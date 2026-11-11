"""Shared review, input, unsaved-draft and per-target result interactions."""

from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, OptionList, Static

from .app import ContextFooter, TopBar
from .ui_trace import trace


class ButtonDialog(ModalScreen):
    """Make the keyboard target explicit even in terminals without color."""

    BINDINGS = [("left", "move_button(-1)", "Previous button"),
                ("right", "move_button(1)", "Next button")]

    def action_move_button(self, direction):
        buttons = list(self.query(".dialog-buttons Button"))
        if self.focused in buttons:
            buttons[(buttons.index(self.focused) + direction) % len(buttons)].focus()

    def on_descendant_focus(self, event):
        hints = self.query(".button-focus-hint")
        if hints:
            hints.first(Static).update(
                f"Enter: {event.widget.label} · ←/→ / Tab: move" if isinstance(event.widget, Button)
                else "Tab / Shift+Tab: select a button"
            )


class ScrollBody(VerticalScroll):
    can_focus = True
    BINDINGS = [("j", "scroll_down", ""), ("k", "scroll_up", "")]


class InputDialog(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, value: str = "", *, password: bool = False):
        super().__init__()
        self.title_text, self.value, self.password = title, value, password

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, markup=False)
            yield Input(self.value, id="dialog-input", password=self.password)
            yield Static("Enter Accept · Esc Cancel", markup=False)

    def on_mount(self):
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def submit(self, event):
        self.dismiss(event.value)

    def action_cancel(self):
        self.dismiss(None)


class ReviewDialog(ButtonDialog):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, lines: list[str], *, apply_label: str = "Apply", instructions: str = ""):
        super().__init__()
        self.title_text, self.lines, self.apply_label = title, lines, apply_label
        self.instructions = instructions

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog review-dialog"):
            yield Static(self.title_text, classes="heading", markup=False)
            with ScrollBody(classes="review-content"):
                yield Static("\n".join(self.lines), markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.apply_label, id="apply", variant="primary")
            yield Static("", classes="button-focus-hint", markup=False)
            if self.instructions:
                yield Static(self.instructions, markup=False)

    def on_mount(self):
        trace("review.open parent=" + type(self.app.screen_stack[-2]).__name__)
        self.query_one("#cancel").focus()

    @on(Button.Pressed)
    def choose(self, event):
        event.stop()
        trace("review.button." + event.button.id)
        self.dismiss(event.button.id == "apply")

    def action_cancel(self):
        trace("review.escape")
        self.dismiss(False)


class DraftDialog(ButtonDialog):
    BINDINGS = [("escape", "stay", "Stay")]

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static("Unsaved changes", classes="heading")
            yield Static("Stay to continue editing, discard changes, or return and keep this draft for this session.")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Stay", id="stay")
                yield Button("Discard", id="discard")
                yield Button("Return (keep draft)", id="keep")
            yield Static("", classes="button-focus-hint", markup=False)

    def on_mount(self):
        trace("draft.open")
        self.query_one("#stay").focus()

    @on(Button.Pressed)
    def choose(self, event):
        event.stop()
        trace("draft.button." + event.button.id)
        self.dismiss(event.button.id)

    def action_stay(self):
        self.dismiss("stay")


class ChoiceDialog(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, choices: tuple[str, ...]):
        super().__init__()
        self.title_text, self.choices = title, choices

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="heading", markup=False)
            yield OptionList(*self.choices, id="choices", markup=False)
            yield Static("↑↓ Move · Enter Choose · Esc Cancel")

    @on(OptionList.OptionSelected)
    def choose(self, event):
        self.dismiss(self.choices[event.option_index])

    def action_cancel(self):
        self.dismiss(None)


class ResultsScreen(Screen):
    BINDINGS = [("escape", "back", "Back"), ("p", "reveal", "Reveal passwords"),
                ("r", "retry", "Retry failures")]

    def __init__(self, title, results, *, retry=None, on_close=None):
        super().__init__()
        self.title_text, self.results = title, results
        self.retry_callback, self.close_callback = retry, on_close
        self.revealed = False
        self.busy = False

    def compose(self):
        yield TopBar()
        yield Static(self.title_text, classes="heading", markup=False)
        with ScrollBody(id="results-body"):
            yield Static("", id="results", markup=False)
        yield ContextFooter()

    def on_mount(self):
        trace("results.open")
        self.render_results()
        self.query_one("#results-body").focus()

    def render_results(self):
        trace(f"results.render total={len(self.results)} ok={sum(r.success for r in self.results)} busy={self.busy}")
        lines = []
        for result in self.results:
            state = "OK" if result.success and result.verified else "CHECK" if result.success else "FAILED"
            lines.append(f"{result.target} — {state}: {result.message}")
            if result.secret:
                lines.append(f"  Temporary password: {result.secret if self.revealed else '•••••••• (p Reveal)'}")
        self.query_one("#results", Static).update("\n".join(lines))
        failed = any(not result.success for result in self.results)
        footer = "Esc Back  ? Help"
        if any(result.secret for result in self.results):
            footer += "  p Hide passwords" if self.revealed else "  p Reveal passwords"
        if failed and self.retry_callback:
            footer += "  r Retry failures"
        if self.busy:
            footer = "Applying reviewed changes…\nPlease wait for per-target results."
        self.query_one(ContextFooter).update(footer + "\nPasswords are cleared when this screen closes." if any(r.secret for r in self.results) else footer)

    def check_action(self, action, parameters):
        if self.busy:
            return False
        if action == "retry":
            return self.retry_callback is not None and any(not result.success for result in self.results)
        if action == "reveal":
            return any(result.secret for result in self.results)
        return True

    def action_reveal(self):
        self.revealed = not self.revealed
        self.render_results()

    @work(group="retry-writes", exclusive=True)
    async def action_retry(self):
        self.busy = True
        try:
            for index, result in enumerate(self.results):
                if not result.success:
                    self.results[index] = await self.retry_callback(result.target)
                    self.render_results()
        finally:
            self.busy = False
            self.render_results()

    def action_back(self):
        for result in self.results:
            result.secret = ""
        self.query_one("#results", Static).update("")
        self.app.pop_screen()
        if self.close_callback:
            self.close_callback()

    def on_unmount(self):
        for result in self.results:
            result.secret = ""
