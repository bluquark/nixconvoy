"""What a run shows on a terminal: its steps as rows redrawn in place, each
ending in one line, in color.  Off one (an agent's pipe, a log) a step
prints its lines as they come and nothing is redrawn."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from typing import IO, Any

from . import strings

# Whether start() took the terminal.
LIVE = False
# Guards the screen: the rows on it, and every write beside them.
_LOCK = threading.RLock()
_ROWS: list[Step] = []
_DRAWN = 0  # rows on the screen now
_MIDLINE = False  # the last write ended inside a line: no row goes below it
_PAUSED = 0
_STOPPED = threading.Event()
# The run's phase and when it began: the row shown while no step has one.
_PHASE: tuple[str, float] | None = None
_LOCAL = threading.local()
# What a note, a warning and die()'s message start with (strings.py), each
# in its color on a terminal.
TINTS = (("warning:", "yellow"), ("note:", "cyan"), (strings.PROC_DIE(msg="").rstrip(), "bold red"))
# What a row's one line leaves out of its step's latest output: escape
# sequences and control characters, and a store path's hash.
UNSHOWN = re.compile(r"\033\[[0-9;?]*[A-Za-z]|[\x00-\x1f\x7f]|(?<=/nix/store/)[a-z0-9]{32}-")


def painter(stream: IO[str] | None = None) -> Callable[[str, str], str]:
    """paint(text, style) in SGR codes when `stream` (stdout) is a terminal
    (NO_COLOR unset), else the text as is: bold, dim, gray, green, yellow,
    red, cyan, magenta."""
    if not (stream or sys.stdout).isatty() or os.environ.get("NO_COLOR"):
        return lambda text, _style: text
    codes = {"bold": "1", "dim": "2", "gray": "90", "green": "32", "yellow": "33", "red": "31",
             "cyan": "36", "magenta": "35"}

    def paint(text: str, style: str) -> str:
        return "\033[" + ";".join(codes[c] for c in style.split()) + "m" + text + "\033[0m"
    return paint


def paint(text: str, style: str) -> str:
    """`text` in `style` (painter) once the terminal is taken, else as is."""
    return _PAINT(text, style) if LIVE else text


def cells(text: str) -> int:
    """The columns `text` takes on a terminal."""
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def clip(text: str, room: int) -> str:
    """`text`, cut short with an ellipsis to fit `room` columns."""
    if cells(text) <= room:
        return text
    fits, used = "", 1  # the ellipsis
    for c in text:
        used += cells(c)
        if used > room:
            break
        fits += c
    return fits + "…" if room > 0 else ""


class Step:
    """One thing under way (step): on a terminal a row - a spinner, the
    label, the seconds so far and the latest line of its output - then the
    one line it ends in."""

    def __init__(self, label: str, done: str, keep: re.Pattern[str] | None, part: bool) -> None:
        self.label, self.done, self.keep, self.part = label, done, keep, part
        self.tail = ""
        self.lines: list[str] = []
        self.t0 = time.monotonic()
        self.open = True
        self.ok = True  # cleared by a block that fails without raising
        _steps().append(self)
        if LIVE:
            with _LOCK:
                _ROWS.append(self)
                _redraw()
        elif label:
            print(label)

    def say(self, line: str) -> None:
        """A line of a core's: one ending in ... is what the step is at
        now, its row's label on a terminal; any other is printed."""
        if LIVE and line.endswith("..."):
            self.label, self.tail = UNSHOWN.sub("", line[:-3]), ""
        else:
            print(line, flush=True)

    def show(self, line: str) -> None:
        """A line of output kept elsewhere: the row's latest."""
        self.tail = UNSHOWN.sub("", line).strip()

    def log(self, line: str) -> None:
        """A line of the step's output: printed, or on a terminal kept
        (`lines`) and shown as the row's latest - the step failing prints
        them all, one ending well those `keep` finds."""
        if LIVE:
            self.lines.append(line)
            self.show(line)
        else:
            print(line, flush=True)

    def row(self, now: float, width: int, frame: str) -> str:
        secs = strings.TERM_ELAPSED(secs=now - self.t0)
        label = clip(self.label, width - cells(secs) - 5)
        tail = clip(self.tail, width - cells(label) - cells(secs) - 7)
        return (f"{paint(frame, 'cyan')} {label}  {paint(secs, 'dim')}"
                + (f"  {paint(tail, 'dim')}" if tail else ""))

    def end(self, failed: bool = False, told: bool = True) -> None:
        """The row gone, and unless not `told` the line the step ends in
        (step)."""
        if not self.open:
            return
        self.open = False
        with contextlib.suppress(ValueError):
            _steps().remove(self)
        if not LIVE:
            return
        with _LOCK:
            if self in _ROWS:
                _ROWS.remove(self)
        took = paint(strings.TERM_TOOK(secs=time.monotonic() - self.t0), "dim")
        indent = "    " if self.label else ""
        out: list[str] = []
        if told and failed:
            if self.label:
                out.append(f"{paint(strings.TERM_FAILED, 'red')} {self.label}  {took}")
            out += [indent + ln for ln in self.lines]
        elif told and not self.part:
            if self.done or self.label:
                out.append(f"{paint(strings.TERM_OK, 'green')} {self.done or self.label}  {took}")
            if self.keep is not None:
                out += [paint(indent + ln, "dim") for ln in self.lines if self.keep.search(ln)]
        if out:
            print("\n".join(out), flush=True)
        else:
            with _LOCK:
                _redraw()


def _steps() -> list[Step]:
    """The calling thread's open steps, the innermost last."""
    if not hasattr(_LOCAL, "steps"):
        _LOCAL.steps = []
    return _LOCAL.steps


@contextlib.contextmanager
def step(label: str = "", done: str = "", keep: re.Pattern[str] | None = None,
         part: bool = False) -> Iterator[Step]:
    """A step of the run, the calling thread's while the block runs: off a
    terminal `label` printed and no more; on one a row (Step; none while it
    has neither a label nor output), ending in `done` (default: the label)
    with the time it took - in no line for a `part` of a step another
    thread ends - or, the block dying (die(): fail) or clearing the step's
    `ok`, in the label marked failed and all its kept output.  A block
    raising otherwise (a sibling's failure sweeping it, a signal) ends it
    in no line: the failure is another's to say."""
    s = Step(label.removesuffix("...") if LIVE else label, done, keep, part)
    try:
        yield s
    except BaseException:
        s.end(told=False)
        raise
    s.end(failed=not s.ok)


def current() -> Step | None:
    """The calling thread's innermost step on a terminal, else None."""
    steps = _steps()
    return steps[-1] if LIVE and steps else None


def fail() -> None:
    """The calling thread's steps ended as failed, for good - whatever
    catches die()'s Fail - ahead of the message die() prints below their
    output."""
    for s in reversed(list(_steps())):
        s.end(failed=True)


def phase(name: str) -> None:
    """The run's phase (proc.Perf.mark), shown while no step has a row."""
    global _PHASE
    _PHASE = (name, time.monotonic()) if name != "end" else None


def good(text: str) -> str:
    """A verdict that something ended well, its first line marked so on a
    terminal."""
    first, newline, rest = text.partition("\n")
    return f"{paint(strings.TERM_OK, 'green')} {paint(first, 'bold')}{newline}{rest}" if LIVE else text


def line(template: strings.M, style: str = "", **fields: object) -> str:
    """`template` filled with `fields`, the first of them (what the line
    is about) bold on a terminal and the rest in `style`."""
    about = next(iter(fields))
    return template(**{k: paint(str(v), "bold" if k == about else style) if k == about or style else v
                       for k, v in fields.items()})


def _rows() -> list[str]:
    size = shutil.get_terminal_size()
    now = time.monotonic()
    frame = strings.TERM_SPINNER[int(now * 10) % len(strings.TERM_SPINNER)]
    live = [s for s in _ROWS if s.label or s.tail]
    if live:
        return [s.row(now, size.columns, frame) for s in live[:max(size.lines - 2, 1)]]
    if _PHASE is not None:
        name, t0 = _PHASE
        secs = strings.TERM_ELAPSED(secs=now - t0)
        return [f"{paint(frame, 'cyan')} {paint(clip(name, size.columns - cells(secs) - 5) + '  ' + secs, 'dim')}"]
    return []


def _redraw(show: bool = True) -> None:
    """The rows as they are now in place of those on the screen (none
    unless `show`), under _LOCK."""
    global _DRAWN
    rows = _rows() if show and not (_PAUSED or _MIDLINE or _STOPPED.is_set()) else []
    if not (_DRAWN or rows):
        return
    # Each line erased by itself: erasing down the screen from its top
    # row is a clear to some terminals, which scroll it into their history.
    spare = max(_DRAWN - len(rows), 0)
    # No wrapping meanwhile: a row wider than the screen after all (a
    # window narrowed since) is cut, not spread over lines never erased.
    _TTY.write("\033[?7l" + (f"\033[{_DRAWN}A\r" if _DRAWN else "") + "".join(r + "\033[K\n" for r in rows)
               + "\033[K\n" * spare + (f"\033[{spare}A" if spare else "") + "\033[?7h")
    _TTY.flush()
    _DRAWN = len(rows)


class Above:
    """stdout or stderr once the terminal is taken: what is written lands
    above the rows, a note's, a warning's or die()'s first word (TINTS)
    in its color on stderr.  A thread's text is held until its line ends
    (print writes the newline apart) or a flush, so no two threads' lines
    splice."""

    def __init__(self, stream: IO[str], tinted: bool) -> None:
        self.stream, self.tinted = stream, tinted
        self.held: dict[int, str] = {}  # by thread

    def write(self, text: str) -> int:
        me = threading.get_ident()
        with _LOCK:
            lines, newline, rest = (self.held.pop(me, "") + text).rpartition("\n")
            if rest:
                self.held[me] = rest
            self.put(lines + newline)
        return len(text)

    def flush(self, every: bool = False) -> None:
        """The calling thread's held text written (`every` thread's)."""
        with _LOCK:
            for thread in list(self.held) if every else [threading.get_ident()]:
                self.put(self.held.pop(thread, ""))
            self.stream.flush()

    def put(self, text: str) -> None:
        global _MIDLINE
        if not text:
            return
        with _LOCK:
            _redraw(show=False)
            said = text
            if self.tinted and not _MIDLINE:
                for prefix, style in TINTS:
                    if text.startswith(prefix):
                        said = paint(prefix, style) + text[len(prefix):]
            self.stream.write(said)
            self.stream.flush()
            _MIDLINE = not text.endswith("\n")
            _redraw()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


def _tick() -> None:
    while not _STOPPED.wait(0.1):
        with _LOCK:
            _redraw()


def start() -> None:
    """Take the terminal for the run, when stdout and stderr both are one
    that redraws (and takes the marks: UTF-8): LIVE from here on, both
    streams writing above the rows (Above)."""
    global LIVE, _TTY, _PAINT
    if LIVE or not (sys.stdout.isatty() and sys.stderr.isatty()) or os.environ.get("TERM") == "dumb" \
            or "utf" not in (sys.stdout.encoding or "").lower():
        return
    _TTY, _PAINT = sys.stdout, painter(sys.stdout)
    LIVE = True
    _STOPPED.clear()
    sys.stdout, sys.stderr = Above(sys.stdout, False), Above(sys.stderr, True)  # type: ignore[assignment]
    threading.Thread(target=_tick, daemon=True).start()


def stop() -> None:
    """The rows off the screen for good, the streams as they were; LIVE
    stays, for what is still printed to be styled."""
    if not LIVE or _STOPPED.is_set():
        return
    with _LOCK:
        _STOPPED.set()
        _redraw(show=False)
        for name in ("stdout", "stderr"):
            s = getattr(sys, name)
            if isinstance(s, Above):
                s.flush(every=True)
                setattr(sys, name, s.stream)


@contextlib.contextmanager
def paused() -> Iterator[None]:
    """The rows off the screen while something else has the terminal (an
    editor, a command writing to it itself)."""
    global _PAUSED
    with _LOCK:
        _PAUSED += 1
        if LIVE:
            _redraw(show=False)
    try:
        yield
    finally:
        with _LOCK:
            _PAUSED -= 1
            if LIVE:
                _redraw()


def relay(pipe: IO[str] | None) -> Callable[[], None]:
    """A child's stderr, piped (PIPE) because the terminal is taken, said
    through this process's from a thread of its own: above the rows, not
    across them.  What it returns waits (a second at most) for all the
    child wrote to be said, once the child is over: before the failure
    that text explains is told."""
    if pipe is None:
        return lambda: None

    def pump() -> None:
        with pipe:
            for line in pipe:
                sys.stderr.write(line)
    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return lambda: thread.join(1)


def child_stderr() -> int | None:
    """What a child whose stderr is not captured gets for it: this
    process's, or on a terminal a pipe for relay."""
    return subprocess.PIPE if LIVE else None


_TTY: IO[str] = sys.stdout
_PAINT = painter(sys.stdout)
