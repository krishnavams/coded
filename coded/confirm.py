"""Yes/no questions asked outside a turn, in the same panel as everything else.

Permission prompts and `ask_user` use `choice.Chooser`; so do the questions
coded asks before work starts — trust this project, run the checks, let a
mission run shell commands, install a plugin, apply a lab proposal, the setup
wizard's yes/no. On a terminal the panel is driven key by key (↑/↓, 1/2,
Enter, Esc), exactly like a permission prompt. Without one (a pipe, a test, an
injected `read`) it is printed with numbers and answered by a line, where
`y`/`n` and an empty line (the default) also work.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional

from rich.markup import escape

from coded import choice, ui


def yes_no(title: str, question: str, *, detail: str = "", yes: str = "Yes",
           no: str = "No", yes_hint: str = "", no_hint: str = "", default: bool = False,
           read: Optional[Callable[[str], str]] = None) -> bool:
    """Ask one yes/no question in the choice panel. Esc, Ctrl-C and end of
    input answer no. `read` (a line reader) forces line mode."""
    q = choice.Question(text=question, options=[choice.Option(yes, yes_hint),
                                                choice.Option(no, no_hint)], other=False)
    chooser = choice.Chooser([q], review=False, title=title, detail=detail,
                             style="permission")
    chooser.cursor[0] = 0 if default else 1
    run(chooser, read=read, default=0 if default else 1)
    answers = chooser.result()
    picked = bool(answers and answers[0].labels and answers[0].labels[0] == yes)
    _record(title or question, yes if picked else no)
    return picked


def run(chooser: choice.Chooser, read: Optional[Callable[[str], str]] = None,
        default: int = 0) -> None:
    """Drive `chooser` until it is done: keys on a terminal, else typed lines."""
    from coded.typeahead import KEY

    reader = None if read is not None else _key_reader()
    if reader is None:
        _run_lines(chooser, read or input, default)
        return
    from rich.live import Live

    live = Live(chooser.render(), console=ui.console, refresh_per_second=20, transient=True)
    try:
        reader.keys_mode = True
        live.start()
        while not chooser.done:
            key = reader(0.1)
            if key is None or not key.startswith(KEY):
                continue
            chooser.key(key[len(KEY):])
            live.update(chooser.render())
    except (KeyboardInterrupt, EOFError):
        chooser.done = chooser.cancelled = True
    finally:
        live.stop()
        close = getattr(reader, "close", None)
        if close is not None:
            close()


def _run_lines(chooser: choice.Chooser, read: Callable[[str], str], default: int) -> None:
    ui.gap()
    ui.console.print(chooser.render(line_mode=True))
    q = chooser.questions[0]
    while not chooser.done:
        try:
            text = read(f"  Answer (1-{len(q.options)}): ").strip()
        except (EOFError, KeyboardInterrupt):
            chooser.done = chooser.cancelled = True
            return
        low = text.lower()
        if low in ("", "y", "yes", "n", "no") and len(q.options) == 2:
            text = str({"": default + 1, "y": 1, "yes": 1, "n": 2, "no": 2}[low])
        before = (chooser.tab, chooser.done)
        chooser.line(text)
        if (chooser.tab, chooser.done) == before:
            ui.console.print(f"  Type a number from 1 to {len(q.options)}.", markup=False)


def _key_reader():
    """The type-ahead reader in key mode, or None where keys cannot be read."""
    try:
        if not sys.stdin or not sys.stdin.isatty() or not sys.stdout.isatty():
            return None
    except (AttributeError, ValueError):
        return None
    from coded.typeahead import default_poll

    reader = default_poll()
    return reader if reader is not None and hasattr(reader, "keys_mode") else None


def _record(question: str, answer: str) -> None:
    """Leave the answer in the scrollback (the live panel is transient)."""
    line = " ".join(question.split())
    ui.console.print(f"[{ui.MUTED}]  {escape(line[:90])} →[/] [bold]{escape(answer)}[/]")
