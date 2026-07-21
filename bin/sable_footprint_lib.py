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

THREE RULES THIS MODULE EXISTS TO GET RIGHT
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

FAIL-CLOSED. Every non-answer — an unparseable diff line, an unmerged/unknown
status, a bd read that errors — raises FootprintUndetermined rather than
returning an empty (and therefore vacuously disjoint) footprint. The caller
turns that into "not disjoint", i.e. the pre-jd5fj.4 behaviour: a full
re-preview. An empty footprint means "git said nothing changed"; it never means
"we could not tell".
"""
from __future__ import annotations

import re
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


# --- planner-declared footprint (bead metadata) -----------------------------

_FOOTPRINT_HEADING = re.compile(r"^#+\s*File footprint\s*$", re.IGNORECASE)
_PARENTHETICAL = re.compile(r"\([^)]*\)")
_CODE_SUFFIXES = (".py", ".sh", ".yml", ".yaml", ".json", ".md", ".ts", ".js", ".toml")


def parse_declared_footprint(description: str) -> frozenset[str]:
    """Entries from a bead description's `## File footprint` section.

    The section is prose written by a planner, not a machine format, so parsing
    is deliberately lossy in the WIDENING direction only: parenthetical asides
    are dropped (they are commentary), tokens are split on commas and
    whitespace, and a token is kept if it looks like a path (contains '/' or
    ends in a known code suffix). A prose fragment that survives as a bogus
    entry can only ADD to the footprint, which costs a full re-preview at worst;
    a real path that is missed leaves the mechanical footprint governing, which
    is the status quo. Neither direction can narrow the mechanical answer."""
    lines = description.splitlines()
    body: list[str] = []
    collecting = False
    for line in lines:
        if _FOOTPRINT_HEADING.match(line.strip()):
            collecting = True
            continue
        if collecting:
            if line.strip().startswith("#"):
                break
            body.append(line)
    if not collecting:
        return frozenset()
    text = _PARENTHETICAL.sub(" ", "\n".join(body))
    entries: set[str] = set()
    for raw in re.split(r"[,\s]+", text):
        tok = raw.strip().strip("`*-—;:").rstrip(".")
        if not tok:
            continue
        if "/" in tok or tok.endswith(_CODE_SUFFIXES):
            entries.add(tok)
    return frozenset(entries)


def declared_footprint(repo: str, bead: str) -> Footprint:
    """The planner-declared footprint for <bead>, read via the bd seam.

    Raises FootprintUndetermined when bd cannot be read at all. That is
    fail-closed on purpose: without the declared footprint we cannot honour
    "wider governs", and promoting on a possibly-narrower mechanical answer is
    the one direction this bead is not allowed to take. A bd that ANSWERS with a
    description containing no footprint section is a different thing entirely —
    that is an empty declared footprint, and the mechanical one governs."""
    if not bead:
        raise FootprintUndetermined("no bead id — cannot read the declared footprint")
    cp = git_lib._run(git_lib._tool("SABLE_MG_BD", "bd") + ["show", bead],
                      cwd=repo, check=False)
    if cp.returncode != 0:
        raise FootprintUndetermined(f"bd show {bead} failed: {cp.stdout.strip()[:200]}")
    return footprint(parse_declared_footprint(cp.stdout), source=f"declared:{bead}")


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

    Any failure anywhere yields disjoint=None with the reason attached, never an
    exception and never a permissive default."""
    try:
        branch_fp = widen(
            mechanical_footprint(repo, base_sha, branch_sha, source="branch-diff"),
            declared_footprint(repo, bead),
            source="branch",
        )
        base_fp = mechanical_footprint(repo, base_sha, new_base_sha, source="base-move")
    except FootprintUndetermined as exc:
        return Assessment(None, f"footprint undetermined — treating as NON-disjoint: {exc}")
    verdict = is_disjoint(branch_fp, base_fp)
    return Assessment(
        disjoint=verdict.disjoint,
        reason=verdict.reason,
        paths=tuple(sorted(branch_fp.paths | base_fp.paths)),
        branch=branch_fp,
        base_move=base_fp,
    )
