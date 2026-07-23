#!/usr/bin/env python3
"""sable_runlock_lib — the SUITE-RUN REGISTRY behind hot-swap clearance
(SABLE-pk15w / SABLE-4qlcf).

WHY THIS EXISTS. ~34 installed sable bins are SYMLINKS into the live working
tree (SABLE-y6ik3), so merging one hot-swaps it for every agent on the host
instantly, with no staging. Before such a merge the seat must answer: *** IS
ANY TEST SUITE EXECUTING IN ANY WORKTREE OF THIS REPO? *** That is chuck's
framing and it is the invariant this module owns. It is deliberately NOT the
question "is one of these named runners running" — that question was asked
three times on 2026-07-22 and was wrong twice:

  v1  "is a sable-spawn-worker PROCESS running?"   UNANSWERABLE. `pytest bin/`
      loads the bin IN-PROCESS; it never appears in the process table. Three
      agents wrote three independent probes; all returned zero; all meaningless.
  v2  "is any `pytest bin/` run in flight?"        TOO NARROW — one runner
      class. A `shell-run-set.sh --run` was concurrently EXEC'ing a real
      sable-spawn-worker governance run.
  v3  "pytest bin/ OR shell-run-set.sh --run?"     ANSWERABLE, STILL EMPIRICAL.
      It lists the two classes we had been bitten by and *** HAS NO MECHANISM
      TO DETECT A THIRD ***: an unenumerated runner reads CLEAR, and that CLEAR
      is indistinguishable from a correct one. The failure direction is
      RELEASING.

THE INVERSION. Runners DECLARE THEMSELVES for the duration of a run; clearance
asks the registry, not the process table. A runner that never registers is a
bug in the runner — and, crucially, the interlock's coverage becomes a property
that can be TESTED (see `audit`) rather than remembered.

Two layers, with different jobs. They are not redundant:

  1. THE REGISTRY (authoritative). An entry file per in-flight run, held for the
     WHOLE run — which is what makes the answer continuous rather than
     point-in-time. This is what closes both ps failure modes at once: the
     pytest half is INVISIBLE to ps (in-process import), the shell half is
     INTERMITTENT (subprocess exec, so a point-in-time ps misses it BETWEEN
     invocations). A held registration has neither property.
  2. THE CORROBORATING PROBE (gap detector, NOT the answer). Scans the process
     table for anything executing one of THIS repo's suite files. Its purpose is
     precisely to catch a runner that did NOT register: registry empty + probe
     sees a suite executing == UNREGISTERED-RUNNER, a THIRD state that is
     neither CLEAR nor ordinary BUSY. *** Without this cross-check, "nothing is
     running" and "something is running that never registered" would be the same
     answer — which would move SABLE-pk15w's defect rather than remove it. ***
     The probe keys on the SUITE-FILE NAMING CONVENTION (hooks/test/test-*.sh,
     bin/test_*.py, pytest over a worktree) — already a mechanically-enforced
     SSOT via shell-run-set.sh --check — rather than on a hand-list of runner
     NAMES, so a brand-new runner executing the same suites is still seen.

FIVE OUTCOMES, never folded together (SABLE-4qlcf's two-axis rule):

  clear                 exit 0  registry empty AND probe saw nothing. THE ONLY
                                releasing answer.
  busy                  exit 1  a live registration is held.
  stale                 exit 4  a registration whose pid is DEAD. FAIL-CLOSED:
                                it does NOT time out and silently release; an
                                operator clears it with `reap`.
  unregistered-runner   exit 3  probe saw a suite executing that no registration
                                covers. A coverage gap in the interlock, stated
                                loudly instead of read as CLEAR.
  could-not-assess      exit 2  registry unreadable/unparseable, probe failed,
                                or a same-uid candidate could not be attributed
                                to a repo. Distinct from CLEAR by construction —
                                asserting that inequality is the load-bearing
                                test, because "reports nothing at all" also
                                "does not report clear".

THE RESIDUAL HOLE, STATED RATHER THAN PAPERED OVER. The runtime cross-check can
only see a runner that leaves a trace in the process table. A future harness
that neither registers, nor execs a suite file, nor runs under pytest — say a
python program that imports bin/test_*.py modules directly — is invisible to
BOTH layers at runtime, and `audit`'s two shapes would not match its source
either. What has changed is where that gap surfaces: it is no longer a silent
CLEAR during a hot-swap window, because such a harness has to be WRITTEN, and
adding a third shape to `audit` is a one-line change made at that moment. It is
a smaller hole than "remembering every runner", not the absence of one
(SABLE-sw8uh tracks closing it).

NEGATIVE CONTROL IS PART OF THE CONTRACT. A gate that can never release is
indistinguishable from correct caution and gets reverted within a day, so
`clearance` on a quiet host MUST return `clear` and the suites assert it
(prove-the-gate-can-release doctrine).

WHERE THE REGISTRY LIVES. `<git-common-dir>/sable-run-registry`, resolved from
the caller's base. Every linked worktree of a repo shares one git common dir, so
one registry answers for ALL worktrees — which is the "in ANY worktree of this
repo" half of the invariant, not an approximation of it. $SABLE_RUN_REGISTRY_DIR
overrides (tests, and any caller that needs an explicit scope).

Outside a git work tree with no override, `register` FAILS LOUDLY rather than
no-op'ing, and `clearance` returns could-not-assess. The two halves fail closed
on the same condition on purpose: there is no configuration in which a run goes
unrecorded while clearance still reads CLEAR.

CLI (also `bin/sable-run-registry`):
  register --runner <label> [--base <dir>]   print the token; hold it for the run
  release <token>                            release it (idempotent)
  clearance [--json] [--base <dir>]          the five-state answer; exit as above
  list [--json]                              entries with live/stale state
  reap <token> | --all-stale                 EXPLICIT operator clearing of stale
  audit [--json]                             static coverage gate: every path
                                             that executes a suite must register
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REGISTRY_DIRNAME = "sable-run-registry"
ENV_REGISTRY_DIR = "SABLE_RUN_REGISTRY_DIR"

CLEAR = "clear"
BUSY = "busy"
STALE = "stale"
UNREGISTERED = "unregistered-runner"
COULD_NOT_ASSESS = "could-not-assess"

EXIT_CODES = {
    CLEAR: 0,
    BUSY: 1,
    COULD_NOT_ASSESS: 2,
    UNREGISTERED: 3,
    STALE: 4,
}


class RunlockError(Exception):
    """Registry could not be resolved or written — always loud, never silent."""


# --- registry location -------------------------------------------------------


def _git(base: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", base, *args],
                              capture_output=True, text=True)
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_common_dir(base: str | None = None) -> str | None:
    """The SHARED git dir for <base> — the same path from every linked worktree,
    which is what makes one registry answer for the whole repo. None when <base>
    is not inside a git work tree."""
    b = base or os.getcwd()
    out = _git(b, "rev-parse", "--git-common-dir")
    if out is None:
        return None
    common = out.strip()
    if not common:
        return None
    if not os.path.isabs(common):
        common = os.path.join(b, common)
    try:
        return os.path.realpath(common)
    except OSError:
        return None


def registry_dir(base: str | None = None) -> str:
    """Resolve the registry directory. $SABLE_RUN_REGISTRY_DIR wins; otherwise
    <git-common-dir>/sable-run-registry. Raises RunlockError when neither is
    available — the loud half of the fail-closed pair (see module docstring)."""
    override = os.environ.get(ENV_REGISTRY_DIR)
    if override:
        return override
    common = git_common_dir(base)
    if not common:
        raise RunlockError(
            f"cannot resolve the run registry: {base or os.getcwd()} is not "
            f"inside a git work tree and ${ENV_REGISTRY_DIR} is unset"
        )
    return os.path.join(common, REGISTRY_DIRNAME)


def worktree_roots(base: str | None = None) -> list[str]:
    """Every worktree root of this repo (realpath'd). The set the probe uses to
    decide whether a candidate process belongs to THIS repo — the "in any
    worktree" half of the invariant."""
    out = _git(base or os.getcwd(), "worktree", "list", "--porcelain")
    if out is None:
        return []
    roots = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            p = line[len("worktree "):].strip()
            try:
                roots.append(os.path.realpath(p))
            except OSError:
                roots.append(p)
    return roots


# --- entries -----------------------------------------------------------------


@dataclass
class Entry:
    token: str
    path: str
    pid: int
    runner: str
    argv: list[str] = field(default_factory=list)
    cwd: str = ""
    started_at: float = 0.0
    host: str = ""

    def as_dict(self) -> dict:
        return {
            "token": self.token, "pid": self.pid, "runner": self.runner,
            "argv": self.argv, "cwd": self.cwd, "started_at": self.started_at,
            "host": self.host,
        }


def register(runner: str, base: str | None = None, argv: list[str] | None = None,
             pid: int | None = None) -> str:
    """Take a registration for the CURRENT run and return its token. Held until
    `release`; the caller is responsible for releasing on exit (shell callers:
    `trap ... EXIT`). Raises RunlockError if the registry cannot be written —
    a runner that cannot register must fail loudly, never proceed unrecorded."""
    if not runner:
        raise RunlockError("register requires a non-empty --runner label")
    d = registry_dir(base)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as exc:
        raise RunlockError(f"cannot create run registry {d}: {exc}") from exc
    token = f"{os.getpid() if pid is None else pid}-{uuid.uuid4().hex[:12]}"
    entry = Entry(
        token=token,
        path=os.path.join(d, token + ".json"),
        pid=os.getpid() if pid is None else pid,
        runner=runner,
        argv=list(argv if argv is not None else sys.argv),
        cwd=os.getcwd(),
        started_at=time.time(),
        host=socket.gethostname(),
    )
    tmp = entry.path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entry.as_dict(), fh)
        os.replace(tmp, entry.path)
    except OSError as exc:
        raise RunlockError(f"cannot write registry entry {entry.path}: {exc}") from exc
    return token


def release(token: str, base: str | None = None) -> bool:
    """Release a registration. Idempotent: releasing an already-released token
    is not an error (a runner's EXIT trap may fire after an inner release)."""
    d = registry_dir(base)
    p = os.path.join(d, token + ".json")
    try:
        os.unlink(p)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RunlockError(f"cannot release registry entry {p}: {exc}") from exc


def read_entries(base: str | None = None) -> tuple[list[Entry], list[str]]:
    """(entries, errors). An unparseable entry lands in `errors`, NOT silently
    in neither list — an unreadable registry is could-not-assess, not clear."""
    errors: list[str] = []
    entries: list[Entry] = []
    try:
        d = registry_dir(base)
    except RunlockError as exc:
        return entries, [str(exc)]
    if not os.path.isdir(d):
        return entries, errors
    try:
        names = sorted(os.listdir(d))
    except OSError as exc:
        return entries, [f"cannot read run registry {d}: {exc}"]
    for name in names:
        if not name.endswith(".json"):
            continue
        p = os.path.join(d, name)
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            errors.append(f"unparseable registry entry {p}: {exc}")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
            errors.append(f"malformed registry entry {p}: missing integer 'pid'")
            continue
        entries.append(Entry(
            token=str(data.get("token") or name[:-5]),
            path=p,
            pid=int(data["pid"]),
            runner=str(data.get("runner") or "<unlabelled>"),
            argv=list(data.get("argv") or []),
            cwd=str(data.get("cwd") or ""),
            started_at=float(data.get("started_at") or 0.0),
            host=str(data.get("host") or ""),
        ))
    return entries, errors


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


# --- the corroborating probe -------------------------------------------------

# Keyed on this repo's SUITE-FILE naming convention (already fail-closed via
# shell-run-set.sh --check), plus the pytest entry point. NOT a list of runner
# names: a new runner executing the same suites still matches. This layer never
# produces the CLEAR answer on its own — it only ever turns a would-be CLEAR
# into unregistered-runner.
SUITE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("hooks/test/test-*.sh (shell suite file)",
     re.compile(r"hooks/test/test-[A-Za-z0-9._-]+\.sh")),
    ("bin/test_*.py (pytest suite file)",
     re.compile(r"bin/test_[A-Za-z0-9._]+\.py")),
    ("pytest entry point",
     re.compile(r"(?:^|[/\s])pytest(?:$|[\s])")),
)


@dataclass
class ProcInfo:
    pid: int
    ppid: int
    uid: int
    args: str
    cwd: str | None = None


class ProbeError(Exception):
    pass


def read_process_table() -> list[ProcInfo]:
    """The host process table via ps. Raises ProbeError if ps is unusable — the
    probe's own failure is could-not-assess, never a quiet CLEAR."""
    try:
        proc = subprocess.run(["ps", "-eo", "pid=,ppid=,uid=,args="],
                              capture_output=True, text=True)
    except OSError as exc:
        raise ProbeError(f"cannot run ps: {exc}") from exc
    if proc.returncode != 0:
        raise ProbeError(f"ps exited {proc.returncode}: {(proc.stderr or '').strip()}")
    out: list[ProcInfo] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, uid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        out.append(ProcInfo(pid=pid, ppid=ppid, uid=uid, args=parts[3]))
    return out


def proc_cwd(pid: int) -> str | None:
    try:
        return os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None


@dataclass
class Candidate:
    pid: int
    ppid: int
    args: str
    why: str
    attributed: bool


def scan_processes(roots: list[str], table: list[ProcInfo] | None = None,
                   self_pid: int | None = None,
                   uid: int | None = None) -> tuple[list[Candidate], list[Candidate]]:
    """(attributed, unattributable). A process is a candidate when its command
    line names one of this repo's suite shapes; it is ATTRIBUTED when its cwd or
    an absolute path in its argv lands inside one of `roots`. A same-uid
    candidate we cannot attribute at all is returned separately and reads
    could-not-assess — "I saw something suite-shaped and could not tell whose"
    is not the same claim as "nothing is running"."""
    tbl = read_process_table() if table is None else table
    me = os.getpid() if self_pid is None else self_pid
    my_uid = os.getuid() if uid is None else uid
    attributed: list[Candidate] = []
    unattributable: list[Candidate] = []
    for p in tbl:
        if p.pid == me or p.uid != my_uid:
            continue
        why = ""
        for label, pat in SUITE_PATTERNS:
            if pat.search(p.args):
                why = label
                break
        if not why:
            continue
        cwd = p.cwd if p.cwd is not None else proc_cwd(p.pid)
        hit = False
        for r in roots:
            if cwd and (cwd == r or cwd.startswith(r + os.sep)):
                hit = True
                break
            if r in p.args:
                hit = True
                break
        if hit:
            attributed.append(Candidate(p.pid, p.ppid, p.args, why, True))
        elif cwd is None:
            # Same user, suite-shaped, and its cwd could not be read. Only a
            # process that has since exited is dropped (a race, not a gap).
            if pid_alive(p.pid):
                unattributable.append(Candidate(p.pid, p.ppid, p.args, why, False))
    return attributed, unattributable


def _ancestors(pid: int, table: list[ProcInfo]) -> list[int]:
    byp = {p.pid: p.ppid for p in table}
    seen: list[int] = []
    cur = pid
    while cur in byp and cur not in seen and cur > 1:
        seen.append(cur)
        cur = byp[cur]
    if cur and cur not in seen:
        seen.append(cur)
    return seen


def _covered_by(pid: int, live_pids: set[int], table: list[ProcInfo]) -> bool:
    """Is <pid> accounted for by some live registration? Coverage runs BOTH
    directions of the process tree on purpose:

      ANCESTOR — the ordinary case: shell-run-set registers its own shell and
        every suite it execs is a descendant.
      DESCENDANT — the WRAPPER case, and it is not hypothetical: a registered
        `pytest bin/` is itself a child of the `bash -c ...` that launched it,
        and that wrapper's command line contains the word pytest too. Checking
        ancestors alone reported such a wrapper as an UNREGISTERED runner —
        a false alarm on a properly registered run, which is the failure mode
        that gets a loud gate muted.
    """
    if pid in live_pids:
        return True
    if not live_pids.isdisjoint(_ancestors(pid, table)):
        return True
    return any(pid in _ancestors(lp, table) for lp in live_pids)


# --- clearance ---------------------------------------------------------------


@dataclass
class Clearance:
    state: str
    reasons: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    entries: list[dict] = field(default_factory=list)
    unregistered: list[dict] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.state]

    @property
    def is_clear(self) -> bool:
        return self.state == CLEAR

    def as_dict(self) -> dict:
        return {
            "state": self.state, "clear": self.is_clear,
            "exit_code": self.exit_code, "reasons": self.reasons,
            "checked": self.checked, "entries": self.entries,
            "unregistered": self.unregistered,
        }

    def render(self) -> str:
        lines = [f"hot-swap clearance: {self.state.upper()}"]
        for r in self.reasons:
            lines.append(f"  - {r}")
        lines.append("  checked:")
        for c in self.checked:
            lines.append(f"    * {c}")
        if self.state == STALE:
            lines.append("  remedy: `sable-run-registry reap <token>` — stale entries "
                         "are cleared by an explicit operator action, never by a "
                         "silent timeout (the releasing direction is the dangerous one).")
        if self.state == UNREGISTERED:
            lines.append("  remedy: that runner must take a registration for the "
                         "duration of its run (`sable-run-registry register --runner "
                         "<label>` + a release trap). Until it does, clearance for a "
                         "hot-swappable bin cannot be trusted to be complete.")
        return "\n".join(lines)


def clearance(base: str | None = None, table: list[ProcInfo] | None = None,
              probe: bool = True) -> Clearance:
    """Answer "is any test suite executing in any worktree of this repo?".

    Precedence, most-informative first — every finding still appears in
    `reasons`, so nothing is hidden by the winning label:
      could-not-assess > unregistered-runner > busy > stale > clear
    """
    reasons: list[str] = []
    checked: list[str] = []
    unknown: list[str] = []

    try:
        d = registry_dir(base)
        checked.append(f"run registry: {d} (authoritative; every worktree of this "
                       f"repo shares it)")
    except RunlockError as exc:
        return Clearance(state=COULD_NOT_ASSESS, reasons=[str(exc)],
                         checked=["run registry: UNRESOLVED"])

    # An empty registry only means "no runs" if a runner COULD have registered.
    # An unwritable registry dir would read as empty forever — a false CLEAR of
    # exactly the releasing kind this module exists to remove.
    if os.path.isdir(d):
        if not os.access(d, os.W_OK | os.X_OK):
            unknown.append(f"run registry {d} is not writable — runners cannot "
                           f"register here, so an empty registry proves nothing")
    else:
        parent = os.path.dirname(d) or "."
        if not (os.path.isdir(parent) and os.access(parent, os.W_OK)):
            unknown.append(f"run registry {d} does not exist and cannot be created "
                           f"(parent {parent} unwritable) — runners cannot register")

    entries, errors = read_entries(base)
    unknown.extend(errors)
    live = [e for e in entries if pid_alive(e.pid)]
    stale = [e for e in entries if not pid_alive(e.pid)]
    for e in live:
        reasons.append(f"live registration: {e.runner} (pid {e.pid}, token {e.token})")
    for e in stale:
        reasons.append(f"STALE registration: {e.runner} (pid {e.pid} is gone, token "
                       f"{e.token}) — fail-closed, needs an explicit reap")

    attributed: list[Candidate] = []
    unregistered: list[Candidate] = []
    if probe:
        roots = worktree_roots(base)
        checked.append("corroborating process probe over this repo's worktrees: "
                       + (", ".join(roots) if roots else "<none resolved>"))
        for label, _pat in SUITE_PATTERNS:
            checked.append(f"process probe pattern: {label}")
        checked.append("process probe scope: same-uid processes only")
        if not roots:
            unknown.append("cannot enumerate this repo's worktrees — the process "
                           "probe has nothing to attribute candidates to")
        else:
            try:
                tbl = read_process_table() if table is None else table
                attributed, unattributable = scan_processes(roots, table=tbl)
                for c in unattributable:
                    unknown.append(f"suite-shaped process pid {c.pid} could not be "
                                   f"attributed to a repo ({c.why}): {c.args}")
                live_pids = {e.pid for e in live}
                for c in attributed:
                    if not _covered_by(c.pid, live_pids, tbl):
                        unregistered.append(c)
            except ProbeError as exc:
                unknown.append(str(exc))
    else:
        checked.append("corroborating process probe: DISABLED for this call")

    for c in unregistered:
        reasons.append(f"UNREGISTERED runner: pid {c.pid} is executing a suite "
                       f"({c.why}) with no registration covering it: {c.args}")

    if unknown:
        return Clearance(state=COULD_NOT_ASSESS, reasons=reasons + unknown,
                         checked=checked,
                         entries=[e.as_dict() for e in entries],
                         unregistered=[c.__dict__ for c in unregistered])
    if unregistered:
        state = UNREGISTERED
    elif live:
        state = BUSY
    elif stale:
        state = STALE
    else:
        state = CLEAR
        reasons.append("registry empty and no suite-shaped process attributed to "
                       "this repo")
    return Clearance(state=state, reasons=reasons, checked=checked,
                     entries=[e.as_dict() for e in entries],
                     unregistered=[c.__dict__ for c in unregistered])


# --- static coverage audit ---------------------------------------------------
#
# The runtime probe catches an unregistered runner only while it is running.
# THIS catches one at commit time: any file that executes a suite must also
# register. Two shapes, with different obligations:
#
#   shape A — execs this repo's SHELL suites directly. Must register itself:
#             nothing else in the path can do it for it.
#   shape B — invokes pytest over this repo. Covered BY CONSTRUCTION by
#             bin/conftest.py, which pytest loads for every collection under
#             bin/ regardless of who invoked it — so no per-runner registration
#             is needed or wanted. The audit verifies that conftest exists and
#             registers, once, and then treats shape-B files as covered.
#
# EXEMPT is the deliberate escape hatch, and it fails in the SAFE direction: an
# exemption is explicit, reasoned, and visible in review, whereas a NEW runner
# is not exempt and therefore fails the audit rather than silently reading CLEAR.

# A registration CALL, not a mere mention of the registry — a file that only
# names sable-run-registry in prose must not read as covered.
REGISTRATION_MARKERS = (
    re.compile(r"runlock_hold[\s(]"),          # the shared bash helper
    re.compile(r"register\s+--runner"),        # the CLI form
    re.compile(r"\brunlock\.register\("),      # the python form
    re.compile(r"\bregister\(\s*f?[\"']"),     # a direct lib import + call
)

SHELL_SUITE_EXEC = re.compile(
    r"(?:bash|sh|zsh|exec)[\"'\s,\]]+[\"']?(?:\$\{?(?:TESTDIR|TEST_DIR)\}?|[^\s\"']*hooks/test)"
)
PYTEST_EXEC = re.compile(r"(?:python[0-9.]*[ \t]+-m[ \t]+pytest|(?:^|[/\s])pytest[ \t]+[-\w./])")
PYTEST_SUITE_FILE = re.compile(r"^bin/test_[^/]+\.py$")

EXEMPT: dict[str, str] = {
    "bin/sable_runlock_lib.py": "the registry itself",
    "bin/sable-run-registry": "the registry CLI itself",
    "bin/conftest.py": "IS the pytest-half registration",
}

AUDIT_ROOTS = (".github/ci", "hooks/multi-manager", "bin", "hooks")


def _strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def audit(repo: str | None = None) -> dict:
    """Static coverage gate. Returns {'ok': bool, 'violations': [...],
    'covered': [...]}. A violation means: this file executes a suite and nothing
    registers the run — i.e. exactly the unenumerated-runner hole, found before
    it can produce a false CLEAR."""
    root = Path(repo or os.getcwd())
    violations: list[str] = []
    covered: list[dict] = []

    conftest = root / "bin" / "conftest.py"
    conftest_ok = conftest.is_file() and any(
        m.search(conftest.read_text(encoding="utf-8", errors="replace"))
        for m in REGISTRATION_MARKERS)
    if not conftest_ok:
        violations.append(
            "bin/conftest.py does not register the pytest session — the ENTIRE "
            "python half of the interlock rests on it (it is what makes every "
            "pytest invocation, including one from a runner nobody has written "
            "yet, register by construction)")

    seen: set[str] = set()
    for sub in AUDIT_ROOTS:
        base = root / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            if rel.startswith("hooks/test/") or "/fixtures/" in rel:
                continue
            if "__pycache__" in rel or path.suffix in (
                    ".md", ".json", ".yaml", ".yml", ".txt", ".pyc"):
                continue
            # A bin/test_*.py file only ever executes under pytest, so conftest
            # has already registered by the time it runs anything — including a
            # shell suite. Skipped rather than listed, so `covered` stays a list
            # of RUNNERS instead of 80 lines of test files.
            if PYTEST_SUITE_FILE.match(rel):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            body = _strip_comments(text)
            shape_a = bool(SHELL_SUITE_EXEC.search(body))
            shape_b = bool(PYTEST_EXEC.search(body))
            if not (shape_a or shape_b):
                continue
            if rel in EXEMPT:
                covered.append({"file": rel, "via": f"exempt: {EXEMPT[rel]}"})
                continue
            registers = any(m.search(text) for m in REGISTRATION_MARKERS)
            if registers:
                covered.append({"file": rel, "via": "own registration"})
            elif shape_a:
                violations.append(
                    f"{rel} executes this repo's shell suites but takes no run "
                    f"registration — a hot-swap during its run would read CLEAR "
                    f"(SABLE-pk15w). Remedy: register for the duration of the run "
                    f"and release on EXIT, or add an explicit EXEMPT entry stating "
                    f"why it is not a suite runner.")
            else:
                covered.append({"file": rel,
                                "via": "bin/conftest.py (pytest half, by construction)"})
    return {"ok": not violations, "violations": violations, "covered": covered}


# --- CLI ---------------------------------------------------------------------


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="sable-run-registry", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("register", help="take a registration for this run")
    p.add_argument("--runner", required=True)
    p.add_argument("--base", default=None)
    p.add_argument("--pid", type=int, default=None,
                   help="the pid to record (default: this process). Shell callers "
                        "pass $$ so the entry names the RUNNER, not the short-lived "
                        "python child that wrote it.")

    p = sub.add_parser("release", help="release a registration (idempotent)")
    p.add_argument("token")
    p.add_argument("--base", default=None)

    p = sub.add_parser("clearance", help="the five-state hot-swap answer")
    p.add_argument("--json", action="store_true")
    p.add_argument("--base", default=None)
    p.add_argument("--no-probe", action="store_true",
                   help="registry only; skips the unregistered-runner cross-check")

    p = sub.add_parser("list", help="registry entries with live/stale state")
    p.add_argument("--json", action="store_true")
    p.add_argument("--base", default=None)

    p = sub.add_parser("reap", help="EXPLICIT operator clearing of stale entries")
    p.add_argument("token", nargs="?")
    p.add_argument("--all-stale", action="store_true")
    p.add_argument("--base", default=None)

    p = sub.add_parser("audit", help="static coverage gate over suite runners")
    p.add_argument("--json", action="store_true")
    p.add_argument("--repo", default=None)

    args = ap.parse_args(argv[1:])
    if not args.cmd:
        ap.print_help()
        return 2

    try:
        if args.cmd == "register":
            print(register(args.runner, base=args.base, pid=args.pid))
            return 0
        if args.cmd == "release":
            release(args.token, base=args.base)
            return 0
        if args.cmd == "clearance":
            c = clearance(base=args.base, probe=not args.no_probe)
            print(json.dumps(c.as_dict(), indent=2) if args.json else c.render())
            return c.exit_code
        if args.cmd == "list":
            entries, errors = read_entries(args.base)
            rows = [{**e.as_dict(), "live": pid_alive(e.pid)} for e in entries]
            if args.json:
                print(json.dumps({"entries": rows, "errors": errors}, indent=2))
            else:
                for r in rows:
                    print(f"{'LIVE ' if r['live'] else 'STALE'} {r['token']}  "
                          f"pid={r['pid']}  {r['runner']}")
                for e in errors:
                    print(f"ERROR {e}", file=sys.stderr)
            return 2 if errors else 0
        if args.cmd == "reap":
            entries, errors = read_entries(args.base)
            if errors:
                for e in errors:
                    print(f"ERROR {e}", file=sys.stderr)
                return 2
            if args.all_stale:
                n = 0
                for e in entries:
                    if not pid_alive(e.pid):
                        release(e.token, base=args.base)
                        n += 1
                        print(f"reaped {e.token} ({e.runner}, pid {e.pid})")
                print(f"reaped {n} stale entr(y/ies)")
                return 0
            if not args.token:
                print("reap needs a token or --all-stale", file=sys.stderr)
                return 2
            match = [e for e in entries if e.token == args.token]
            if match and pid_alive(match[0].pid):
                print(f"refusing to reap {args.token}: pid {match[0].pid} is ALIVE — "
                      f"that is a live run, not a stale entry", file=sys.stderr)
                return 1
            release(args.token, base=args.base)
            print(f"reaped {args.token}")
            return 0
        if args.cmd == "audit":
            res = audit(args.repo)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                for c in res["covered"]:
                    print(f"COVERED   {c['file']}  — {c['via']}")
                if res["violations"]:
                    print(f"::error::sable-run-registry audit: "
                          f"{len(res['violations'])} suite runner(s) take no run "
                          f"registration:")
                    for v in res["violations"]:
                        print(f"  - {v}")
                else:
                    print(f"sable-run-registry audit: {len(res['covered'])} suite-"
                          f"executing path(s) checked — all covered")
            return 0 if res["ok"] else 1
    except RunlockError as exc:
        print(f"::error::sable-run-registry: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
