"""sable_stall_probe_lib — structural (not lexical) hold/stall classification
for night-stall-probe.py (SABLE-cod50).

WHY STRUCTURAL, NOT LEXICAL (SABLE-bbq28 contract line c, recurring INSIDE
the instrument that was line c's own fix). The probe's original
`deliberate_idle()` decided "did this manager choose to hold?" by substring-
matching the pane against a hardcoded marker list ("nothing dispatchable",
"event-driven", "standing by", ...). A deliberate hold can be phrased
unboundedly many ways -- optimus's real hold text ("standing down -- a wake
will come from a landing, a message, or the fresh Lincoln") was semantically
a hold but lexically absent from the list, so the probe read it as a dropped
wake and would have nudged a manager that had already correctly decided to
stop. Growing the list only defers the next miss.

A dropped wake has ONE shape instead of unbounded phrasings: either the wake
never left the composer (still sitting there unsubmitted), or it landed and
no turn output rendered before the composer went idle again (a truncated
turn, including a session-rate-limit cut mid-turn). This module detects that
SHAPE directly rather than enumerating its lexical complement.

Reuses bin/sable_pane_lib's canonical pane-state primitives (pane_busy,
session_limit_reset) instead of re-deriving them -- the original probe
reimplemented a crude local BUSY_MARKER check rather than importing the
shared one, the same enumerate-vs-derive mistake one layer up.

WHY THE MANAGER-RESOLUTION AND VERDICT HALVES LIVE HERE TOO (SABLE-r69ho).
night-stall-probe.py is GITIGNORED and worktree-local, so nothing in CI can
ever import it and no suite on a fresh checkout can assert anything about it.
Its exit-1 STALL path was structurally unreachable for an unknown length of
time and no test noticed, because there was no test that COULD. *** A CHECK
THAT CANNOT FIRE IS INDISTINGUISHABLE FROM A CONDITION NEVER MET *** -- which
is the bead's own thesis, one layer up.

So the two properties that must never silently regress -- managers resolve by
@sable_role tag rather than by pane id, and the STALL verdict is REACHABLE --
are implemented here, in the tracked module, where test_sable_stall_probe.py
asserts them on every run on every machine. The subprocess leg of that suite
drives the real script where it exists and DIFFERENTIALLY asserts it agrees
with these functions, so the two cannot drift unobserved; where the script is
absent that leg skips loudly and these assertions still hold.

*** THESE FUNCTIONS ARE NOT YET CALLED BY night-stall-probe.py, AND THAT IS
DELIBERATE, NOT AN OVERSIGHT. *** The live script resolves its imports from
the MAIN checkout's bin/, not from this branch, so wiring it to a symbol that
only exists here would raise ImportError in the cockpit the moment it ran --
converting a working instrument into a permanently-DEGRADED one, which is the
exact failure r69ho was. It lands inert; adoption is SABLE-4udwf, and the
tracking gap that forces the whole arrangement is SABLE-0m78j.
"""
from __future__ import annotations

import re

from sable_pane_lib import pane_busy, session_limit_reset

MANAGER_ROLES = ("optimus", "tarzan", "chuck")

_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Composer/box furniture: blank, a box-drawing border row, or the shell-cwd
# footer line (`  user@host:~/path`) printed below the composer. None of
# these are turn CONTENT, so they must not count as evidence a turn ran.
_BORDER_RE = re.compile(r"^[\s\-─│╭╮╰╯]*$")
_CWD_LINE_RE = re.compile(r"^\s*\S+@\S+:")

SABLE_MSG_MARK = "⟦SABLE-MSG⟧"


def _clean(line: str) -> str:
    return _CTRL_RE.sub("", line).strip()


def _is_chrome(line: str) -> bool:
    """A pane row that is composer/box furniture, not turn content."""
    if not line:
        return True
    if _BORDER_RE.match(line):
        return True
    if _CWD_LINE_RE.match(line):
        return True
    return False


def deliberate_hold(capture: str) -> bool | None:
    """Classify an idle manager pane's tail as a deliberate hold or a
    dropped-wake stall, by SHAPE rather than by matching known hold phrases.

    Returns:
      True  -- a settled, empty composer with rendered turn output before
               it: a deliberate hold, in any phrasing whatsoever.
      False -- the stall shape: an unanswered wake still sitting unsent in
               the composer, an empty composer with no turn content
               rendered since the last event (a truncated turn), or a turn
               cut short by the session-rate-limit banner.
      None  -- the pane is not in a settled idle-composer state at all
               (still busy, or no composer row is locatable in the
               captured window) -- COULD-NOT-ASSESS for this axis, never
               guessed (the silent-instrument contract).
    """
    if pane_busy(capture):
        return None

    cleaned = [_clean(line) for line in capture.splitlines()]

    bare_idx = None
    glyph_idx = None
    for i, line in enumerate(cleaned):
        if line.startswith(("❯", ">")):  # ❯ or >
            glyph_idx = i
            if line in ("❯", ">"):
                bare_idx = i

    if glyph_idx is None:
        return None  # no composer row in the captured window at all

    if glyph_idx != bare_idx:
        # The bottom-most composer row still holds unsubmitted text: the box
        # never went back to bare. A wake sitting there unsent is exactly
        # the dropped-wake shape -- nothing can act on a message that was
        # never even sent.
        return False if SABLE_MSG_MARK in cleaned[glyph_idx] else None

    above = [ln for ln in reversed(cleaned[:bare_idx]) if not _is_chrome(ln)]
    if not above:
        return False  # empty composer, nothing rendered above it: a
        # truncated turn -- the other dropped-wake shape.

    recent = "\n".join(reversed(above))
    if session_limit_reset(recent) is not None:
        return False  # a turn cut short by the rate-limit banner still
        # repaints a normal-looking empty composer below it.

    return True


def resolve_manager_panes(list_panes_text: str,
                          roles: tuple[str, ...] = MANAGER_ROLES
                          ) -> dict[str, str]:
    """{role: pane_id} for the manager roles present in a live pane dump.

    `list_panes_text` is the stdout of
        tmux list-panes -a -F '#{pane_id} #{@sable_role}'
    -- the same enumeration the probe already performs for its worker-pane
    axis, one extra format field, no new mechanism.

    *** DERIVE, NEVER ENUMERATE (SABLE-r69ho, SABLE-hadqx class). *** This
    replaced a literal `{"%1": "optimus", "%2": "tarzan", "%3": "chuck"}`.
    tmux pane ids are session-lifetime-unique and MONOTONICALLY INCREASING --
    never reused, and re-issued on every pane recreation, worker spawn/reap
    and cockpit recycle -- so a hardcoded map is correct only for the first
    fleet ever launched in a session and is stale forever after. It was stale:
    the live ids were %10/%11/%12 while the map named %1/%2/%3, so all three
    manager axes read COULD-NOT-ASSESS on every run and the probe's exit-1
    STALL path became structurally unreachable.

    Returns ONLY the roles it actually found. A role absent from the dump is
    absent from this result and is NOT reported here as anything -- turning
    that into an explicit COULD-NOT-ASSESS is manager_axis_states()'s job.
    The split is deliberate; see that function's docstring for why collapsing
    the two is the strictly-worse bug.
    """
    found: dict[str, str] = {}
    for line in list_panes_text.splitlines():
        parts = line.split()
        # A pane with no @sable_role tag prints its id and an empty field, so
        # it splits to one token and is skipped -- an untagged pane is not a
        # manager, and must never be guessed into one.
        if len(parts) >= 2 and parts[1] in roles and parts[1] not in found:
            found[parts[1]] = parts[0]
    return found


def manager_axis_states(resolved: dict[str, str], capture,
                        roles: tuple[str, ...] = MANAGER_ROLES
                        ) -> tuple[dict[str, str], list[str]]:
    """Per-manager BUSY / IDLE / COULD-NOT-ASSESS, plus the degraded reasons.

    `resolved` is resolve_manager_panes()'s output; `capture(pane_id)` returns
    `(text, err)` with text None when the pane could not be read.

    *** THIS ITERATES `roles`, NEVER `resolved`, AND THAT IS THE WHOLE POINT.
    *** Deriving the map must not become a way to silently DROP a manager that
    failed to register. The bug this replaces failed LOUD -- every axis
    unreadable, DEGRADED, exit 2 -- which is diagnosable in one run. A naive
    derivation returns whatever it happens to find, so a two-entry map sails
    straight through `all(v == "IDLE" ...)` and the probe reports RUNNING
    BECAUSE IT FORGOT A MANAGER EXISTS. That is silent-healthy in place of
    loud-broken: strictly worse than the defect being fixed, and invisible.

    So every role in `roles` gets an entry, always; an axis that could not be
    read is COULD-NOT-ASSESS and carries a reason naming the roles that DID
    resolve, so the failure points at itself.
    """
    states: dict[str, str] = {}
    degraded: list[str] = []
    for name in roles:
        pane = resolved.get(name)
        if pane is None:
            states[name] = "COULD-NOT-ASSESS"
            degraded.append(
                f"{name}: no live pane carries @sable_role={name} "
                f"(resolved roles: {sorted(resolved) or 'none'})")
            continue
        text, err = capture(pane)
        if text is None:
            states[name] = "COULD-NOT-ASSESS"
            degraded.append(f"{name} pane {pane} unreadable: {err}")
            continue
        states[name] = "BUSY" if pane_busy(text) else "IDLE"
    return states, degraded


def stall_verdict(states: dict[str, str], held: dict[str, bool | None],
                  ready: int | None, in_flight: int | None, cap: int,
                  degraded=()) -> tuple[str, int]:
    """(verdict, exit_code) for the probe's three-way outcome.

      DEGRADED       2  at least one axis could not be assessed -- verdict
                        WITHHELD. Checked first and unconditionally: an
                        instrument that cannot read an axis must never score
                        it, in either direction (the silent-instrument
                        contract). DEGRADED outranks STALL.
      STALL          1  every manager IDLE, the fleet below cap, and at least
                        one manager idle with NO stated hold decision. That
                        last conjunct is the dropped-wake shape; see
                        deliberate_hold().
      RUNNING (HELD) 0  every manager IDLE but ALL of them by decision -- the
                        correct terminal state of a drain, not a stall.
      RUNNING        0  anything else, i.e. somebody is working.

    `ready` is retained as context only and is NOT a gate: `bd ready` counts
    beads with no blocker, not beads that are DISPATCHABLE, and the two
    diverge exactly at the end of a drain, so gating on it false-STALLs on a
    correctly-held pool (measured 2026-07-24 ~03:43).

    `in_flight` is the in_progress count, the CEILING on running work -- not
    the pane count, which is a floor that undercounts bundled dispatches
    (SABLE-42k96). Gating on the ceiling means the probe can only ever be
    conservative about capacity.
    """
    if degraded or ready is None or in_flight is None:
        return "DEGRADED", 2
    all_idle = bool(states) and all(v == "IDLE" for v in states.values())
    below_cap = in_flight < cap
    stuck = [n for n, h in held.items() if h is False]
    if all_idle and below_cap and stuck:
        return "STALL", 1
    if all_idle and ready > 0 and not stuck:
        return "RUNNING (HELD)", 0
    return "RUNNING", 0
