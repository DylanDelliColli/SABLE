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

*** SCOPE CORRECTION, SABLE-l662t (measured 2026-07-24) ***
Tuning loose is right; matching the wrong THING is not, and v1 conflated them.
Closing the completed 9-child epic SABLE-be4lo reported 61 hits of which
approximately zero were real decay, because the detector matched the SPELLING of
a retired identifier rather than the property it exists to enforce — "an
INSTRUCTION whose precondition is this identifier being LIVE". Three independent
defects, fixed separately because they fail differently:

  LAYER 1  a sub-bead id is its parent's id plus ".N", so every child reference
           read as a mention of the parent. 45 of 52 flagged lines, measured.
           Fixed in _mention_re.
  LAYER 2  "co-located on the same LINE" was the proxy for "in the same
           instruction", but a bead description is one line per PARAGRAPH — the
           triggering token sat up to 427 characters away, and was usually the
           NOUN "checker". Fixed in is_instructional_near, which NARROWS
           classification only: demoted mentions are still reported, as
           CITATIONs. Separating is safe, suppressing is not.
  LAYER 3  the truncation line named the bare command that had just truncated,
           so "see them all with: ..." looped. Fixed in remedy_command.

The measured effect on the corpus that produced the bug: 52 lines reported as
instructions -> 2 instructions + 5 citations, with every known positive intact.
A false flag still costs only one read, so the asymmetry above is UNCHANGED —
what changed is that the flags now describe the retirement that actually
happened. The failure direction is stated at each fix; the paired negative
controls in test_identifier_decay.py are what keep a future "just make it
quieter" patch from blinding the detector, which would pass every
no-false-positives test and be strictly worse than the noise.
"""
from __future__ import annotations

import re
import shlex
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


# Sentence boundary. LAYER 2, SABLE-l662t — see is_instructional_near().
# Enders only (`.!?;` followed by whitespace). A comma/em-dash split was
# measured against the live corpus and cleared no additional false positive
# while adding a real false-negative shape ("DO NOT CLOSE — SABLE-X is
# unmerged"), so it is deliberately NOT here.
SENTENCE_END_RE = re.compile(r"(?<=[.!?;])\s+")

# The two classes a mention can fall into. An INSTRUCTION has a truth value
# that the retirement can falsify; a CITATION does not.
INSTRUCTION = "instruction"
CITATION = "citation"


@dataclass(frozen=True)
class Flag:
    """One open bead line that names a retiring identifier.

    `kind` is INSTRUCTION (real decay — something to fix or consciously accept)
    or CITATION (a reference to history, which never decays). See
    is_instructional_near() for how the two are told apart, and why the
    citations are reported rather than dropped.
    """
    identifier: str      # the identifier being retired
    referrer_id: str     # the OPEN bead still naming it
    referrer_title: str
    field: str           # title | description | notes
    line_no: int         # 1-indexed within that field
    line: str            # the matching line, stripped
    kind: str = INSTRUCTION


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
    # Delimited on both sides; a trailing possessive ("gz3v2's") still matches
    # because "'" is not in the trailing class.
    #
    # LAYER 1, SABLE-l662t — `(?!\.\d)` is the prefix-collision fix. A sub-bead
    # id is its PARENT's id plus ".N", so a matcher delimited only on
    # [A-Za-z0-9_-] reads every "SABLE-x.7" as a mention of SABLE-x. It is not:
    # the children are retired on their own schedule, days apart, and closing
    # the parent retires nothing of theirs. Measured over the live corpus when
    # the 9-child epic SABLE-be4lo closed: 45 of 52 flagged lines mentioned ONLY
    # a child. The inflation scales with child count, so it was worst for
    # exactly the closes that matter most — completed epics.
    #
    # The exclusion is `.` + DIGIT, deliberately, not a bare `.`: "verify
    # SABLE-x." ending a sentence is a real mention, and dropping it would be
    # the blinding failure this fix must not commit (see the paired controls in
    # test_identifier_decay.py).
    return re.compile(rf"(?<![A-Za-z0-9._-])(?:{alts})(?!\.\d)(?![A-Za-z0-9_-])")


def is_provenance(line: str) -> bool:
    """True for link/provenance lines ("RELATES: SABLE-X") that must never flag."""
    return bool(PROVENANCE_RE.match(line))


def is_instructional(line: str) -> bool:
    """True when the line tells the reader to DO something."""
    return bool(INSTRUCTIONAL_RE.search(line))


def sentence_spans(line: str) -> list[tuple[int, str]]:
    """(offset, text) for each sentence-ish span of `line`, covering it exactly."""
    spans: list[tuple[int, str]] = []
    pos = 0
    for m in SENTENCE_END_RE.finditer(line):
        spans.append((pos, line[pos:m.start()]))
        pos = m.end()
    spans.append((pos, line[pos:]))
    return spans


def is_instructional_near(line: str, mention: re.Pattern) -> bool:
    """True when an instructional token shares a SENTENCE with a mention.

    LAYER 2, SABLE-l662t. The original predicate asked whether the instructional
    vocabulary appeared anywhere on the same LINE as the identifier — and a bead
    description is one "line" per PARAGRAPH. Measured on the live corpus, the
    token that made each false positive fire sat 128, 161, 304, 422 and 427
    characters and several sentences away from the mention; four of the seven
    were the NOUN "check"/"checker" in narrative prose ("the checker is a good
    instrument"), not an instruction about anything. Co-location on a paragraph
    is not co-location in a clause, so the scope tightens from line to sentence.

    *** THE FAILURE DIRECTION IS CHOSEN ON PURPOSE. *** A missed instruction is
    the expensive error — it silently keeps a decayed instruction live, and an
    instruction's wrongness is invisible from the instruction. So:

      * this NARROWS classification only, never the sweep. A mention demoted
        here is still returned by sweep() and still reported, as a CITATION.
        Nothing is suppressed; the two classes are separated and only
        instructions are counted. Separating is safe, suppressing is not.
      * the split is sentence enders only, the most conservative rule that
        cleared the measured noise (5 of 7 false positives; the 2 survivors are
        comma-joined clauses, left flagging rather than risk the cut).
      * it is a HEURISTIC and will misfile some fraction. When it does, the
        misfiled item is visible under --all with its full line, so a reader who
        suspects a miss can see everything the pre-fix tool would have shown.
    """
    spans = sentence_spans(line)
    for m in mention.finditer(line):
        for start, text in spans:
            if start <= m.start() < start + len(text):
                if INSTRUCTIONAL_RE.search(text):
                    return True
                break
    return False


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
            # The line-level gate is UNCHANGED: a mention with no instructional
            # vocabulary anywhere on the line is still not reported at all.
            # Layer 2 only decides which CLASS the survivors land in, which
            # bounds the citation class to what the pre-fix tool already
            # printed — the fix cannot re-introduce noise through a new door.
            if not is_instructional(line):
                continue
            kind = INSTRUCTION if is_instructional_near(line, mention) else CITATION
            flags.append(Flag(identifier=identifier, referrer_id=bead_id,
                              referrer_title=title, field=field,
                              line_no=line_no, line=line, kind=kind))
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


def remedy_command(identifiers: Sequence[str],
                   remedy_flags: Sequence[str] = ("--all",)) -> str:
    """The command that shows EVERYTHING this report summarised.

    LAYER 3, SABLE-l662t. This used to render the bare invocation — the exact
    command that had just truncated — so a reader who followed "see them all
    with: ..." got the same ten lines and the same message, forever. A remedy
    that loops is worse than no remedy: it spends the reader's trust before
    their attention. Every caller-visible flag of the original invocation is
    reproduced here (--branch, -C) so the printed command sweeps for the same
    thing the report above it describes, and every part is shell-quoted so it
    can be pasted and run rather than merely read.
    """
    parts = ["sable-identifier-decay", *remedy_flags, *identifiers]
    return " ".join(shlex.quote(p) for p in parts)


def format_flags(flags: Sequence[Flag], identifiers: Sequence[str],
                 *, max_rendered: int = MAX_RENDERED,
                 remedy_flags: Sequence[str] = ("--all",)) -> str:
    """Operator-visible report. Empty string when there is nothing to say.

    Measured against the real corpus the shipped detector averages ~1.2
    flags/close, but the distribution is BIMODAL: ~84% of closes emit zero and a
    rare hub bead emits twenty-plus. Twenty-plus is not readable, and
    readability — not quietness — is the bar. So the render caps, and the cap is
    NEVER silent: the head line always states the true total and the truncation
    line names the command that shows the rest. A silent cap reads as
    "everything is covered" when it isn't, which is the defect class one level up.

    SABLE-l662t: INSTRUCTIONS and CITATIONS are counted and rendered separately.
    Only instructions are decay, so only instructions raise the ⚠ banner and
    only they are counted in the head. Citations are stated as a one-line
    non-actionable tail and shown in full under --all — they are separated from
    the actionable class, never suppressed. When the retirement leaves ONLY
    citations (the measured live case: 61 reported, ~0 real) the report drops to
    a quiet informational notice, because there is nothing for the closer to do.
    """
    if not flags:
        return ""
    instructions = [f for f in flags if f.kind == INSTRUCTION]
    citations = [f for f in flags if f.kind != INSTRUCTION]
    names = ", ".join(identifiers)
    remedy = remedy_command(identifiers, remedy_flags)
    show_all = max_rendered <= 0

    def render(items: Sequence[Flag]) -> list[str]:
        out = []
        for f in items:
            out.append(f"    {f.referrer_id} ({f.field}:{f.line_no}) "
                       f"{_clip(f.referrer_title, 80)}")
            out.append(f"      {_clip(f.line)}")
        return out

    n = len(instructions)
    if n == 0:
        # Nothing to fix. One quiet line instead of a wall of warnings — but the
        # count and the way to read them are still stated, so "no instructions"
        # can never be confused with "nothing was found".
        c = len(citations)
        lines = [f"ℹ identifier-decay: retiring {names} — {c} historical "
                 f"citation{'' if c == 1 else 's'} name it (past evidence, not "
                 f"instructions) and no open instruction depends on it. Nothing to "
                 f"fix. {'Listed below:' if show_all else 'See them with: ' + remedy}"]
        if show_all:
            lines.extend(render(citations))
        lines.append("  " + KNOWN_LIMIT)
        return "\n".join(lines)

    head = (f"⚠ identifier-decay: retiring {names} leaves {n} open "
            f"instruction{'' if n == 1 else 's'} still naming it:")
    lines = [head]
    shown = list(instructions) if show_all else list(instructions[:max_rendered])
    lines.extend(render(shown))
    if len(shown) < n:
        lines.append(f"    … {n - len(shown)} more not shown — see them all with: {remedy}")
    if citations:
        c = len(citations)
        # No trailing punctuation after a command — a printed command has to be
        # pastable, which is the whole of layer 3's lesson.
        tail = "listed below:" if show_all else f"see them with: {remedy}"
        lines.append(f"  Plus {c} historical citation{'' if c == 1 else 's'} of "
                     f"{names} (past evidence, not instructions — nothing to fix), "
                     f"{tail}")
        if show_all:
            lines.extend(render(citations))
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
