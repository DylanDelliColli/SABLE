#!/usr/bin/env python3
"""Identifier-decay sweep — the PROSE half of merged-vs-active (SABLE-x9vby).

An instruction pinned to a specific identifier decays SILENTLY when that
identifier is retired through normal, correct action. The decay is invisible
precisely BECAUSE the retiring action was right: nothing failed, nothing
errored, and the stale instruction still READS as satisfiable. Whoever follows
it does something harmless and wrong, then ships.

The motivating case (chuck, 2026-07-21): SABLE-jejx3's notes carried
"HARD REQUIREMENT: verify gz3v2's suppression still holds after your change, or
migrate the hold...". Lincoln closed SABLE-gz3v2 correctly; the requirement then
pointed at a closed bead with nothing to migrate, while the LIVE hold
(SABLE-3nymz) silently lost its bandage. Caught by hand. This module is the
mechanization of that catch.

WHAT THIS DETECTS (v1):
    an OPEN bead whose text INSTRUCTIONALLY names an identifier that is being
    retired right now — a bead id at `bd close`, a branch name at promote.

*** KNOWN LIMIT — STATED IN THE SHIPPED OUTPUT, NOT ONLY HERE ***
This is a NON-GOAL of v1, not an oversight. The sweep cannot see an instruction
invalidated because a CODE PATH STOPPED BEING REACHED with no identifier
retired anywhere. That is live instance #5 (SABLE-3nymz: jejx3 made
"DO NOT CLOSE, closing re-arms the inverted auto-file" false by moving holds
ahead of predicate 3 — no identifier died, no referenced line changed). A
detector whose limits are undocumented gets TRUSTED PAST THEM, which is worse
than no detector, so `KNOWN_LIMIT` below is printed with every flag.

TUNING: LOOSE, deliberately (lincoln ratified, tarzan directed). The costs are
asymmetric — a false flag costs one read by whoever is closing; a MISS costs a
recycled agent honouring a dead instruction indefinitely, with no way to
discover it is dead, because an instruction's wrongness is invisible from the
instruction. So this errs toward flagging: bare-suffix mentions count, the
instructional vocabulary is wide, and only explicit provenance lines are
excluded. The measured ~0.28 instructional-referrals-per-close figure is
EVIDENCE OF VIABILITY (the hook fires rarely enough to be read), NOT a tuning
target to protect — landing at 1-2 flags/close is fine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

# How many flag lines a single report renders before truncating (loudly — see
# format_flags). Sized from the measured distribution: no non-outlier close in
# the 50-close sample produced more than five.
MAX_RENDERED = 10

# Fields of a bead whose prose can carry an instruction. `bd list --json`
# supplies exactly these three; anything absent is skipped.
SCANNED_FIELDS = ("title", "description", "notes")

# The limit statement that ships WITH every flag (dispatch boundary 1).
KNOWN_LIMIT = (
    "LIMIT (v1): this sees instructions that NAME a retired identifier. It does "
    "NOT see an instruction invalidated because a code path stopped being "
    "reached, with no identifier retired and no referenced line changed "
    "(SABLE-3nymz). Those still need a human. Do not trust this past that line."
)

# Instructional vocabulary — co-location of an identifier with any of these on
# the SAME line is what separates "this bead tells you to do something about X"
# from "this bead mentions X". Deliberately wide (TUNE LOOSE).
INSTRUCTIONAL_RE = re.compile(
    r"(?i)(?<![\w-])("
    r"must|shall|should|need(?:s|ed)?|"
    r"require\w*|verif\w*|migrat\w*|assert\w*|ensure\w*|confirm\w*|check\w*|"
    r"until|before|hold(?:s|ing)?|wait|"
    r"do\s+not|don'?t|never|always|mandatory|hard\s+requirement"
    r")(?![\w-])"
)

# Provenance lines — "this bead is RELATED to X" is a link, not an instruction,
# and flagging every relate-link is the banner-blindness failure mode. Requires
# an explicit separator so ordinary prose starting with one of these words is
# not swallowed.
PROVENANCE_RE = re.compile(
    r"(?i)^[\s\-*>#\d.)\[\]]*("
    r"relates?(?:\s+to)?|related(?:\s+to)?|see\s+also|refs?|references?|"
    r"blocks|blocked\s+by|depends?\s+on|dependenc\w*|"
    r"parent|children|child|epic|supersed\w*|duplicate\s+of|dup\s+of|"
    r"links?|context|source|origin|cf"
    r")\s*[:=\-–—]"
)


@dataclass(frozen=True)
class Flag:
    """One open bead line that instructionally names a retiring identifier."""
    identifier: str      # the identifier being retired
    referrer_id: str     # the OPEN bead still naming it
    referrer_title: str
    field: str           # title | description | notes
    line_no: int         # 1-indexed within that field
    line: str            # the matching line, stripped


def identifier_variants(identifier: str, *, bare_suffix: bool = True) -> list[str]:
    """Surface forms an instruction may use for `identifier`.

    Bead ids are written both fully (`SABLE-gz3v2`) and bare (`gz3v2`) — the
    known-positive line chuck found by hand says "verify gz3v2's suppression
    still holds", with no prefix at all. A full-id-only matcher would MISS the
    exact case this was built for, so the bare suffix counts too whenever it is
    distinctive enough (>= 4 chars) to not collide with ordinary words.

    Branch names get no bare form (`bare_suffix=False`): their trailing segment
    ("target", "guard") is an ordinary English word.
    """
    variants = [identifier]
    if bare_suffix and "-" in identifier:
        suffix = identifier.rsplit("-", 1)[1]
        # a sub-bead suffix ("jd5fj.4") keeps its dotted part
        if len(suffix.split(".")[0]) >= 4:
            variants.append(suffix)
    return variants


def _mention_re(variants: Sequence[str]) -> re.Pattern:
    alts = "|".join(re.escape(v) for v in variants)
    # delimited on both sides; a trailing possessive ("gz3v2's") still matches
    # because "'" is not in the trailing class.
    return re.compile(rf"(?<![A-Za-z0-9._-])(?:{alts})(?![A-Za-z0-9_-])")


def is_provenance(line: str) -> bool:
    """True for link/provenance lines ("RELATES: SABLE-X") that must never flag."""
    return bool(PROVENANCE_RE.match(line))


def is_instructional(line: str) -> bool:
    """True when the line tells the reader to DO something."""
    return bool(INSTRUCTIONAL_RE.search(line))


def sweep_bead(identifier: str, bead: dict, *, bare_suffix: bool = True) -> list[Flag]:
    """Flags contributed by one open bead for one retiring identifier."""
    mention = _mention_re(identifier_variants(identifier, bare_suffix=bare_suffix))
    bead_id = bead.get("id") or ""
    title = bead.get("title") or ""
    flags: list[Flag] = []
    for field in SCANNED_FIELDS:
        text = bead.get(field) or ""
        for line_no, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or not mention.search(line):
                continue
            if is_provenance(line):
                continue
            if not is_instructional(line):
                continue
            flags.append(Flag(identifier=identifier, referrer_id=bead_id,
                              referrer_title=title, field=field,
                              line_no=line_no, line=line))
    return flags


def sweep(identifiers: Iterable[str], beads: Iterable[dict], *,
          bare_suffix: bool = True) -> list[Flag]:
    """Every instructional referral to any retiring identifier in the corpus.

    Beads whose own id is being retired are skipped — a bead's instructions
    about itself are not decay.
    """
    ids = [i for i in identifiers if i]
    retiring = {i.lower() for i in ids}
    flags: list[Flag] = []
    for bead in beads:
        if (bead.get("id") or "").lower() in retiring:
            continue
        for identifier in ids:
            flags.extend(sweep_bead(identifier, bead, bare_suffix=bare_suffix))
    return flags


def _clip(text: str, width: int = 220) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def format_flags(flags: Sequence[Flag], identifiers: Sequence[str],
                 *, max_rendered: int = MAX_RENDERED) -> str:
    """Operator-visible report. Empty string when there is nothing to say.

    Measured against the real corpus the shipped detector averages ~1.2
    flags/close, but the distribution is BIMODAL: ~84% of closes emit zero and a
    rare hub bead emits twenty-plus. Twenty-plus is not readable, and
    readability — not quietness — is the bar. So the render caps, and the cap is
    NEVER silent: the head line always states the true total and the truncation
    line names the command that shows the rest. A silent cap reads as
    "everything is covered" when it isn't, which is the defect class one level up.
    """
    if not flags:
        return ""
    names = ", ".join(identifiers)
    n = len(flags)
    head = (f"⚠ identifier-decay: retiring {names} leaves {n} open "
            f"instruction{'' if n == 1 else 's'} still naming it:")
    lines = [head]
    shown = list(flags[:max_rendered]) if max_rendered > 0 else list(flags)
    for f in shown:
        lines.append(f"    {f.referrer_id} ({f.field}:{f.line_no}) {_clip(f.referrer_title, 80)}")
        lines.append(f"      {_clip(f.line)}")
    if len(shown) < n:
        lines.append(f"    … {n - len(shown)} more not shown — see them all with: "
                     f"sable-identifier-decay {' '.join(identifiers)}")
    lines.append("  Fix each instruction or consciously accept it — it will not "
                 "fail loudly, it will just read as satisfiable forever.")
    lines.append("  " + KNOWN_LIMIT)
    return "\n".join(lines)


def format_unassessed(identifiers: Sequence[str], reason: str) -> str:
    """LOUD on the report (standing discipline 7): a sweep that could not run
    must never be mistaken for a clean one. Fail-open on the DECISION — callers
    never block on this — but say plainly that nothing was checked."""
    names = ", ".join(identifiers) or "(none)"
    return (f"⚠ identifier-decay: COULD NOT ASSESS {names} — {reason}. "
            f"This is NOT a clean result: nothing was checked. Not blocking "
            f"(fail-open); check by hand if the identifier carries instructions.")
