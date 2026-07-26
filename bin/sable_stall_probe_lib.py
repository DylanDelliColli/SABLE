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
"""
from __future__ import annotations

import re

from sable_pane_lib import pane_busy, session_limit_reset

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
