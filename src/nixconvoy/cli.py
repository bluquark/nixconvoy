"""The nixconvoy command line; its synopsis (SYNOPSIS) is in strings.py."""

from __future__ import annotations

import atexit
import contextlib
import os
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import IO, NoReturn

from . import commands, config, hooks, init, names, proc, review, strings, submodule
from .checkout import (
    await_background_push,
    lock_checkout,
    lock_holder,
    note_prefix,
    pop_tmp_commits,
    require_work_branch,
    sub_parent,
)
from .deploy import deploy, refuse_staged
from .proc import PERF, TOOL, Fail, die, ok, out, painter
from .strings import BACK_TO_FLAG, DRY_FLAG, EVICT_FLAG


def usage(stream: IO[str] | None = None) -> str:
    """The synopsis in its sections, colored when `stream` (stdout) is a
    terminal: the headings and the subcommand bold, flags yellow and the value a flag takes cyan,
    positional values green; inside [...] the brackets, "..." and "|"
    gray."""
    paint = painter(stream)
    depth = 0

    def color(m: re.Match[str]) -> str:
        nonlocal depth
        if m.group("open"):
            depth += 1
            return paint("[", "gray")
        if m.group("close"):
            depth -= 1
            return paint("]", "gray")
        if m.group("mark"):
            return paint(m.group("mark"), "gray") if depth else m.group("mark")
        if m.group("flag"):
            return paint(m.group("flag"), "yellow") + (
                " " + paint(m.group("fval"), "cyan") if m.group("fval") else "")
        return paint(m.group("pos"), "green")

    lines = [strings.USAGE_HEADING]
    for form in strings.SYNOPSIS.splitlines():
        if form.startswith("= "):
            lines += ["", paint(form[2:] + ":", "bold")]
            continue
        sub, _, rest = form.partition(" ")
        rest = re.sub(r"(?P<open>\[)|(?P<close>\])|(?P<mark>\.\.\.|\|)"
                      r"|(?P<flag>(?<![\w<])--?\w[\w-]*)(?: (?P<fval><[\w-]+>))?|(?P<pos>@?<[\w-]+>)", color, rest)
        lines.append(f"  {TOOL} {paint(sub, 'bold')} {rest}".rstrip())
    return "\n".join(lines)


def bad_usage() -> NoReturn:
    sys.stdout.flush()
    print(usage(sys.stderr), file=sys.stderr)
    sys.exit(1)


# Each subcommand's flags, True for one taking a value (never one starting
# with -), and whether it takes positionals.  A value flag given twice: the
# last counts.
FLAGS: dict[str, tuple[dict[str, bool], bool]] = {
    "deploy": ({"-m": True, EVICT_FLAG: False, DRY_FLAG: False}, True),
    "land": ({"-m": True, "--with-children": False, "--not-yet-deployed": False}, True),
    "undeploy": ({"--with-children": False, BACK_TO_FLAG: True, DRY_FLAG: False}, True),
    "close": ({"--force": False}, True),
    "open": ({"--submodule": False, "--child": False, "--branch": True}, True),
    "pull": ({}, False),
    "status": ({}, False),
}


def main() -> None:
    try:
        dispatch()
    except Fail:  # said by die()
        sys.exit(1)


def dispatch() -> None:
    args = sys.argv[1:]
    if not args:
        with contextlib.suppress(metadata.PackageNotFoundError):  # run from the source tree
            rev = Path(__file__).with_name("rev")  # written by nix/package.nix
            print(strings.CLI_VERSION(version=metadata.version(TOOL),
                                      rev=f" ({rev.read_text().strip()})" if rev.exists() else ""))
        print(usage())
        return
    if args[0] not in ("deploy", "land", "undeploy", "close", "pull", "status", "review", "open", "hook", "init"):
        bad_usage()
    subcmd = args.pop(0)
    if subcmd == "hook":
        # Where Claude Code runs it, which need not be a repository.
        if len(args) != 1 or args[0] not in hooks.EVENTS:
            bad_usage()
        hooks.run_hook(args[0])
        return
    start = Path.cwd()
    os.chdir(out(["git", "rev-parse", "--show-toplevel"]))
    config.set_root(Path.cwd())
    if subcmd == "init":
        # Before anything reads nixconvoy.toml, which it writes.
        init_args(args, start)
    note_prefix(Path.cwd())
    if subcmd == "init":
        return
    if subcmd == "review":
        os.chdir(start)  # its paths are the caller's
        sys.exit(review.main(args))
    if subcmd not in ("status", "close", "open"):
        atexit.register(PERF.write, Path.cwd())
    takes, positionals = FLAGS[subcmd]
    flags: dict[str, list[str]] = {}
    given: list[str] = []
    while args:
        a = args.pop(0)
        if a in takes:
            values = flags.setdefault(a, [])
            if takes[a]:
                if not args or args[0].startswith("-"):
                    bad_usage()
                values.append(args.pop(0))
        elif positionals and not a.startswith("-"):
            if a not in given:
                given.append(a)
        else:
            bad_usage()
    msg = flags["-m"][-1] if "-m" in flags else None
    back_to = flags[BACK_TO_FLAG][-1] if BACK_TO_FLAG in flags else None
    with_children = "--with-children" in flags
    not_yet_deployed = "--not-yet-deployed" in flags
    dry = DRY_FLAG in flags
    # An undeploy's dry activation: where each branch it names would be.
    as_if: dict[str, str] | None = {} if dry and subcmd == "undeploy" else None

    if subcmd == "pull":
        commands.pull()  # takes each checkout's lock itself, this one included
        return
    if subcmd == "status":
        commands.status()
        return
    if subcmd == "open":
        branch = flags["--branch"][-1] if "--branch" in flags else None
        sub_flag, child = "--submodule" in flags, "--child" in flags
        if len(given) != 1 or child + bool(branch) + sub_flag > 1:
            bad_usage()
        if sub_flag:
            submodule.create(given[0], require_work_branch("open --submodule"))
        elif child:
            commands.open_worktree(given[0], None)
        else:
            commands.open_checkout(start / Path(given[0]).expanduser(), branch)
        return
    if subcmd == "close":
        force = "--force" in flags
        # One kind of name per call: a submodule's (declared: never a
        # worktree's), a path's, else a subagent worktree's.
        declared = names.gitmodule_names(Path.cwd())
        kinds = {"submodule" if n in declared else "path" if is_path(n) else "worktree" for n in given}
        if len(kinds) > 1:
            bad_usage()
        if kinds == {"path"}:
            take_lock()
            for n in given:
                commands.close_checkout(start / Path(n).expanduser(), force=force)
        elif kinds == {"submodule"}:
            take_lock()
            commands.close_submodules(given, force=force)
        else:
            commands.close_worktrees(given, force=force)
        return
    top = Path.cwd()
    here = top.resolve()
    if subcmd in ("land", "undeploy"):
        if (with_children and given) or (msg is not None and (not msg.strip() or len(given) > 1)):
            bad_usage()
        if with_children and subcmd == "land":
            take_lock()
            commands.land_with_children(msg, not_yet_deployed)
            return
        parent_too = False
        if with_children:
            # Each sub holding increments, then this checkout.
            if sub_parent(here) is not None:
                die(strings.UNDEPLOY_CHILDREN_FROM_SUB)
            given = [str(d) for d in commands.subs_of(here) if commands.has_increments(d)]
            parent_too = commands.has_increments(here)
            if not given and not parent_too:
                die(strings.UNDEPLOY_NOTHING_ANYWHERE)
        # Each worktree once, however many of its names were given.
        each: dict[Path, str] = {}
        for n in given:
            each.setdefault(Path(n) if with_children else names.worktree(n, here, subcmd, subagent=False), n)
        given, named = list(each.values()), list(each.keys())
        if subcmd == "land":
            # Several worktrees, one after the other, each under its own
            # checkout's lock.
            for name, d in zip(given, named, strict=True):
                if len(given) > 1:
                    print(strings.CLI_SECTION(name=d.name))
                os.chdir(d)
                lock = lock_checkout()
                if lock is None:
                    die(strings.CLI_RUNNING_IN_WORKTREE(holder=lock_holder(), name=name))
                commands.land(msg, not_yet_deployed)
                lock.close()
            if given:
                return
        else:
            undo = [*named, here] if parent_too or not named else named
            # Another checkout's unfinished landing may take this one too:
            # finished while this one's lock is free.
            if not dry:
                for d in [d for d in undo if d != here]:
                    os.chdir(d)
                    held = lock_checkout()
                    if held is not None:
                        commands.resume_landings()
                        held.close()
                os.chdir(here)
            # What the undeploys make due is deployed from this checkout,
            # without a message: its lock is held from before anything
            # moves, and nothing is staged for that deploy to carry, a dead
            # run's tmp commits unwound.
            take_lock()
            await_background_push()
            pop_tmp_commits()
            refuse_staged(here)
            if not dry:
                commands.resume_landings()
            # One checkout after the other, another's under its own lock.
            # An undeploy that fails in one stops none of the others, and
            # what the rest undid is deployed all the same.
            due, failed = False, []
            for d in undo:
                if len(undo) > 1:
                    print(strings.CLI_SECTION(name=d.name))
                os.chdir(d)
                lock = None
                try:
                    if d != here:
                        lock = lock_checkout()
                        if lock is None:
                            die(strings.CLI_RUNNING_IN_WORKTREE(holder=lock_holder(), name=each[d]))
                    due |= commands.undeploy(back_to or "HEAD^", as_if)
                except Fail:
                    if len(undo) == 1:
                        raise
                    failed.append(d.name)
                finally:
                    if lock is not None:
                        lock.close()
            os.chdir(here)
            if due:
                deploy([], None, False, as_if=as_if)
            if failed:
                die(strings.UNDEPLOY_FAILED_IN(names=", ".join(failed)))
            return
    take_lock()
    if subcmd == "land":
        commands.land(msg, not_yet_deployed)
    else:  # deploy
        if not dry:
            commands.resume_landings()
        if dry and msg is not None:
            # Accepted so a deploy's command line reruns with the flag added.
            print(strings.CLI_DRY_IGNORES_MSG(), file=sys.stderr)
        deploy(given, msg, EVICT_FLAG in flags, dry)


def init_args(args: list[str], start: Path) -> None:
    """init's command line: at most one <flake-dir>, relative to where
    nixconvoy was run."""
    flake: str | None = None
    while args:
        a = args.pop(0)
        if a.startswith("-") or flake is not None:
            bad_usage()
        flake = a
    init.main(flake, start)


def is_path(arg: str) -> bool:
    """A close argument naming an agent checkout by its path rather than a
    subagent worktree by its name (no / in one) or a branch
    (work/<machine>/<branch-suffix> has slashes too): one written as a path, or with a /
    in it when it is no branch of this repository."""
    if arg.startswith(("/", "~", "./", "../")) or arg.endswith("/"):
        return True
    return "/" in arg and not ok(["git", "rev-parse", "-q", "--verify", f"refs/heads/{arg}"])


def take_lock() -> None:
    """The current checkout's deploy lock, held until exit."""
    lock = lock_checkout()
    if lock is None:
        die(strings.CLI_RUNNING_HERE(holder=lock_holder()))
    proc.HELD.append(lock)


if __name__ == "__main__":
    main()
