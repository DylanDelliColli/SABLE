#!/usr/bin/env python3
"""sable_footprint_lib — changed-path footprints and disjointness (SABLE-jd5fj.4).

The evidence layer under OPTIMISTIC DISJOINT PROMOTION. When the integration
branch moves while a preview is in flight, the gate's question stops being "did
CI pass?" (it did, on an older base) and becomes "could the base-move and the
branch possibly interact?". This module answers the SECOND question, and only
that one — it never decides anything about a promotion. The decision tree lives
in sable_gate_promote_lib.

WHAT DISJOINTNESS IS AND IS NOT (the locked contract, SABLE-djopw). Disjoint
footprints are NECESSARY, NEVER SUFFICIENT, for skipping re-verification.
`git merge-tree` already proves the merge is TEXTUALLY clean; a textually-clean
merge of file-disjoint changes still breaks semantically all the time (a
signature change plus a new caller, an enum extended in one branch and switched
on in the other, a shared constant). So a disjoint verdict from this module buys
exactly one thing: the right to run the CHEAP impact-scoped tier on the real
combined tree instead of a full re-preview. It never buys skipping the re-test.

Do NOT read SABLE-nueh3's 0/126 measured semantic-break rate as evidence this is
low risk. That measurement was taken under a regime where the exact CI-tested
tree object is the object that fast-forwards, so the failure class was
STRUCTURALLY IMPOSSIBLE, not merely rare. jd5fj.4 is precisely what removes that
structural guarantee. The usable prior is the rule-of-three upper bound (<=2.4%),
which is why the impact tier still runs on every optimistic path.

FOUR RULES THIS MODULE EXISTS TO GET RIGHT
-------------------------------------------
1. RENAMES AND DELETIONS ARE CHANGES. The changed-path set is computed with
   --find-renames and counts BOTH sides of a rename, and it INCLUDES D-status
   paths. A deletion is a change to that path; treating a deleted path as
   "absent, therefore not in the footprint" is the classic way a disjointness
   check goes wrong — the modify/delete pair (one side edits foo.py, the other
   deletes it) is textually clean under some merge strategies and is exactly the
   interaction that must class as OVERLAP.

2. LOCKFILES AND COMMITTED GENERATED ARTIFACTS ARE NON-DISJOINT SENTINELS. They
   are projections of state neither diff describes: two "disjoint" dependency
   bumps that both rewrite a lockfile are not disjoint in any sense that
   matters. A sentinel on EITHER side forces overlap regardless of path
   intersection.

3. WIDER GOVERNS. The mechanical footprint is combined with the planner-declared
   footprint from the bead's `## File footprint` section by UNION, never
   intersection. When the two disagree, the answer is the union of risk. A
   declared footprint that names more than the diff means the planner expected
   blast radius the diff does not show; a diff that touches more than was
   declared means the work grew. Both widen; neither narrows.

4. WRITE-WRITE DISJOINTNESS DOES NOT PROVE INDEPENDENCE (SABLE-jd5fj.18). Two
   changes can touch no file in common and still be coupled: branch A reads a
   file that branch B writes, so A's tested behaviour silently depends on the
   version of that file B is changing underneath it. The file-set check above
   is blind to this — it only ever compared WRITES to WRITES. This module
   therefore also carries a planner-declared READ footprint (`## File reads`,
   parsed the same way as `## File footprint`) and a widened predicate,
   `is_rw_disjoint`, that also checks writes(one side) against reads(the
   other). Reads have no mechanical fallback yet — that is the follow-up bead,
   built on jd5fj.10's structured footprint field — so an ABSENT `## File
   reads` section is not "declares nothing" the way an absent `## File
   footprint` section is. It is a NON-ANSWER, and `declared_reads()` raises
   FootprintUndetermined for it, on purpose: the permanent floor is that an
   undeclared read set fails toward serialization, never toward
   "parallel-safe". A section that IS present, even naming zero paths, is a
   real answer and is returned as an ordinary (possibly empty) Footprint.

FAIL-CLOSED. Every non-answer — an unparseable diff line, an unmerged/unknown
status, a bd read that errors — raises FootprintUndetermined rather than
returning an empty (and therefore vacuously disjoint) footprint. The caller
turns that into "not disjoint", i.e. the pre-jd5fj.4 behaviour: a full
re-preview. An empty footprint means "git said nothing changed"; it never means
"we could not tell".
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass

import sable_gate_git_lib as git_lib


class FootprintUndetermined(Exception):
    """A footprint could not be computed. NEVER downgrade this to an empty
    footprint: empty is disjoint from everything, and a non-answer must not
    become the most permissive answer."""


# --------------------------------------------------------------------------
# Non-disjoint sentinels
# --------------------------------------------------------------------------

# Dependency lockfiles, by basename anywhere in the tree. Two changes that both
# touch one of these are entangled through the resolver even when no other path
# intersects. requirements*.txt is included deliberately: it is not a lockfile
# in the strict sense, but it pins the same shared resolution surface.
LOCKFILE_NAMES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "bun.lockb", "Cargo.lock", "Gemfile.lock", "poetry.lock", "uv.lock",
    "Pipfile.lock", "composer.lock", "go.sum", "go.mod", "flake.lock",
    "mix.lock", "gradle.lockfile", "packages.lock.json", "pdm.lock",
    "conan.lock", "requirements.txt", "requirements-dev.txt",
})

# Committed generated artifacts, by path prefix. A generated tree is a
# projection of sources: two branches can regenerate it from unrelated edits and
# collide in content that neither diff explains. `.beads/` is this repo's own
# instance (bd writes metadata.json + config there).
GENERATED_PREFIXES = (
    "dist/", "build/", "out/", "target/", "vendor/", "node_modules/",
    "generated/", "gen/", ".beads/",
)

# Committed generated artifacts, by filename shape.
GENERATED_SUFFIXES = (
    ".lock", ".min.js", ".min.css", ".pb.go", "_pb2.py", "_pb2_grpc.py",
    ".g.dart", ".snap", ".generated.ts", ".generated.js", ".generated.py",
)


def is_sentinel(path: str) -> bool:
    """True iff touching this path makes a change NON-DISJOINT by itself.

    Sentinels are checked per path, on both sides, before any intersection is
    computed — a sentinel does not need a counterpart on the other side to force
    overlap. That asymmetry is intentional: the entanglement a lockfile or a
    generated tree represents is with the WHOLE repository state, not with a
    specific opposing path."""
    if not path:
        return False
    name = path.rsplit("/", 1)[-1]
    if name in LOCKFILE_NAMES:
        return True
    # Matched with a leading separator on both sides so a generated directory
    # counts wherever it is nested, not only at the repo root.
    if any(f"/{p}" in f"/{path}" for p in GENERATED_PREFIXES):
        return True
    return any(name.endswith(s) for s in GENERATED_SUFFIXES)


# --------------------------------------------------------------------------
# Footprints
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Footprint:
    """A change's blast radius as a set of ENTRIES plus its sentinels.

    An entry ending in '/' is a directory PREFIX (only planner-declared
    footprints produce these — a mechanical diff always yields concrete paths).
    `paths` is the concrete subset, which is what the impact tier is scoped to.
    """
    entries: frozenset[str]
    paths: frozenset[str]
    sentinels: frozenset[str]
    source: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.entries


def footprint(entries, source: str = "") -> Footprint:
    """Build a Footprint from an iterable of entries, classifying sentinels."""
    ents = frozenset(e for e in (x.strip() for x in entries) if e)
    paths = frozenset(e for e in ents if not e.endswith("/"))
    return Footprint(entries=ents,
                     paths=paths,
                     sentinels=frozenset(p for p in paths if is_sentinel(p)),
                     source=source)


def widen(a: Footprint, b: Footprint, source: str = "") -> Footprint:
    """WIDER GOVERNS: combine two footprints by UNION (rule 3 in the module
    docstring). Deliberately not named `intersect` even though the locked
    contract's prose says the mechanical footprint is "intersected with" the
    declared one — the operation the contract specifies is combine-with-wider-
    governing, and a reader who sees set intersection here would implement the
    narrower answer, which is the one unsafe direction."""
    return footprint(a.entries | b.entries, source or f"{a.source}+{b.source}")


# --- mechanical footprint (git) --------------------------------------------

# --name-status lines: a status letter, an optional similarity score (R097),
# a TAB, then one path — or, for R/C, old TAB new.
_STATUS_LINE = re.compile(r"^([A-Z])(\d*)\t(.+)$")

# Statuses whose meaning is a definite, attributable path change.
_SINGLE_PATH_STATUSES = frozenset("AMDT")   # add, modify, DELETE, typechange
_PAIR_PATH_STATUSES = frozenset("RC")       # rename, copy — BOTH sides count


def parse_name_status(out: str) -> frozenset[str]:
    """Parse `git diff --name-status --find-renames` output into a path set.

    Every D-status path is INCLUDED (rule 1). Every R/C record contributes BOTH
    its old and its new path — a rename is a change at the source as much as at
    the destination, and a footprint that named only the destination would call
    a rename disjoint from an edit of the original file.

    A line that does not parse, or an unmerged/unknown status (U, X, B), raises
    FootprintUndetermined. git's stderr is folded into stdout by the shared
    subprocess seam, so a stray warning line is possible — but a diff we cannot
    fully account for is a non-answer, and this module never converts one of
    those into a permissive footprint. Non-matching lines that are pure
    whitespace are the only thing skipped.
    """
    paths: set[str] = set()
    for line in out.splitlines():
        if not line.strip():
            continue
        m = _STATUS_LINE.match(line)
        if not m:
            raise FootprintUndetermined(f"unparseable diff --name-status line: {line!r}")
        status, _score, rest = m.group(1), m.group(2), m.group(3)
        if status in _SINGLE_PATH_STATUSES:
            paths.add(rest.strip())
        elif status in _PAIR_PATH_STATUSES:
            parts = [p.strip() for p in rest.split("\t") if p.strip()]
            if len(parts) != 2:
                raise FootprintUndetermined(f"malformed rename record: {line!r}")
            paths.update(parts)
        else:
            raise FootprintUndetermined(
                f"unmerged or unknown diff status {status!r} in {line!r}")
    return frozenset(paths)


def changed_paths(repo: str, from_sha: str, to_sha: str) -> frozenset[str]:
    """The changed-path set between two commits, renames followed, deletions
    included. Both sides of a comparison are always computed against the SAME
    reference commit (the base the preview was built on), so the two footprints
    are commensurable; if the branch is not actually descended from that
    reference, the extra paths that shows up as only make the footprint wider,
    which is the safe direction."""
    cp = git_lib._git(repo, "-c", "core.quotepath=false", "diff", "--name-status",
                      "--find-renames", "--no-color", from_sha, to_sha, check=False)
    if cp.returncode != 0:
        raise FootprintUndetermined(
            f"git diff {from_sha[:7]}..{to_sha[:7]} failed: {cp.stdout.strip()}")
    return parse_name_status(cp.stdout)


def mechanical_footprint(repo: str, from_sha: str, to_sha: str, source: str = "") -> Footprint:
    return footprint(changed_paths(repo, from_sha, to_sha),
                     source or f"diff {from_sha[:7]}..{to_sha[:7]}")


@dataclass(frozen=True)
class TrackedPathClassification:
    """Dispatch-time existence evidence from one git tree census.

    ``present`` and ``absent`` partition exactly the caller's declarations.
    They say nothing about the ambient filesystem: an untracked file on disk
    is absent from the validated object and therefore absent here.  That is the
    load-bearing distinction for SABLE-35mqf — a phantom claim cannot collide
    with another worker, so filesystem presence would let local residue turn a
    releasing error into a false green.
    """

    present: frozenset[str]
    absent: frozenset[str]


def classify_tracked_paths(repo: str, paths) -> TrackedPathClassification:
    """Partition declared paths by presence in dispatch-time ``HEAD``.

    One ``git ls-tree`` is the authority for the whole declaration.  Concrete
    paths require an exact tracked entry; directory declarations ending in
    ``/`` accept the corresponding tracked tree; slash-bearing directory names
    without the cosmetic trailing slash do too. Broken symlinks and submodules
    count because git tracks the entry regardless of whether its ambient
    target/content can currently be traversed.

    Git failure is a non-answer and raises ``FootprintUndetermined``.  Never
    replace it with an empty tree: that would classify every declared path as
    absent and turn an instrument failure into a false authoring accusation.
    """
    declared = frozenset(path.strip() for path in paths if path.strip())
    if not declared:
        return TrackedPathClassification(frozenset(), frozenset())

    cp = git_lib._git(
        repo,
        "-c", "core.quotepath=false",
        "ls-tree", "-r", "-t", "-z", "--name-only", "HEAD", "--",
        check=False,
    )
    if cp.returncode != 0:
        detail = cp.stdout.strip().replace("\x00", " ")[:240]
        raise FootprintUndetermined(
            "git tree census for declared paths failed at HEAD"
            + (f": {detail}" if detail else ""))

    tracked = frozenset(entry for entry in cp.stdout.split("\x00") if entry)
    present = frozenset(
        path for path in declared
        if ((path.rstrip("/") in tracked
             or any(entry.startswith(path) for entry in tracked))
            if path.endswith("/") else path in tracked)
    )
    return TrackedPathClassification(present, declared - present)


# --- planner-declared footprint (bead metadata) -----------------------------

_FOOTPRINT_HEADING = re.compile(r"^#+\s*File footprint\s*$", re.IGNORECASE)
_READS_HEADING = re.compile(r"^#+\s*File reads\s*$", re.IGNORECASE)
_PARENTHETICAL = re.compile(r"\([^)]*\)")
_CODE_SUFFIXES = (".py", ".sh", ".yml", ".yaml", ".json", ".md", ".ts", ".js", ".toml")

# The fleet-wide convention (hooks/test/test-optimistic-promotion.sh,
# bin/sable-spawn-worker's tag_footprint_metadata, and this module's own
# tests) for writing an EXPLICITLY empty section: a lone 'none' line under
# the heading. It is not path-shaped and is correctly never added to
# `entries`, but it is also not an authoring error the SABLE-zx2yv
# completeness check should flag — it is the deliberate spelling of "this
# section is declared, and empty", so it must not count as a `dropped` token.
_EMPTY_SECTION_MARKERS = frozenset({"none"})


@dataclass(frozen=True)
class FootprintSectionParse:
    """Lossless evidence from one kind of declared-footprint section.

    `entries` is the accepted path-shaped set.  `dropped` is equally
    load-bearing at DISPATCH time: a partial parse can be narrower than the
    author intended, so callers without a mechanical diff fallback must refuse
    it rather than treating the accepted subset as complete.  `found` keeps an
    absent heading distinct from a present empty/unreadable declaration.
    """

    found: bool
    entries: frozenset[str]
    dropped: frozenset[str]


def _collect_section(description: str, heading: re.Pattern[str]
                     ) -> tuple[bool, frozenset[str], frozenset[str]]:
    """Shared prose-section extractor for the bead-description sub-sections
    this module parses (`## File footprint`, `## File reads`).

    Returns (found, entries, dropped): `found` says whether the heading
    appeared at all, which the caller needs to tell "not declared" apart from
    "declared as empty" (SABLE-jd5fj.18) — a distinction `parse_declared_footprint`'s
    caller does not need, because the mechanical footprint already governs the
    write side, but `parse_declared_reads`'s caller very much does, because
    reads have no such fallback.

    Parenthetical asides are commentary. Commas/newlines delimit declaration
    entries; all path-shaped tokens are retained, a short annotation after a
    path is ignored, and tokens before the first path (or in a fragment with no
    path) are reported in `dropped`. That last set is what lets dispatch-time
    callers reject an incomplete parse rather than silently narrowing it.

    `dropped` is the SABLE-zx2yv completeness signal: every non-empty token
    that was not part of a recognised path/annotation form and is not a
    recognised "declared empty" marker (`_EMPTY_SECTION_MARKERS`, e.g. a lone
    'none' line) — notably a bare filename like 'Makefile'. A caller with no
    mechanical fallback for narrowing must treat non-empty `dropped` as "this
    declaration could not be fully understood", never as complete. Optimistic
    promotion's write side may ignore it only because a mechanical diff is
    always unioned behind it."""
    # Every matching section contributes.  Appended corrections are common in
    # long-lived bead descriptions; stopping at the first heading made later
    # corrections dead text (SABLE-9dmuu).  Another heading ends only the
    # current body, not the search for a later matching section.
    bodies: list[list[str]] = []
    body: list[str] | None = None
    for line in description.splitlines():
        stripped = line.strip()
        if heading.match(stripped):
            if body is not None:
                bodies.append(body)
            body = []
            continue
        if body is not None:
            if stripped.startswith("#"):
                bodies.append(body)
                body = None
            else:
                body.append(line)
    if body is not None:
        bodies.append(body)
    if not bodies:
        return False, frozenset(), frozenset()

    entries: set[str] = set()
    dropped: set[str] = set()
    for section in bodies:
        text = _PARENTHETICAL.sub(" ", "\n".join(section))
        # Commas and newlines are declaration boundaries. Within one fragment,
        # a path may carry a short trailing annotation (`hooks/a.sh
        # implementation`); those words are commentary, matching the
        # pre-consolidation accepted authoring form. Words BEFORE the first
        # path remain `dropped`: `Likely hooks/a.sh` is an uncertain/partial
        # declaration and cannot be laundered into a complete one. A fragment
        # with NO path-shaped token (notably bare `Makefile`) is wholly dropped.
        for line in text.splitlines():
            for fragment in line.split(","):
                tokens = [
                    raw.strip().strip("`*-—;:").rstrip(".")
                    for raw in fragment.split()
                ]
                tokens = [tok for tok in tokens if tok]
                path_positions = [
                    index for index, tok in enumerate(tokens)
                    if "/" in tok or tok.endswith(_CODE_SUFFIXES)
                ]
                if path_positions:
                    entries.update(tokens[index] for index in path_positions)
                    for tok in tokens[:path_positions[0]]:
                        if tok.lower() not in _EMPTY_SECTION_MARKERS:
                            dropped.add(tok)
                else:
                    dropped.update(
                        tok for tok in tokens
                        if tok.lower() not in _EMPTY_SECTION_MARKERS)
    return True, frozenset(entries), frozenset(dropped)


def parse_footprint_section(description: str) -> FootprintSectionParse:
    """Public, lossless `## File footprint` parser shared by every dispatch
    consumer (SABLE-rzrak/SABLE-mh967).

    Do not replace this with a bare set projection in a dispatch gate: doing so
    collapses absent, unreadable, and partially-readable declarations into the
    same permissive shape.  Optimistic promotion's mechanical-diff-backed
    projection remains `parse_declared_footprint` below.
    """

    found, entries, dropped = _collect_section(description, _FOOTPRINT_HEADING)
    return FootprintSectionParse(found, entries, dropped)


def parse_declared_footprint(description: str) -> frozenset[str]:
    """Entries from a bead description's `## File footprint` section. An
    absent section is treated as "declares nothing" — see _collect_section —
    because the mechanical footprint governs the write side regardless.

    A dropped token (SABLE-zx2yv) is silently ignored here, on purpose: the
    write side's guarantee never depended on the prose parser catching every
    token, only on the mechanical footprint being unioned in afterward — see
    declared_footprint()."""
    return parse_footprint_section(description).entries


def parse_declared_reads(description: str) -> tuple[bool, frozenset[str]]:
    """Entries from a bead description's `## File reads` section
    (SABLE-jd5fj.18) — the read-side counterpart to `## File footprint`.

    Returns (declared, entries). `declared` is False when the heading is
    absent (the read set is UNKNOWN, not empty) and True when the heading is
    present, even with zero entries (the planner explicitly said "this reads
    nothing beyond its own footprint"). The caller — declared_reads() below —
    must treat "not declared" as a non-answer, unlike parse_declared_footprint's
    caller: writes always have a mechanical fallback, and reads, today, do
    not (mechanical/import-graph derivation of reads is the follow-up bead).

    This function's 2-tuple return is a stable public contract other callers
    (bin/sable-spawn-worker's tag_footprint_metadata) unpack directly, so it
    deliberately does NOT surface the `dropped`-token completeness signal
    _collect_section now tracks (SABLE-zx2yv) — declared_reads() below reads
    _collect_section itself to get at that third element instead of widening
    this signature."""
    declared, entries, _ = _collect_section(description, _READS_HEADING)
    return declared, entries


def _read_bead(repo: str, bead: str) -> dict:
    """Read <bead> via the bd seam ONCE, preferring `--json` so the structured
    footprint fields (SABLE-jd5fj.10) are visible. Returns
    {"description": str, "metadata": dict}.

    A bd stub that ignores `--json` and emits raw prose (every fixture that
    predates this bead, and any real bd whose `show` output is not valid JSON
    for some other reason) is NOT an error: its whole stdout becomes
    `description`, `metadata` stays `{}`, and the structured path is simply
    never consulted — the prose parser takes over exactly as it did before
    this function existed. Only a genuine bd FAILURE (non-zero exit) raises."""
    cp = git_lib._run(git_lib._tool("SABLE_MG_BD", "bd") + ["show", bead, "--json"],
                      cwd=repo, check=False)
    if cp.returncode != 0:
        raise FootprintUndetermined(f"bd show {bead} failed: {cp.stdout.strip()[:200]}")
    try:
        data = json.loads(cp.stdout)
    except (json.JSONDecodeError, ValueError):
        return {"description": cp.stdout, "metadata": {}}
    record = data[0] if isinstance(data, list) and data else data
    if not isinstance(record, dict):
        return {"description": cp.stdout, "metadata": {}}
    return {"description": record.get("description") or "",
            "metadata": record.get("metadata") or {}}


def _metadata_entries(metadata: dict, key: str) -> frozenset[str] | None:
    """The trichotomy (tarzan's ruling, SABLE-jd5fj.10) applied to one flat
    string-list metadata key: `None` means the key is ABSENT (undeclared /
    not run — the caller must fail toward the next fallback, never toward an
    empty footprint); a present key returns its comma-split entries, which may
    themselves be an empty frozenset (declared, found nothing). Collapsing
    the `None` case into `frozenset()` here would be the exact writer-side
    collapse this schema exists to prevent, just moved into the reader."""
    if key not in metadata:
        return None
    raw = metadata.get(key) or ""
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def declared_footprint(repo: str, bead: str) -> Footprint:
    """The planner-declared footprint for <bead>.

    Prefers the structured `footprint_writes` metadata field (SABLE-jd5fj.10)
    over the `## File footprint` prose section: the field, when present, is
    used VERBATIM and the prose parser is not consulted at all. A bead
    dispatched before this field existed carries no such key, so this falls
    back to the prose parser exactly as before — the fallback is not a
    degraded mode, it is the ordinary path for every pre-field bead.

    Raises FootprintUndetermined when bd cannot be read at all. That is
    fail-closed on purpose: without the declared footprint we cannot honour
    "wider governs", and promoting on a possibly-narrower mechanical answer is
    the one direction this bead is not allowed to take. A bd that ANSWERS with
    no structured field and no prose footprint section is a different thing
    entirely — that is an empty declared footprint, and the mechanical one
    governs."""
    if not bead:
        raise FootprintUndetermined("no bead id — cannot read the declared footprint")
    record = _read_bead(repo, bead)
    structured = _metadata_entries(record["metadata"], "footprint_writes")
    if structured is not None:
        return footprint(structured, source=f"declared:{bead}:field")
    return footprint(parse_declared_footprint(record["description"]),
                     source=f"declared:{bead}:prose")


def declared_reads(repo: str, bead: str) -> Footprint:
    """The planner-declared READ footprint for <bead> (SABLE-jd5fj.18): the
    files this branch's behaviour depends on, whether or not it writes them.

    Prefers the structured `footprint_reads_declared` metadata field
    (SABLE-jd5fj.10) over the `## File reads` prose section, same precedence
    as declared_footprint(). The trichotomy is carried through unchanged: a
    present-but-empty structured field ("declared, found nothing") behaves
    exactly like a present-but-empty prose section — both return an ordinary
    empty Footprint — while an ABSENT structured field falls back to the
    prose parser, and only if THAT is also absent does this raise.

    Unlike declared_footprint(), an ABSENT read declaration (structured AND
    prose) is NOT "declares nothing" — it raises FootprintUndetermined. This
    is the floor this bead builds: reads have no mechanical fallback yet, so
    silence about what a branch reads must fail toward serialization exactly
    like an unparseable diff does, never toward the old silent 'parallel-safe'
    default.

    SABLE-zx2yv: the floor above catches TOTAL silence (heading absent) but
    used to miss PARTIAL silence — a `## File reads` section that IS present
    but contains a token the tokenizer cannot recognise as a path (a bare
    repo-root filename like 'Makefile', a directory name with no trailing
    slash, an extensionless script) was consumed as a complete answer,
    silently narrowing the declared read set with nothing behind it. This
    reads _collect_section directly (rather than going through
    parse_declared_reads) to get at its `dropped` token set and raises the
    same FootprintUndetermined a missing heading would, rather than trusting
    an incompletely-tokenized section as complete. Only the PROSE path needs
    this: the structured `footprint_reads_declared` metadata field above is
    consumed verbatim, never re-tokenized, so it cannot suffer this gap."""
    if not bead:
        raise FootprintUndetermined("no bead id — cannot read the declared read footprint")
    record = _read_bead(repo, bead)
    structured = _metadata_entries(record["metadata"], "footprint_reads_declared")
    if structured is not None:
        return footprint(structured, source=f"declared-reads:{bead}:field")
    declared, entries, dropped = _collect_section(record["description"], _READS_HEADING)
    if not declared:
        raise FootprintUndetermined(
            f"no declared read footprint (field or '## File reads' section) for "
            f"{bead} — undeclared read set forces serialization (SABLE-jd5fj.18 floor)")
    if dropped:
        raise FootprintUndetermined(
            f"'## File reads' section for {bead} contains unrecognised token(s) "
            f"{', '.join(sorted(dropped))} — the parser cannot tell whether these "
            f"name real paths, so the declared read set may be narrower than "
            f"intended (SABLE-zx2yv: present-but-incomplete forces serialization "
            f"exactly like an absent heading, never toward a trusted-complete set)")
    return footprint(entries, source=f"declared-reads:{bead}:prose")


# --------------------------------------------------------------------------
# Disjointness
# --------------------------------------------------------------------------

def _entry_covers(entry: str, other: str) -> bool:
    """True iff `entry` names `other` or contains it. Directory prefixes cover
    everything beneath them; concrete paths cover only themselves."""
    if entry == other:
        return True
    prefix = entry if entry.endswith("/") else entry + "/"
    return other.startswith(prefix)


@dataclass(frozen=True)
class Disjointness:
    disjoint: bool
    reason: str
    overlap: tuple[str, ...] = ()


def is_disjoint(a: Footprint, b: Footprint) -> Disjointness:
    """Can these two changes possibly touch the same thing?

    NOT disjoint if either side touches a sentinel (rule 2), or if any entry on
    one side names or contains an entry on the other. Everything else is
    disjoint — which, per the module docstring, licenses an impact-scoped
    re-test on the combined tree and nothing more."""
    sentinels = tuple(sorted(a.sentinels | b.sentinels))
    if sentinels:
        return Disjointness(False,
                            f"non-disjoint sentinel touched (lockfile / committed generated "
                            f"artifact): {', '.join(sentinels)}",
                            sentinels)
    hits = sorted({e for x in a.entries for y in b.entries
                   if _entry_covers(x, y) or _entry_covers(y, x)
                   for e in (x, y)})
    if hits:
        return Disjointness(False, f"footprints intersect at: {', '.join(hits)}", tuple(hits))
    return Disjointness(True, "footprints are disjoint (no shared path, no sentinel)")


def is_rw_disjoint(writes_a: Footprint, reads_a: Footprint,
                   writes_b: Footprint, reads_b: Footprint) -> Disjointness:
    """safe_to_parallel(A, B) == writes(A) disjoint writes(B)
                            AND writes(A) disjoint reads(B)
                            AND writes(B) disjoint reads(A)
    (SABLE-jd5fj.18 — the floor layer under optimistic disjoint promotion).

    Write-write disjointness (rule 4 in the module docstring) only rules out
    two changes editing the same file; it says nothing about A reading a file
    B writes, which is exactly the coupling that let two footprint-disjoint
    branches promote in parallel when they were not independent. Checked in
    this order, with the reason naming WHICH conjunct failed, because "I could
    not tell" and "I can tell, and they conflict" must stay distinguishable in
    the report even though both resolve to the same NOT-disjoint decision."""
    ww = is_disjoint(writes_a, writes_b)
    if not ww.disjoint:
        return Disjointness(False, f"write/write: {ww.reason}", ww.overlap)
    a_writes_b_reads = is_disjoint(writes_a, reads_b)
    if not a_writes_b_reads.disjoint:
        return Disjointness(False,
                            f"read/write coupling (B reads what A writes): {a_writes_b_reads.reason}",
                            a_writes_b_reads.overlap)
    b_writes_a_reads = is_disjoint(writes_b, reads_a)
    if not b_writes_a_reads.disjoint:
        return Disjointness(False,
                            f"read/write coupling (A reads what B writes): {b_writes_a_reads.reason}",
                            b_writes_a_reads.overlap)
    return Disjointness(True,
                        "writes and declared reads are mutually disjoint (no shared path, no sentinel)")


@dataclass(frozen=True)
class Assessment:
    """The whole footprint story for one stale-base promote attempt.

    `disjoint` is a THREE-valued answer: True / False / None, where None means
    undetermined. The caller must treat None exactly like False — the tri-state
    exists so the EVIDENCE can distinguish "we looked and they overlap" from "we
    could not look", not so the two can be acted on differently."""
    disjoint: bool | None
    reason: str
    paths: tuple[str, ...] = ()
    branch: Footprint | None = None
    base_move: Footprint | None = None


def assess(repo: str, bead: str, base_sha: str, branch_sha: str,
           new_base_sha: str) -> Assessment:
    """Is the branch's footprint disjoint from the base-move's footprint?

    Both sides are diffed against base_sha — the commit the green preview was
    built on — so they describe changes to the same starting state. The branch
    side is then WIDENED by the planner-declared footprint from the bead before
    the comparison; the base-move side has no bead and stays mechanical.

    SABLE-jd5fj.18: write-write disjointness is checked first and, if it
    already fails, decides the answer exactly as before — nothing about that
    path changed. Only when the writes ARE disjoint does this go on to ask the
    second question: does the branch's declared READ footprint intersect what
    the base-move WROTE? The base-move side has no bead of its own (it may be
    the union of several already-merged branches), so there is no declared
    read set to ask for on that side — this floor only checks the direction
    the concrete defect exposed, branch-reads vs base-move-writes; the
    symmetric direction is out of scope (see the module docstring, rule 4).

    Any failure anywhere yields disjoint=None with the reason attached, never an
    exception and never a permissive default. An undeclared read footprint is
    exactly such a failure — there is no mechanical fallback for reads yet, so
    silence about what the branch reads is a non-answer, not an empty one."""
    try:
        branch_fp = widen(
            mechanical_footprint(repo, base_sha, branch_sha, source="branch-diff"),
            declared_footprint(repo, bead),
            source="branch",
        )
        base_fp = mechanical_footprint(repo, base_sha, new_base_sha, source="base-move")
    except FootprintUndetermined as exc:
        return Assessment(None, f"footprint undetermined — treating as NON-disjoint: {exc}")

    paths = tuple(sorted(branch_fp.paths | base_fp.paths))
    ww_verdict = is_disjoint(branch_fp, base_fp)
    if not ww_verdict.disjoint:
        return Assessment(False, ww_verdict.reason, paths, branch_fp, base_fp)

    try:
        branch_reads = declared_reads(repo, bead)
    except FootprintUndetermined as exc:
        return Assessment(None, f"read footprint undetermined — treating as NON-disjoint: {exc}",
                          paths, branch_fp, base_fp)

    rw_verdict = is_rw_disjoint(branch_fp, branch_reads, base_fp, footprint(()))
    if not rw_verdict.disjoint:
        return Assessment(False, rw_verdict.reason, paths, branch_fp, base_fp)

    # `paths` deliberately stays the CHANGED-path union (branch writes + base
    # move), not widened by the read footprint: reads() only informs the
    # disjointness verdict, and the impact tier this feeds is scoped to what
    # actually changed in the combined tree, not to paths nobody touched.
    return Assessment(
        disjoint=True,
        reason=rw_verdict.reason,
        paths=paths,
        branch=branch_fp,
        base_move=base_fp,
    )


# --------------------------------------------------------------------------
# Dispatch-record CLI — shell hooks consume the same parser as Python callers
# --------------------------------------------------------------------------

_SCAVENGE_FILE_RE = re.compile(
    r"(?:^|[\s\(\[\"'])("
    r"(?:[\w\-./]+/)?[\w\-./]+\."
    r"(?:ts|tsx|js|jsx|py|rs|go|java|rb|md|yaml|yml|toml|json|sh|sql|css|scss|html)"
    r")(?=[\s\)\]\"',:;]|$)",
    re.MULTILINE,
)


def scavenge_file_tokens(text: str) -> frozenset[str]:
    """Pre-convention fallback for descriptions with NO footprint heading.

    It is intentionally not used once an author supplied a declaration: a
    malformed declaration must be repaired, not laundered by unrelated prose.
    """

    return frozenset(match.group(1) for match in _SCAVENGE_FILE_RE.finditer(text))


def dispatch_record_lines(record: dict, *, scavenge: bool = False) -> tuple[str, ...]:
    """Render one bead record as the established `c`/`f`/`u` line protocol.

    `c0|c1` is the current wip-claim presence bit used by the claim writer,
    `f<path>` is accepted evidence, and `u<reason>` is a declaration that was
    present but could not be read completely.  A dispatch consumer must refuse
    when ANY `u` line exists, even if some `f` lines were salvaged.
    """

    if not isinstance(record, dict):
        raise ValueError("dispatch record is not an object")
    description = record.get("description", "") or ""
    if not isinstance(description, str):
        raise ValueError("dispatch record description is not a string")
    metadata = record.get("metadata", {}) or {}
    if not isinstance(metadata, dict):
        raise ValueError("dispatch record metadata is not an object")

    raw_claims = metadata.get("wip_claims", "") or ""
    if not isinstance(raw_claims, str):
        raise ValueError("dispatch record wip_claims is not a string")
    current_claims = raw_claims.rstrip("\n")
    files = {part.strip() for part in current_claims.split(",") if part.strip()}
    unreadable: list[str] = []
    if current_claims.strip() and not files:
        unreadable.append("wip_claims metadata")

    section = parse_footprint_section(description)
    if section.found:
        files.update(section.entries)
        if section.dropped:
            unreadable.append(
                "'## File footprint' section rejected token(s): "
                + ", ".join(sorted(section.dropped)))
        elif not section.entries:
            unreadable.append("'## File footprint' section")
    elif scavenge:
        files.update(scavenge_file_tokens(description))

    lines = ["c1" if current_claims else "c0"]
    lines.extend("f" + path for path in sorted(files))
    lines.extend("u" + reason for reason in unreadable)
    return tuple(lines)


def _dispatch_record_from_stdin() -> dict:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid dispatch record JSON: {exc}") from exc
    if isinstance(data, list):
        if len(data) != 1:
            raise ValueError("dispatch record JSON must contain exactly one record")
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("dispatch record JSON is not an object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SABLE footprint evidence parser")
    parser.add_argument("--read-dispatch-record", action="store_true",
                        help="read one bd-show JSON record and emit c/f/u evidence")
    parser.add_argument("--scavenge", action="store_true",
                        help="use the legacy file-token fallback only when no heading exists")
    args = parser.parse_args(argv)
    if not args.read_dispatch_record:
        parser.error("--read-dispatch-record is required")
    try:
        lines = dispatch_record_lines(
            _dispatch_record_from_stdin(), scavenge=args.scavenge)
    except ValueError as exc:
        print(f"footprint dispatch record error: {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
