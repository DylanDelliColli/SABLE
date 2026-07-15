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


def _already_pending(capture_text: str, snippet: str) -> bool:
    """True when `snippet` already sits somewhere in `capture_text` — evidence a
    PRIOR deliver_text attempt already typed it into this pane (SABLE-msxj: a
    caller retrying a busy-at-t0 send whose earlier attempt's line is still
    queued would otherwise retype it, producing a literal second queued copy and
    a duplicate turn once both submit). Reuses the same canonical, whitespace/
    control-byte-insensitive comparison as dispatch_landed."""
    want = _canon(snippet)
    return bool(want) and want in _canon(capture_text)


def pane_ready(capture: str) -> bool:
    """The TUI is ready to accept input once its input box shows an EMPTY prompt
    line (just the prompt glyph). While booting (splash) or on a blocking gate
    screen there is no such empty line.

    NOTE: readiness is NOT idleness. A pane running a turn ALSO shows the empty
    composer prompt at the bottom, so pane_ready returns True mid-turn — see
    pane_idle for the stronger "ready AND not mid-turn" predicate the interrupt
    path needs (SABLE-m6is)."""
    for line in reversed(capture.splitlines()):
        if _clean(line) in ("❯", ">"):
            return True
    return False


# The status line a running Claude turn renders (whatever the spinner glyph /
# elapsed-time / token-count prefix, it always carries the "esc to interrupt"
# affordance); an idle pane never shows it. This is the busy signal pane_ready
# is blind to (SABLE-m6is).
_BUSY_MARKERS = ("esc to interrupt",)


def pane_busy(capture: str) -> bool:
    """True while the pane is MID-TURN. Control bytes are stripped and all
    whitespace collapsed to single spaces before matching, so a status line
    padded/reflowed by box-drawing or a variable-width spinner prefix still
    matches (SABLE-m6is). The composer prompt is shown DURING a turn too, which
    is exactly why pane_ready alone reported a busy pane 'ready' and the
    interrupt path typed into a pane still redrawing."""
    hay = " ".join(_CTRL_RE.sub(" ", capture).split()).lower()
    return any(marker in hay for marker in _BUSY_MARKERS)


def pane_idle(capture: str) -> bool:
    """The pane is ready for a NEW submitted turn: its composer shows the empty
    prompt (pane_ready) AND no turn is currently running (not pane_busy).
    --interrupt defers typing until THIS holds, not merely until pane_ready:
    a busy pane shows the empty composer prompt too, so typing on pane_ready
    alone raced the spinner redraw / composer-clear of the interrupted turn and
    silently dropped the message (SABLE-m6is)."""
    return pane_ready(capture) and not pane_busy(capture)


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


# A numbered menu option line ("1. No, exit", optionally cursor-marked "❯ 2.
# Yes, I accept"), and the explicit keypress affordances interactive
# selectors print ("Enter to confirm/select", "(Use arrow keys)", "(y/n)").
_DIALOG_OPTION_RE = re.compile(r"^[❯>]?\s*\d+[.)]\s+\S")
_DIALOG_AFFORDANCE_RE = re.compile(
    r"enter to (confirm|select)|use arrow keys|\(y/n\)", re.IGNORECASE)


def dialog_posture(capture: str) -> bool:
    """True when the pane shows an interactive selector/dialog demanding a
    keypress — a numbered-choice menu or an explicit 'Enter to confirm/select'
    /'Use arrow keys' affordance — rather than the normal composer (SABLE-m94k).

    accept_startup_gate only recognizes the two KNOWN blocking gates (the
    bypass-permissions warning, the trust-folder dialog) well enough to
    auto-clear them; this predicate instead flags ANY unrecognized dialog so a
    caller can REFUSE to type into it rather than blindly submit — the 73t4
    incident that motivated this bead was exactly an Enter-to-select dialog
    accept_startup_gate did not recognize, so wait_for_ready's False return
    was silently discarded and the dispatch text was typed into the dialog.

    Deliberately conservative: a booting/splash screen is also not pane_ready,
    but has neither signature below, so it is correctly NOT flagged as a
    dialog (still-booting and stuck-on-a-dialog need different handling —
    the caller retries the former's wait but must never type into the
    latter). Requires TWO menu-option lines (not one) so a single incidental
    numbered line in scrollback can't false-positive a legit spawn."""
    lines = [_clean(line) for line in capture.splitlines()]
    if any(_DIALOG_AFFORDANCE_RE.search(line) for line in lines):
        return True
    return sum(1 for line in lines if line and _DIALOG_OPTION_RE.match(line)) >= 2


# The session-limit banner Claude Code prints when a message/session rate
# limit cuts a turn short ("You have hit your session limit - resets 2pm").
# The turn dies but the composer goes right back to its normal empty prompt,
# so nothing else distinguishes that pane from one simply between turns — an
# alive pane with a CUT turn (SABLE-ita7: the SABLE-tz7h.4 worker pane sat
# "running" for ~5 hours after hitting this, invisible to sable-worker-status
# the whole time).
_SESSION_LIMIT_RE = re.compile(r"hit your session limit.*?resets?\s+(.+)", re.IGNORECASE)


def session_limit_reset(capture: str) -> str | None:
    """The reset-time text ('2pm', 'tomorrow at 9am', ...) when a line of
    `capture` carries the Claude Code session-rate-limit banner, or None if
    the banner isn't present anywhere in the pane. Only tests for the
    banner's TEXT — whether the pane is actually STALLED on it (an idle
    composer, turn cut off) versus the banner merely sitting in older
    scrollback while a later turn keeps processing is the caller's call,
    combining this with pane_ready (SABLE-ita7)."""
    for line in capture.splitlines():
        m = _SESSION_LIMIT_RE.search(_clean(line))
        if m:
            return m.group(1).strip()
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
    a stray control byte must not hide the box entirely (SABLE-zaum).

    When NO composer glyph is locatable at all (box_start is None) we CANNOT
    prove the snippet left the input box, so we do NOT claim it landed
    (SABLE-wvk9). The prior `return True` here was the visible-versus-submitted
    conflation optimus flagged: a booting/gated pane, or a busy pane whose
    prompt line was momentarily obscured by a spinner/reflow at capture time,
    can show the typed text while it sits UNSUBMITTED as pending input — the
    exact silent-swallow signature behind two stranded handoffs (text left in
    worker composers, delivery assumed successful). Failing closed here makes
    deliver_text keep retrying and ultimately report non-delivery, which routes
    to the durable inbox-bead fallback instead of a phantom 'delivered'.

    A running-turn (busy) marker BELOW the last prompt glyph is the INVERSE
    trap (SABLE-uh4b, the mirror of SABLE-wvk9): once a message SUBMITS, Claude
    Code echoes it into the transcript as its own prompt-glyph line ("❯ <msg>",
    glyph + a REGULAR space) and starts the turn — but in the brief redraw
    window right after Enter the empty composer has not repainted yet, so that
    echo is momentarily the LAST prompt-glyph line while the turn already runs
    beneath it. The live composer always sits at the very BOTTOM with only frame
    chrome (border / cwd / mode lines) below it, never a busy status line — so a
    busy marker below the last glyph proves that glyph is a submitted echo, not
    the editable box, and the snippet has LANDED. Without this, that redraw-race
    capture false-negatived the landing (report-NOT-landed-when-it-DID), filing
    a duplicate durable fallback bead for a message that actually submitted and
    blocking a P0 worker release."""
    want = _canon(snippet)
    if not want or want not in _canon(capture):
        return False
    lines = capture.splitlines()
    box_start = None
    for i, line in enumerate(lines):
        if _clean(line).startswith(("❯", ">")):
            box_start = i
    if box_start is None:
        return False
    if pane_busy("\n".join(lines[box_start + 1:])):
        return True
    return want not in _canon("\n".join(lines[box_start:]))


# The composer's queued-message footer hint (SABLE-msxj). h0jw's box-based
# delayed-confirm signals assumed a busy-at-t0 line gets hoisted ABOVE the
# composer with the input box cleared — but the real Claude-TUI instead leaves
# a queued line visible IN the composer/queued area and marks it with this
# footer. dispatch_landed's box scan never recognizes that posture (the text
# never "leaves the box" the way the hoist model expects), so a genuinely
# delivered-queued send timed out h0jw's poll budget and scored as failure
# (SABLE-l8a5: closed false-fail, evidence the queued line was live in the pane
# the whole time).
_QUEUED_FOOTER_MARKERS = ("press up to edit queued messages",)


def pane_has_queued_message(capture: str, snippet: str) -> bool:
    """True when `snippet` sits in the pane ALONGSIDE the queued-messages footer
    hint — the real Claude-TUI posture for a line that queued behind a busy turn
    (SABLE-msxj). Unlike dispatch_landed, this does NOT require the snippet to
    have left the input box: the footer itself is the delivered-queued proof,
    independent of exactly where the text renders. Whitespace/control-byte
    insensitive, matching every other marker check in this module."""
    want = _canon(snippet)
    if not want or want not in _canon(capture):
        return False
    hay = " ".join(_CTRL_RE.sub(" ", capture).split()).lower()
    return any(marker in hay for marker in _QUEUED_FOOTER_MARKERS)


def submitted_own_turn(capture: str, snippet: str) -> bool:
    """Positive proof that `snippet` is its OWN submitted turn — the signal the
    DELAYED-confirmation path (SABLE-h0jw) polls for after a BUSY-at-t0 send,
    instead of failing closed the instant the pane is busy at t0 (SABLE-d21h).

    On a pane that was busy at send time our line first QUEUES behind the running
    turn: Claude Code hoists it above the composer and clears the input box, so
    dispatch_landed's visible-and-not-in-box signal holds IDENTICALLY for a line
    that merely queued (droppable on the running turn's compaction/redraw/reap) as
    for one genuinely submitted. d21h could not tell them apart in a single
    capture and so failed CLOSED at t0 — which filed a durable noise bead EVEN
    WHEN the queued line later submitted and landed (LINCOLN 2026-07-14: two
    instances into chuck's busy pane). This predicate keys on three signals a
    still-queued capture can NEVER present (the first two watch across the turn
    boundary; the third recognizes the queued posture itself as already
    sufficient), so any one of them confirms a real delivered send without
    resurrecting the phantom-confirm:

      1. the pane is IDLE and dispatch_landed — a queued line always sits behind a
         RUNNING turn, so an idle capture proves that turn ended AND our line
         persisted in the transcript (a dropped queue-line would be gone); or
      2. dispatch_landed holds via a running-turn busy marker directly BELOW the
         last prompt-glyph line — our OWN turn has started and echoed our line as
         its prompt (the SABLE-uh4b redraw race). A queued line's busy marker sits
         ABOVE the empty composer that anchors the bottom of the frame, never
         below our echo, so this branch cannot fire for a merely-queued line; or
      3. pane_has_queued_message — the real Claude-TUI's queued-messages footer
         (SABLE-msxj) rendered alongside our snippet. h0jw's assumption above (a
         queued line gets hoisted above the composer and the box cleared) does not
         hold for this TUI posture: the line stays visible IN the composer/queued
         area, so dispatch_landed's box scan never proves it left the box and
         branches 1-2 above can time out the poll budget on a send that in fact
         already succeeded — the exact false-fail SABLE-l8a5 evidenced (closed
         false-fail after the queued line was confirmed live in the pane)."""
    if pane_has_queued_message(capture, snippet):
        return True
    if not dispatch_landed(capture, snippet):
        return False
    if pane_idle(capture):
        return True
    lines = capture.splitlines()
    box_start = None
    for i, line in enumerate(lines):
        if _clean(line).startswith(("❯", ">")):
            box_start = i
    return box_start is not None and pane_busy("\n".join(lines[box_start + 1:]))


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


def wait_for_idle(base, pane, timeout, interval=0.5, capture=None, sleep=None,
                  run=None) -> bool:
    """Poll until the pane is IDLE (empty prompt AND no running turn), accepting
    any blocking startup gate (bypass warning / trust dialog) on the way exactly
    as wait_for_ready does — a freshly spawned pane may be mid-kick-turn or
    sitting on a gate (SABLE-m6is noted the fresh-spawn window as a second busy
    state). sable-msg --interrupt calls this AFTER sending a single Escape:
    injection is deferred until the interrupted turn has actually settled, so
    the message is not swallowed by a still-busy pane's redraw. Returns True once
    idle; False if the timeout elapses first (the caller then attempts delivery
    anyway and degrades to the verified-failure fallback — never worse than the
    pre-idle-wait behavior)."""
    capture = capture or (lambda: capture_pane(base, pane))
    sleep = sleep or time.sleep
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True).returncode == 0)
    waited = 0.0
    while waited < timeout:
        cap = capture()
        if pane_idle(cap):
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
    no-op. Returns False (clean) if the pane vanishes.

    A landing is only counted when the pane was IDLE at send time — pane_idle at
    t0, captured BEFORE typing (SABLE-d21h). A message typed into a BUSY pane
    cannot be verified as its own submitted turn: Claude Code hoists a queued
    line ABOVE the composer and clears the input box, so dispatch_landed's
    visible-and-not-in-box signal holds IDENTICALLY for a message merely QUEUED
    behind the running turn — and a queued line can be dropped on that turn's
    compaction/redraw or a pane reap (the queued-while-busy swallow that stranded
    two handoffs). Only an idle->our-turn transition (idle at t0, then our text
    visible outside the box) is a genuine submitted turn.

    When the pane is BUSY at t0 we type + submit ONCE (best-effort queue) and then
    DELAY confirmation (SABLE-h0jw) instead of failing closed immediately (the
    SABLE-d21h behavior, which filed a durable noise bead EVEN WHEN the queued
    line later submitted and landed — LINCOLN 2026-07-14, two instances into
    chuck's busy pane). We watch the queued line across the turn boundary and
    confirm the moment it PROVABLY became its own submitted turn (submitted_own_turn
    — a signal a still-queued capture can never present). Only if the poll budget
    elapses with the line still queued (or dropped) do we report False and let the
    caller degrade to the durable fallback — never worse than d21h, strictly
    better whenever the running turn ends within the budget. No Enter is resent on
    this leg: a queued line auto-submits when the turn ends, and a stray Enter
    would risk a spurious blank turn.

    The fresh-pane dispatch (sable-spawn-worker) and manager kicks (sable-tmux /
    -spawn-manager) all wait_for_ready first, so their pane is idle at t0 and takes
    the idle path below — neither guard false-negatives them.

    A caller may invoke deliver_text again after a busy-at-t0 attempt exhausted
    its poll budget and reported False (SABLE-msxj) — but if that PRIOR attempt's
    line is still sitting queued behind the (possibly still-running) turn, typing
    unconditionally produces a literal SECOND queued copy, and the recipient gets
    a duplicate turn once both submit. So when the pane is busy at t0 AND
    `snippet` already sits somewhere in that t0 capture, we skip the type+Enter
    entirely and fall straight into the same delayed-confirmation poll below for
    the ALREADY-queued line — idempotent re-verification, not a second send."""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True).returncode == 0)
    capture = capture or (lambda: capture_pane(base, pane))
    sleep = sleep or time.sleep
    cap0 = capture()
    idle_at_send = pane_idle(cap0)
    already_pending = (not idle_at_send) and _already_pending(cap0, snippet)
    if not already_pending:
        if run(base + ["send-keys", "-t", pane, "-l", text]) is False:
            return False
        if run(base + ["send-keys", "-t", pane, "Enter"]) is False:
            return False
    if not idle_at_send:
        # Busy at t0: the line queued behind the running turn. Rather than fail
        # closed now (SABLE-d21h) — which filed a noise bead even when the queue
        # later submitted+landed — DELAY confirmation (SABLE-h0jw): poll until the
        # line PROVABLY became its own submitted turn, failing closed only on a
        # budget timeout. No Enter is resent (a queued line auto-submits on
        # turn-end; a stray Enter risks a spurious blank turn).
        for _ in range(max(1, tries)):
            sleep(interval)
            if submitted_own_turn(capture(), snippet):
                return True
        return submitted_own_turn(capture(), snippet)
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
    """True iff a session literally named `name` exists. The '=' anchor forces
    tmux's exact-match target syntax -- a bare name falls back to fnmatch/
    prefix resolution when there's no exact match, so has-session -t sable
    would spuriously succeed whenever some OTHER session merely starts with
    'sable' (e.g. a per-repo sable-<repo> fleet), which is exactly the
    collision LEGACY_SESSION ('sable') is a prefix of every derived name for
    (SABLE-hvwk)."""
    run = run or _tmux_run
    return run(base + ["has-session", "-t", f"={name}"]).returncode == 0


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


# --- Dispatch throttle knob (SABLE-mmdt), shared by sable-spawn-worker (the
# refusal) and sable-view (the cockpit count-vs-cap line) so the default can
# never drift between the gate and its observability surface.
WORKER_CAP_DEFAULT = 4


def worker_cap(env=None) -> int:
    """Max live worker panes per session (SABLE_MAX_WORKERS). Default 4 — the
    2026-07-07 full-fleet dispatch (~15 workers + Docker) froze the WSL host.
    0 is an explicit emergency stop (every spawn refused). Unparseable or
    negative values keep the DEFAULT throttle rather than lifting it."""
    raw = ((env if env is not None else os.environ).get("SABLE_MAX_WORKERS") or "").strip()
    if not raw:
        return WORKER_CAP_DEFAULT
    try:
        cap = int(raw)
    except ValueError:
        return WORKER_CAP_DEFAULT
    return cap if cap >= 0 else WORKER_CAP_DEFAULT


# --- Autonomous-role operating-loop kicks (SABLE-bldh.14, moved here for
# SABLE-dqhn.2 so sable-tmux --autostart and sable-spawn-manager share ONE
# source). Lincoln is the operator's pane — never kicked.
AUTONOMOUS_ROLES = {"optimus", "tarzan", "chuck"}
KICK_TAG = "SABLE-AUTOSTART"


def kick_message(role: str, deliverable: str | None = None) -> str:
    """The turn that starts a role's operating loop.

    A non-None `deliverable` selects the BOUNDED PRODUCER branch
    (architecture.json decision 1, SABLE-tz7h.1) instead of an autonomous
    manager loop: the kick carries only the lifecycle contract — write the
    deliverable, flag done, exit, never loop — because the actual task brief
    arrives separately via sable-msg once the pane is ready.

    The lane-manager kick (role in ("optimus", "tarzan")) was reworded in
    SABLE-nmmh to EVENT-DRIVEN 'end your turn when idle' phrasing: the previous
    'pause briefly and loop' text taught the pane to occupy its own turn while
    waiting, which deafened its inbound message channel (queued/--interrupt
    sable-msgs couldn't land mid-turn — the SABLE-kkgt urgent-delivery failure).
    Chuck's kick and the bare-common fallback are unchanged. Both the manager
    and chuck strings are byte-locked by
    test_sable_spawn_manager.test_manager_kick_text_byte_identical_regression —
    any reword there must update that assertion deliberately."""
    common = (f"[{KICK_TAG}] Operator: begin your operating loop now and run it "
              f"autonomously — do not wait for further input.")
    if deliverable:
        return (f"[{KICK_TAG}] Operator: you are a BOUNDED PRODUCER pane — a "
                f"single-shot analysis run, not a looping manager. Your task brief "
                f"will arrive separately (via sable-msg) once you're ready; this "
                f"kick is only your lifecycle contract. Do your analysis, write your "
                f"complete deliverable to {deliverable}, then set "
                f"`tmux set-option -p -t \"$TMUX_PANE\" @sable_status done` and exit. "
                f"Do not write to `bd` — writes happen post-merge, not from this "
                f"pane. Never loop back for more work: once your deliverable is "
                f"written and you have flagged done, you are finished.")
    if role in ("optimus", "tarzan"):
        return (f"{common} Drain your lane from `bd ready`: verify each ready bead, "
                f"claim it, and `sable-spawn-worker <id> --scope <name>`; review the "
                f"results and reap done panes. You are EVENT-DRIVEN: when nothing is "
                f"actionable, end your turn — a new ⟦SABLE-MSG⟧ turn or a "
                f"worker-landing notification wakes you; never foreground-sleep to "
                f"hold the pane. Stand down when a wake finds the pool and your inbox "
                f"empty with no workers in flight.")
    if role == "chuck":
        return (f"{common} You are event-driven: each ⟦SABLE-MSG⟧ PR-ready "
                f"message from a manager is a merge request — review and merge it, then "
                f"report back. Also drain any existing for-chuck beads and run a "
                f"stranded-recovery sweep now, then idle waiting for messages.")
    return common
