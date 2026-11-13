"""An interactive choice prompt shared by the REPL and the TUI.

Used for permission prompts ("Do you want to proceed?") and for the agent's
questions (`ask_user`), including several questions at once with a review page
before they are submitted. The state machine is pure: front-ends feed it key
tokens (`key()`) or typed lines (`line()`, for terminals that cannot send keys)
and draw `render()`; nothing here reads the terminal.

Keys: ↑/↓ move, Enter selects, a digit picks that option, ←/→ or Tab switch
between questions, Space toggles in a multi-select question, Esc cancels.
On the "Type something" row, typed characters go into the free answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Union

UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
ENTER, ESCAPE, TAB, BACKTAB, BACKSPACE = "enter", "escape", "tab", "shift_tab", "backspace"
CLEAR = "ctrl_u"
KEYS = frozenset({UP, DOWN, LEFT, RIGHT, ENTER, ESCAPE, TAB, BACKTAB, BACKSPACE, CLEAR})

MAX_QUESTIONS = 4
MAX_OPTIONS = 6
OTHER_LABEL = "Type something."


@dataclass
class Option:
    label: str
    description: str = ""


@dataclass
class Question:
    text: str
    options: List[Option] = field(default_factory=list)
    header: str = ""
    multi_select: bool = False
    # A free-answer row after the options ("Type something.").
    other: bool = True
    other_label: str = OTHER_LABEL
    # Enter on an empty free-answer row counts (a permission "No" needs no reason).
    other_empty_ok: bool = False

    @classmethod
    def make(cls, text: str, options: Sequence[Union[str, dict, Option]] = (),
             header: str = "", multi_select: bool = False) -> "Question":
        opts: List[Option] = []
        for o in options:
            if isinstance(o, Option):
                opt = o
            elif isinstance(o, dict):
                opt = Option(str(o.get("label") or "").strip(),
                             str(o.get("description") or "").strip())
            else:
                opt = Option(str(o).strip())
            if opt.label:
                opts.append(opt)
        return cls(text=text.strip(), options=opts[:MAX_OPTIONS], header=header.strip(),
                   multi_select=multi_select)


@dataclass
class Answer:
    labels: List[str] = field(default_factory=list)
    text: Optional[str] = None  # the free answer, when that row was chosen
    answered: bool = False

    def __str__(self) -> str:
        parts = list(self.labels)
        if self.text:
            parts.append(self.text)
        return ", ".join(parts)


class Chooser:
    """One or more questions; finished when `done` (then `cancelled` or `answers`)."""

    def __init__(self, questions: List[Question], *, review: Optional[bool] = None,
                 title: str = "", detail: str = "", style: str = "question"):
        if not questions:
            raise ValueError("at least one question")
        self.questions = questions
        self.review = len(questions) > 1 if review is None else review
        self.title = title
        self.detail = detail
        self.style = style  # "question" or "permission" (only the colour differs)
        self.tab = 0
        self.cursor = [0] * len(questions)
        self.checked: List[set] = [set() for _ in questions]
        self.texts = [""] * len(questions)
        self.answers = [Answer() for _ in questions]
        self.review_cursor = 0
        self.done = False
        self.cancelled = False

    # -- geometry -------------------------------------------------------------
    @property
    def on_review(self) -> bool:
        return self.review and self.tab == len(self.questions)

    def _rows(self, qi: int) -> int:
        q = self.questions[qi]
        return len(q.options) + (1 if q.other else 0) + (1 if q.multi_select else 0)

    def _other_row(self, qi: int) -> Optional[int]:
        q = self.questions[qi]
        return len(q.options) if q.other else None

    def _done_row(self, qi: int) -> Optional[int]:
        q = self.questions[qi]
        return self._rows(qi) - 1 if q.multi_select else None

    @property
    def editing(self) -> bool:
        """The cursor is on the free-answer row: typed keys go into its text."""
        return not self.on_review and self.cursor[self.tab] == self._other_row(self.tab)

    # -- moving ---------------------------------------------------------------
    def _last_tab(self) -> int:
        return len(self.questions) if self.review else len(self.questions) - 1

    def _go(self, tab: int) -> None:
        self.tab = max(0, min(tab, self._last_tab()))

    def _advance(self) -> None:
        """After answering a question: the next one, the review page, or finish."""
        if self.tab < len(self.questions) - 1:
            self.tab += 1
        elif self.review:
            self.tab = len(self.questions)
            self.review_cursor = 0
        else:
            self.done = True

    def _record(self, qi: int) -> None:
        q = self.questions[qi]
        labels = [q.options[i].label for i in sorted(self.checked[qi]) if i < len(q.options)]
        text = self.texts[qi].strip() if (len(q.options) in self.checked[qi]) else None
        self.answers[qi] = Answer(labels, text or None, bool(labels or text))

    # -- input ----------------------------------------------------------------
    def key(self, k: str) -> None:
        if self.done:
            return
        if k == ESCAPE:
            self.done = self.cancelled = True
            return
        if self.on_review:
            self._review_key(k)
            return
        qi = self.tab
        q = self.questions[qi]
        rows = self._rows(qi)
        if k == UP:
            self.cursor[qi] = (self.cursor[qi] - 1) % rows
        elif k == DOWN:
            self.cursor[qi] = (self.cursor[qi] + 1) % rows
        elif k in (TAB, RIGHT) and not (k == RIGHT and self.editing and self.texts[qi]):
            self._go(self.tab + 1)
        elif k in (BACKTAB, LEFT) and not (k == LEFT and self.editing and self.texts[qi]):
            self._go(self.tab - 1)
        elif k == ENTER:
            self._enter(qi)
        elif self.editing and k == BACKSPACE:
            self.texts[qi] = self.texts[qi][:-1]
        elif self.editing and k == CLEAR:
            self.texts[qi] = ""
        elif self.editing and k not in KEYS:
            self.texts[qi] += k
            if q.multi_select:
                other = self._other_row(qi)
                self.checked[qi].add(other)
                self._record(qi)
        elif k.isdigit() and k != "0" and int(k) <= len(q.options):
            self.cursor[qi] = int(k) - 1
            if q.multi_select:
                self._toggle(qi, int(k) - 1)
            else:
                self._enter(qi)
        elif k.isdigit() and q.other and int(k) == len(q.options) + 1:
            # The free row's number: focus it to type, or (a permission "No")
            # answer at once.
            self.cursor[qi] = len(q.options)
            if q.other_empty_ok and not q.multi_select:
                self._enter(qi)
        elif k == " " and q.multi_select and self.cursor[qi] < len(q.options):
            self._toggle(qi, self.cursor[qi])

    def _toggle(self, qi: int, row: int) -> None:
        self.checked[qi] ^= {row}
        self._record(qi)

    def _enter(self, qi: int) -> None:
        q = self.questions[qi]
        row = self.cursor[qi]
        if q.multi_select:
            if row == self._done_row(qi):
                self._record(qi)
                self.answers[qi].answered = True
                self._advance()
            elif row == self._other_row(qi):
                if self.texts[qi].strip():
                    self.checked[qi].add(row)
                    self._record(qi)
            else:
                self._toggle(qi, row)
            return
        if row == self._other_row(qi):
            if not self.texts[qi].strip() and not q.other_empty_ok:
                return
            self.checked[qi] = {row}
        else:
            self.checked[qi] = {row}
        self._record(qi)
        self.answers[qi].answered = True
        self._advance()

    def _review_key(self, k: str) -> None:
        if k in (UP, DOWN):
            self.review_cursor ^= 1
        elif k in (LEFT, BACKTAB):
            self._go(self.tab - 1)
        elif k == "1":
            self.done = True
        elif k == "2":
            self.done = self.cancelled = True
        elif k == ENTER:
            self.done = True
            self.cancelled = self.review_cursor == 1

    def line(self, text: str) -> None:
        """A whole typed line, for terminals without key-by-key input: a number
        (or numbers, for a multi-select) picks options, other text is the free
        answer, an empty line on the review page submits."""
        text = text.strip()
        if self.on_review:
            self._review_key({"": ENTER, "1": "1", "2": "2", "y": "1", "n": "2"}.get(
                text.lower(), "?"))
            return
        qi = self.tab
        q = self.questions[qi]
        picks = [p for p in text.replace(",", " ").split() if p]
        if picks and all(p.isdigit() and 1 <= int(p) <= len(q.options) for p in picks):
            if q.multi_select:
                self.checked[qi] = {int(p) - 1 for p in picks}
                self._record(qi)
                self.answers[qi].answered = True
                self._advance()
            else:
                self.cursor[qi] = int(picks[0]) - 1
                self._enter(qi)
            return
        if text and q.other or (not text and q.other_empty_ok):
            self.texts[qi] = text
            self.checked[qi] = {self._other_row(qi)} if q.other else set()
            self._record(qi)
            self.answers[qi].answered = True
            self._advance()
        elif not text:
            self.answers[qi] = Answer()  # skipped
            self._advance()

    # -- result ---------------------------------------------------------------
    def result(self) -> Optional[List[Answer]]:
        """The answers once done, or None if cancelled (Esc / Cancel)."""
        if not self.done or self.cancelled:
            return None
        return self.answers

    # -- drawing --------------------------------------------------------------
    def render(self, line_mode: bool = False):
        """The prompt as a rich renderable (the same in the REPL and the TUI)."""
        from rich.box import ROUNDED
        from rich.console import Group
        from rich.panel import Panel
        from rich.text import Text

        from coded import ui

        accent = ui.WARN if self.style == "permission" else ui.ACCENT
        parts = []
        if self.review:
            parts.append(self._tabs(accent))
            parts.append(Text(""))
        if self.detail:
            parts.append(Text(self.detail.rstrip("\n"), style="white"))
            parts.append(Text(""))
        if self.on_review:
            parts += self._review_body(accent)
        else:
            parts += self._question_body(self.tab, accent, line_mode)
        parts.append(Text(""))
        parts.append(Text(self._hint(line_mode), style=ui.MUTED))
        title = self.title or ("Question" if not self.review else "Questions")
        return Panel(Group(*parts), title=f"[bold {accent}]{_esc(title)}[/]", title_align="left",
                     border_style=accent, box=ROUNDED, padding=(0, 1), expand=True)

    def _tabs(self, accent: str):
        from rich.text import Text

        from coded import ui

        t = Text("← ", style=ui.MUTED)
        for i, q in enumerate(self.questions):
            mark = "☒" if self.answers[i].answered else "☐"
            name = q.header or f"Q{i + 1}"
            style = f"bold reverse {accent}" if i == self.tab else (
                ui.ACCENT2 if self.answers[i].answered else "white")
            t.append(f" {mark} {name} ", style=style)
            t.append(" ")
        style = f"bold reverse {accent}" if self.on_review else "white"
        t.append(" ✔ Submit ", style=style)
        t.append(" →", style=ui.MUTED)
        return t

    def _question_body(self, qi: int, accent: str, line_mode: bool) -> list:
        from rich.text import Text

        from coded import ui

        q = self.questions[qi]
        out = [Text(q.text, style="bold")]
        cur = None if line_mode else self.cursor[qi]
        for i, opt in enumerate(q.options):
            row = Text()
            here = cur == i
            row.append("❯ " if here else "  ", style=f"bold {accent}")
            if q.multi_select:
                row.append("[✔] " if i in self.checked[qi] else "[ ] ",
                           style=ui.ACCENT2 if i in self.checked[qi] else ui.MUTED)
            row.append(f"{i + 1}. ", style=accent if here else ui.MUTED)
            row.append(opt.label, style=f"bold {accent}" if here else "white")
            out.append(row)
            if opt.description:
                out.append(Text(f"     {opt.description}", style=ui.MUTED))
        other = self._other_row(qi)
        if other is not None:
            row = Text()
            here = cur == other
            row.append("❯ " if here else "  ", style=f"bold {accent}")
            if q.multi_select:
                on = other in self.checked[qi]
                row.append("[✔] " if on else "[ ] ", style=ui.ACCENT2 if on else ui.MUTED)
            row.append(f"{other + 1}. ", style=accent if here else ui.MUTED)
            if self.texts[qi] or here:
                if q.other_empty_ok and self.texts[qi]:
                    row.append("No: ", style=f"bold {accent}" if here else "white")
                row.append(self.texts[qi], style="white")
                if here:
                    row.append("▌", style=accent)
                if not self.texts[qi]:
                    row.append(q.other_label, style=f"italic {ui.MUTED}")
            else:
                row.append(q.other_label, style="white")
            out.append(row)
        done = self._done_row(qi)
        if done is not None:
            here = cur == done
            row = Text("❯ " if here else "  ", style=f"bold {accent}")
            row.append("Next →" if qi < len(self.questions) - 1 or self.review else "Done",
                       style=f"bold {accent}" if here else ui.MUTED)
            out.append(row)
        return out

    def _review_body(self, accent: str) -> list:
        from rich.text import Text

        from coded import ui

        out = [Text("Review your answers", style="bold")]
        missing = False
        for q, a in zip(self.questions, self.answers):
            row = Text(" ● ", style=accent)
            row.append(q.text, style="white")
            out.append(row)
            if a.answered and str(a):
                out.append(Text(f"   → {a}", style=ui.ACCENT2))
            else:
                missing = True
                out.append(Text("   → (not answered)", style=ui.WARN))
        out.append(Text(""))
        if missing:
            out.append(Text("⚠ Some questions are not answered.", style=ui.WARN))
        out.append(Text("Ready to submit your answers?", style="bold"))
        for i, label in enumerate(("Submit answers", "Cancel")):
            here = self.review_cursor == i
            row = Text("❯ " if here else "  ", style=f"bold {accent}")
            row.append(f"{i + 1}. ", style=accent if here else ui.MUTED)
            row.append(label, style=f"bold {accent}" if here else "white")
            out.append(row)
        return out

    def _hint(self, line_mode: bool) -> str:
        if line_mode:
            if self.on_review:
                return "Enter or 1 to submit · 2 to cancel"
            q = self.questions[self.tab]
            if q.multi_select:
                return "numbers separated by spaces, or your own answer · Enter to skip"
            if self.style == "permission":
                return "a number (y/n/a also work)"
            return "a number or your own answer · Enter to skip"
        if self.on_review:
            return "Enter to select · ↑/↓ to move · ← to go back · Esc to cancel"
        q = self.questions[self.tab]
        nav = " · Tab/←/→ between questions" if self.review else ""
        pick = "Space/Enter to toggle" if q.multi_select else "Enter to select"
        return f"{pick} · ↑/↓ to move{nav} · Esc to cancel"


def _esc(text: str) -> str:
    return text.replace("[", r"\[")


# ---------------------------------------------------------------------------
# Permission prompts
# ---------------------------------------------------------------------------
YES, ALWAYS, NO = "Yes", "Yes, and don't ask again this session", "No"


def permission_chooser(title: str, detail: str) -> Chooser:
    """"Do you want to proceed?" — the approval prompt for a risky action."""
    q = Question(text="Do you want to proceed?",
                 options=[Option(YES), Option(f"{ALWAYS} ({title.split(' — ')[-1].lower()})")],
                 other=True, other_label="No, and tell coded what to do differently (esc)",
                 other_empty_ok=True)
    return Chooser([q], review=False, title=title, detail=detail, style="permission")


def permission_answer(chooser: Chooser) -> str:
    """The prompt callback's answer: "yes", "always", "no" or "no: <feedback>"."""
    answers = chooser.result()
    if not answers:
        return "no"
    a = answers[0]
    if a.labels and a.labels[0] == YES:
        return "yes"
    if a.labels and a.labels[0].startswith(ALWAYS):
        return "always"
    return f"no: {a.text}" if a.text else "no"


def permission_line(answer: str) -> Optional[str]:
    """A typed answer to a permission prompt in line mode; None if not one."""
    a = answer.strip().lower()
    if a in ("y", "yes", "n", "no", "a", "always"):
        return answer.strip()
    return {"1": "yes", "2": "always", "3": "no"}.get(a)
