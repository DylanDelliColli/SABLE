"""sable_pane_lib — shared tmux-pane readiness + dispatch helpers (SABLE-bldh.14).

Used by both `sable-spawn-worker` (deliver a worker's dispatch prompt) and
`sable-tmux --autostart` (kick a role pane's operating loop). The hard parts —
beating the boot/dropped-Enter race and accepting blocking startup gates (the
bypass-permissions warning, the trust dialog) — live here once.

Both importers are symlinks on PATH, so they resolve this module via
`os.path.realpath(__file__)`'s directory; no separate install step is needed.
"""
from __future__ import annotations

import os
import re
import subprocess
import time

# Non-printable control bytes (except \t\n which are whitespace-handled). A
# stray echoed Escape on the prompt line must not defeat glyph detection
# (SABLE-zaum).
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(line: str) -> str:
    """A pane line with control bytes stripped and whitespace trimmed."""
    return _CTRL_RE.sub("", line).strip()


def _canon(text: str) -> str:
    """Canonical form for presence checks: control bytes stripped, ALL
    whitespace removed. Pane wraps can split a message MID-WORD (capture-pane
    without -J emits the segments as separate lines), so any comparison that
    preserves spaces mismatches across a wrap boundary (SABLE-1umr)."""
    return "".join(_CTRL_RE.sub("", text).split())


def pane_ready(capture: str) -> bool:
    """The TUI is ready to accept input once its input box shows an EMPTY prompt
    line (just the prompt glyph). While booting (splash) or on a blocking gate
    screen there is no such empty line."""
    for line in reversed(capture.splitlines()):
        if _clean(line) in ("❯", ">"):
            return True
    return False


def accept_startup_gate(capture: str) -> str | None:
    """The menu key that ACCEPTS a known blocking startup gate, or None. The
    bypass-permissions warning defaults to '1. No, exit' (so a blind Enter kills
    the session), and a fresh worktree may show a trust dialog. Accept bypass with
    '2' (Yes, I accept) and trust with '1' (Yes, I trust this folder)."""
    low = capture.lower()
    if "bypass permissions mode" in low and "accept" in low:
        return "2"
    if "trust this folder" in low or ("do you trust" in low and "yes" in low):
        return "1"
    return None


def dispatch_landed(capture: str, snippet: str) -> bool:
    """True once the instruction has been SUBMITTED: the snippet appears in the
    pane but no longer sits in the input box. The box is the LAST prompt-glyph
    line plus every line after it — the composer sits at the bottom of the
    pane, and a message wider than the pane WRAPS onto glyph-less continuation
    lines (SABLE-1umr: judging only the glyph line itself false-positived
    "landed" for any wrapped message, so no Enter was ever resent and the text
    sat unsubmitted while the sender reported delivered). All comparisons are
    control-char/whitespace-insensitive (_canon): wraps may split mid-word, and
    a stray control byte must not hide the box entirely (SABLE-zaum)."""
    want = _canon(snippet)
    if not want or want not in _canon(capture):
        return False
    lines = capture.splitlines()
    box_start = None
    for i, line in enumerate(lines):
        if _clean(line).startswith(("❯", ">")):
            box_start = i
    if box_start is None:
        return True
    return want not in _canon("\n".join(lines[box_start:]))


def capture_pane(base: list[str], pane: str) -> str:
    # -J joins wrapped lines, so a message wider than the pane comes back as
    # the one line it really is — box detection then sees the whole composer.
    return subprocess.run(base + ["capture-pane", "-p", "-J", "-t", pane],
                          capture_output=True, text=True).stdout


def wait_for_ready(base, pane, timeout, interval=0.5, capture=None, sleep=None,
                   run=None) -> bool:
    """Poll until the pane shows its empty prompt, accepting any blocking startup
    gate (bypass warning / trust dialog) on the way so the session doesn't die on
    the gate's default 'No, exit'."""
    capture = capture or (lambda: capture_pane(base, pane))
    sleep = sleep or time.sleep
    # tolerant runner: a dead pane must not crash the caller
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True).returncode == 0)
    waited = 0.0
    while waited < timeout:
        cap = capture()
        if pane_ready(cap):
            return True
        key = accept_startup_gate(cap)
        if key is not None:
            run(base + ["send-keys", "-t", pane, key])
            run(base + ["send-keys", "-t", pane, "Enter"])
        sleep(interval)
        waited += interval
    return False


def deliver_text(base, pane, text, snippet, tries=8, interval=1.0,
                 run=None, capture=None, sleep=None) -> bool:
    """Type `text` into the pane and submit it — Enter is sent IMMEDIATELY after
    the text (submission must not depend on the landed-check failing first,
    SABLE-1umr), then resent until the text leaves the input box (the
    dropped-Enter race). A resent Enter on an already-empty box is a harmless
    no-op. Returns False (clean) if the pane vanishes."""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True).returncode == 0)
    capture = capture or (lambda: capture_pane(base, pane))
    sleep = sleep or time.sleep
    if run(base + ["send-keys", "-t", pane, "-l", text]) is False:
        return False
    if run(base + ["send-keys", "-t", pane, "Enter"]) is False:
        return False
    for _ in range(max(1, tries)):
        sleep(interval)
        if dispatch_landed(capture(), snippet):
            return True
        if run(base + ["send-keys", "-t", pane, "Enter"]) is False:
            return False
    return dispatch_landed(capture(), snippet)


# --- Per-repo session resolution (SABLE-e1e3.1). One tmux server can host one
# fleet PER REPO: the session name derives from the repo root
# (sable-<basename>), the session records its root in the @sable_repo session
# option (the collision guard), and every tool resolves its target session
# through resolve_session below instead of assuming the literal 'sable'.

LEGACY_SESSION = "sable"


class SessionCollision(RuntimeError):
    """A derived session name is held by a DIFFERENT repo's fleet."""


def _tmux_run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def tmux_base(socket: str | None = None) -> list[str]:
    return ["tmux", "-L", socket] if socket else ["tmux"]


def repo_root(base: str | None = None) -> str | None:
    """The MAIN-worktree root of the repo containing `base` (git common dir's
    parent — same resolution as sable-mode's state path), or None outside a
    repo."""
    base = base or os.getcwd()
    try:
        r = subprocess.run(["git", "-C", base, "rev-parse", "--git-common-dir"],
                           capture_output=True, text=True)
        common = r.stdout.strip()
        if r.returncode == 0 and common:
            cpath = common if os.path.isabs(common) else os.path.join(base, common)
            return os.path.realpath(os.path.dirname(os.path.realpath(cpath)) or "/")
    except Exception:
        pass
    return None


def sanitize_session_name(name: str) -> str:
    """tmux-safe session fragment: '.' and ':' are meaningful in tmux targets,
    so they (and other hostile chars) become dashes; runs collapse; leading/
    trailing dashes strip; empty falls back to 'repo'."""
    out = re.sub(r"[^A-Za-z0-9_-]", "-", name)
    out = re.sub(r"-{2,}", "-", out).strip("-")
    return out or "repo"


def derived_session(root: str) -> str:
    return f"sable-{sanitize_session_name(os.path.basename(root.rstrip('/')))}"


def session_exists(base: list[str], name: str, run=None) -> bool:
    run = run or _tmux_run
    return run(base + ["has-session", "-t", name]).returncode == 0


def session_repo(base: list[str], name: str, run=None) -> str | None:
    """The repo root recorded on the session (@sable_repo session option), or
    None when unset (pre-e1e3 sessions / hand-made ones)."""
    run = run or _tmux_run
    r = run(base + ["show-options", "-v", "-t", name, "@sable_repo"])
    val = (r.stdout or "").strip()
    return val if r.returncode == 0 and val else None


def _panes_under_root(base: list[str], name: str, root: str, run=None) -> bool:
    """True when any pane of the session has its cwd inside `root` — the
    transitional heuristic that lets a pre-e1e3 fleet named 'sable' keep being
    addressed by ITS repo (its lincoln pane sits at the root) while never
    matching a different repo's tools."""
    run = run or _tmux_run
    r = run(base + ["list-panes", "-s", "-t", name, "-F", "#{pane_current_path}"])
    if r.returncode != 0:
        return False
    prefix = root.rstrip("/") + "/"
    for line in (r.stdout or "").splitlines():
        path = line.strip()
        if path == root or path.startswith(prefix):
            return True
    return False


def calling_pane_session(base: list[str], run=None) -> str | None:
    """The tmux session that actually hosts the CALLING pane, when running
    inside tmux ($TMUX_PANE set) -- the ground truth for "which fleet am I
    in", independent of whatever repo the caller's CWD happens to sit in
    (SABLE-ssd8: a worker's shell CWD may be a DIFFERENT repo's worktree than
    the session that actually spawned it, e.g. a worker dispatched by
    tarzan's session but working in a cross-repo worktree -- CWD-derivation
    resolved the wrong session and couldn't find tarzan at all). None outside
    tmux, or if the pane has already vanished."""
    run = run or _tmux_run
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    r = run(base + ["display-message", "-p", "-t", pane, "#{session_name}"])
    out = (getattr(r, "stdout", "") or "").strip()
    return out if getattr(r, "returncode", 1) == 0 and out else None


def resolve_session(socket: str | None = None, base: str | None = None,
                    run=None, _root: str | None | object = "auto",
                    _pane_session: str | None | object = "auto") -> str:
    """The tmux session this repo's fleet lives in (or should be created as).

    Precedence: SABLE_TMUX_SESSION env verbatim -> the CALLING PANE's actual
    tmux session when running inside tmux (SABLE-ssd8 -- ground truth for
    "which fleet am I in", independent of CWD) -> derived sable-<basename>
    when that session exists and is not owned by another repo (SessionCollision
    when it is) -> the legacy 'sable' session when it exists and its panes live
    in this repo -> the derived name (creation target). Outside a git repo AND
    outside tmux, the legacy name is returned unchanged. `_root`/`_pane_session`
    are test seams."""
    env = os.environ.get("SABLE_TMUX_SESSION")
    if env:
        return env
    tb = tmux_base(socket)
    pane_session = (calling_pane_session(tb, run=run) if _pane_session == "auto"
                    else _pane_session)
    if pane_session:
        return pane_session
    root = repo_root(base) if _root == "auto" else _root
    if root is None:
        return LEGACY_SESSION
    name = derived_session(root)
    if session_exists(tb, name, run=run):
        owner = session_repo(tb, name, run=run)
        if owner and owner != root:
            raise SessionCollision(
                f"tmux session '{name}' belongs to repo {owner}, not {root} — "
                f"set SABLE_TMUX_SESSION to a unique name for one of the two repos.")
        return name
    if session_exists(tb, LEGACY_SESSION, run=run) and \
            _panes_under_root(tb, LEGACY_SESSION, root, run=run):
        return LEGACY_SESSION
    return name


# --- Autonomous-role operating-loop kicks (SABLE-bldh.14, moved here for
# SABLE-dqhn.2 so sable-tmux --autostart and sable-spawn-manager share ONE
# source). Lincoln is the operator's pane — never kicked.
AUTONOMOUS_ROLES = {"optimus", "tarzan", "chuck"}
KICK_TAG = "SABLE-AUTOSTART"


def kick_message(role: str) -> str:
    """The turn that starts a role's operating loop."""
    common = (f"[{KICK_TAG}] Operator: begin your operating loop now and run it "
              f"autonomously — do not wait for further input.")
    if role in ("optimus", "tarzan"):
        return (f"{common} Drain your lane from `bd ready`: verify each ready bead, "
                f"claim it, and `sable-spawn-worker <id> --scope <name>`; review the "
                f"results, reap done panes, then pause briefly and loop. Stand down "
                f"when the pool and your inbox are empty.")
    if role == "chuck":
        return (f"{common} You are event-driven: each ⟦SABLE-MSG⟧ PR-ready "
                f"message from a manager is a merge request — review and merge it, then "
                f"report back. Also drain any existing for-chuck beads and run a "
                f"stranded-recovery sweep now, then idle waiting for messages.")
    return common
