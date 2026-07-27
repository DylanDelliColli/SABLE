#!/usr/bin/env python3
"""sable_activation_debt_lib — the install/activation obligation ledger (SABLE-3zjc1).

THE DEFECT THIS EXISTS FOR
--------------------------
This fleet has two post-landing obligation classes and tracks exactly one of
them. A GATE-CLASS RE-PIN is owed at a named trigger, written into a protocol,
recorded as bead metadata, and verified by object hash — a re-pin that is owed
is VISIBLE AS OWED. An INSTALL is owed whenever a landing changes a file whose
consumed copy is a COPY rather than a live symlink, and there is no ledger, no
count, no trigger and no acceptance check: the obligation exists only in
whatever prose the landing seat happened to write.

The measured consequence, 2026-07-26: THREE landed-but-inert fixes accumulated
behind one P2, and each read DONE in every instrument the fleet owns — bead
closed, code on the spine, `sable-contained` passing, merge report green —
while doing nothing where it is consumed. "How much of what we landed today is
actually running?" was a question no instrument here could answer, and the
error is always in the optimistic direction.

Landed-and-inert is a DEFERRED IMPROVEMENT, not a regression: nothing broke,
which is exactly why each of the three was correctly deprioritised on its own
merits. A debt that is individually always-defensible and collectively
unbounded is the precise case for a ledger, not for a seventh catalogued way
activation can fail (we had six already, and the count still went to three).

THE FOUR PROPERTIES THAT MAKE THIS A LEDGER AND NOT A REPORT
------------------------------------------------------------
P1  MECHANICAL CREATION. An obligation comes out of the diff (which paths
    changed) crossed with the ownership table below (which of those have a
    consumed copy that is a copy). No seat prose is an input. See
    `obligations_for_landing`.

P2  THE ACCEPTANCE IS A BEHAVIOUR CHECK AT THE CONSUMED PATH, AND IS
    INDEPENDENT OF THE REFERENCE THE INSTALL USED. The second half is the
    strengthening tarzan added within an hour of filing, from a live instance:
    chuck's first orchestration-install ran with the checkout at 0c52680 while
    the spine was d5eae21, so it installed PRE-fix content, exited 0, and was
    silent. His acceptance was "live matches HEAD" — a check AT THE CONSUMED
    PATH, it hashes the installed file — AND IT PASSED, because the installer
    had copied from that same stale tree and the thing being verified and the
    thing doing the verifying were derived from one corrupted reference. A
    hash-equality acceptance cannot detect staleness it SHARES with the
    operation. Only `grep -c timeout` — which asks "does the installed file
    HAVE THE PROPERTY WE INSTALLED IT FOR?" rather than "does it match some
    reference?" — disagreed. See `AcceptanceKind` and `validate_acceptance`.

P3  WHICH TOOL DISCHARGES IT, AND WHAT AUTHORIZATION THAT TOOL REQUIRES. The
    column nobody asked for until the class was worked. The install obligation
    is not one queue behind one broken installer; it is SEVERAL installers with
    different scopes and different blast radii, and which tool owns a given
    file IS NOT DISCOVERABLE FROM THE FILE. Worked instance: SABLE-z95e2 lands
    hooks/tdd-evidence.sh and the authorized sable-orchestration-install run
    could not possibly have activated it — that installer never mentions the
    file, zero hits across its entire output. install.sh owns it, and install.sh
    additionally writes hook REGISTRATIONS into settings.json (install.sh:414),
    a materially larger operator decision. THE DANGEROUS PROPERTY IS THE
    SILENCE: omitting a file it does not manage is CORRECT behaviour for the
    orchestration installer and emits no warning, no skip line, nothing. The
    run reports exit 0 and "all files identical to source", which is true of
    its own scope and says nothing about the file you cared about. Both lincoln
    and chuck made exactly that error in one exchange. So an obligation whose
    discharging mechanism cannot be identified FROM THE OBLIGATION ITSELF is
    worse than an untracked one: you can believe it discharged by watching the
    wrong tool succeed. `discharge_scan` therefore never consults an
    installer's exit status — it runs the acceptance, always.

P4  THE LIFECYCLE IS NOT THE BEAD'S. Closing the work bead does not clear the
    debt; all three founding instances have CLOSED beads. Structurally, no
    function in this module reads bead status at all, and `open_obligations`
    takes the status map only so a test can prove the output is invariant
    under it.

THE DISPATCH-TIME LEG (optimus, 2026-07-26)
-------------------------------------------
The same missing data bites one step EARLIER. The dispatch screen asks "can
these run in parallel" (footprint disjointness) and never "can this land at all
right now". Under an unconstrained regime the two predicates coincide closely
enough that nobody notices; under a constrained one — operator asleep, installs
frozen, hot-swap class held — they diverge sharply, and roughly 90% of bin/
could not land on the night this was found. A branch was dispatched that was
un-landable by construction, avoidable with one `readlink` nobody thought to
run because no screen asks that question.

The input is identical to the ledger's, so `landability` is a lookup over the
same classification rather than a third view of one missing primitive. Its
NEGATIVE CONTROL is load-bearing: a footprint of pure test files and repo-only
paths must come back FREE. A predicate that classifies everything as hot-swap
would stop all dispatch under a constrained regime — worse than the gap it
replaces, and the same always-serialize erosion recorded on SABLE-47try.

THE BOUND ON EVERY ANSWER HERE, STATED BECAUSE AN EMPTY RESULT LOOKS THE SAME
AS A CLEAN ONE
-----------------------------------------------------------------------------
`INERT` from this module means "NO INSTALLER IN `OWNERSHIP` CLAIMS THIS PATH",
not "this path is provably consumed nowhere". The ownership table IS the bound.
A fourth installer, or a scope this table gets wrong, reports INERT — the
optimistic direction — so the table is the thing to audit when the ledger looks
suspiciously empty. `describe_bounds()` prints it, and `classify_path` returns
UNRESOLVED rather than guessing whenever the consumed path cannot be read.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import sable_snapshot_lib as snapshot_lib

LEDGER_FILE = "activation-debt.jsonl"

# A derived acceptance property must be at least this long to be trusted as
# distinctive. A three-character added line ("fi", "})") greps non-zero against
# almost any file, so an acceptance built on one is a green that could not have
# failed — the exact failure shape this module exists to remove.
MIN_PROPERTY_LEN = 16


class ActivationDebtError(Exception):
    """Base for this module's refusals."""


class AcceptanceTooWeak(ActivationDebtError):
    """Raised when an acceptance would report success while the fix is inert."""


# --------------------------------------------------------------------------
# Activation classes — what a landing does, or fails to do, at the consumed path
# --------------------------------------------------------------------------

class ActivationClass(str, Enum):
    """How a path's consumed copy responds to a landing.

    HOT_SWAP      the consumed copy resolves INTO a live working tree of this
                  repo, so the pull that lands the branch IS the activation.
                  No install is owed — and the branch cannot land at all under
                  a regime that holds hot-swaps for an operator-present window.
    INSTALL_OWED  an installer claims the path and the consumed copy is not a
                  live link into a checkout: a plain copy, a pinned snapshot
                  symlink resolving outside the checkout, or nothing installed
                  yet. All three need an install to activate.
    INERT         no installer in the table claims the path. It lands freely
                  and activates nowhere. See the bound in the module docstring.
    UNRESOLVED    the consumed path could not be read. Never guessed at as
                  either of the others.
    """
    HOT_SWAP = "hot-swap"
    INSTALL_OWED = "install-owed"
    INERT = "inert"
    UNRESOLVED = "unresolved"


# Fail-closed severity for folding many paths into one verdict: UNRESOLVED
# outranks HOT_SWAP because an unreadable consumed path is not evidence of
# freedom to land.
_CLASS_SEVERITY = {
    ActivationClass.INERT: 0,
    ActivationClass.INSTALL_OWED: 1,
    ActivationClass.HOT_SWAP: 2,
    ActivationClass.UNRESOLVED: 3,
}


# --------------------------------------------------------------------------
# P3: the ownership table — which tool discharges, and what it costs to run
# --------------------------------------------------------------------------

BIN_INSTALL = "sable-bin-install"
ORCHESTRATION_INSTALL = "sable-orchestration-install"
BASE_INSTALL = "install.sh"

# Authorization strings are part of the record, not decoration: an obligation
# that names its installer but not its blast radius still lets an operator
# authorize a much larger operation than they meant to.
AUTH_BIN = ("user scope ~/.local/bin; symlinks into the checkout by default, "
            "but a pinned snapshot resolves outside it and needs a re-pin")
AUTH_ORCHESTRATION = ("orchestration scope (--user writes the LIVE ~/.claude, "
                      "SABLE-2avau); merges multi-manager rows into settings.json")
AUTH_BASE = ("OPERATOR DECISION — install.sh also writes base-tier hook "
             "REGISTRATIONS into settings.json (install.sh:414), a materially "
             "larger blast radius than the orchestration installer")


@dataclass(frozen=True)
class OwnershipRule:
    """One installer's claim over a family of repo paths.

    `consumed` is a POSIX-join template resolved against a root; `{name}` is the
    path's basename. `root` selects which install root it hangs off, matching
    how the installer itself derives its destination — so a scratch HOME moves
    both the installer and this table together.
    """
    installer: str
    authorization: str
    pattern: str
    root: str          # "claude" | "local_bin"
    consumed: str


# Ordered: FIRST MATCH WINS, so hooks/multi-manager/*.sh must precede
# hooks/*.sh or the orchestration layer would be attributed to install.sh —
# an ownership error in the direction that makes you watch the wrong tool.
OWNERSHIP: tuple[OwnershipRule, ...] = (
    OwnershipRule(ORCHESTRATION_INSTALL, AUTH_ORCHESTRATION,
                  "hooks/multi-manager/*.sh", "claude",
                  "hooks/multi-manager/{name}"),
    OwnershipRule(ORCHESTRATION_INSTALL, AUTH_ORCHESTRATION,
                  "templates/multi-manager/roles/*.md", "claude",
                  "sable/roles/{name}"),
    OwnershipRule(ORCHESTRATION_INSTALL, AUTH_ORCHESTRATION,
                  "templates/multi-manager/agents.yaml", "claude",
                  "sable/agents.yaml"),
    OwnershipRule(BASE_INSTALL, AUTH_BASE,
                  "hooks/*.sh", "claude", "hooks/{name}"),
    OwnershipRule(BASE_INSTALL, AUTH_BASE,
                  "agents/*.md", "claude", "agents/{name}"),
    OwnershipRule(BIN_INSTALL, AUTH_BIN,
                  "bin/sable-*", "local_bin", "{name}"),
)


@dataclass(frozen=True)
class Roots:
    """Where the installers put things. Derived from the environment exactly as
    the installers derive it, so pointing HOME at a scratch dir relocates the
    table and the tools together — the only way to exercise this against a real
    installed layout without writing to the developer's live ~/.claude
    (SABLE-2avau: `sable-orchestration-install --user` silently targets the
    LIVE ~/.claude with no escape; SABLE-k0nvp: a pinning suite polluted the
    real ~/.local twice)."""
    claude: str
    local_bin: str

    def resolve(self, root: str) -> str:
        if root == "claude":
            return self.claude
        if root == "local_bin":
            return self.local_bin
        raise ActivationDebtError(f"unknown install root {root!r}")


def roots_from_env(env: dict | None = None) -> Roots:
    env = os.environ if env is None else env
    home = env.get("HOME") or os.path.expanduser("~")
    claude = env.get("CLAUDE_USER_DIR") or os.path.join(home, ".claude")
    local_bin = env.get("SABLE_BIN_DEST") or os.path.join(home, ".local", "bin")
    return Roots(claude=claude, local_bin=local_bin)


@dataclass(frozen=True)
class ConsumedSite:
    """One place a repo path is consumed from, and who put it there."""
    repo_path: str
    consumed_path: str
    installer: str
    authorization: str


# A multi-manager hook reaching for a sibling library: "../../bin/<lib>.py".
# The installer copies the lib to wherever that reference NORMALIZES TO from
# the INSTALLED hook dir (bin/sable-orchestration-install:336) — which is why
# bin/sable_inline_body_guard_lib.py is consumed from ~/.claude/bin/ and not
# from ~/.claude/hooks/multi-manager/. Deriving the site instead of hardcoding
# it keeps this table honest when a hook changes how deep it reaches.
_SIBLING_REF = re.compile(r"((?:\.\./)+)bin/([A-Za-z0-9_]+\.py)")


def _sibling_lib_sites(repo: str, repo_path: str, roots: Roots) -> list[ConsumedSite]:
    """Sites for a bin/*.py consumed as a multi-manager hook's sibling library.

    Scans the repo's hooks for a reference to this library rather than checking
    whether a copy happens to exist already: a BRAND-NEW lib has no installed
    copy yet and is exactly the case where the obligation matters most.
    """
    name = os.path.basename(repo_path)
    hook_dir = os.path.join(roots.claude, "hooks", "multi-manager")
    sites: list[ConsumedSite] = []
    seen: set[str] = set()
    try:
        hooks = sorted(Path(repo, "hooks", "multi-manager").glob("*.sh"))
    except OSError:
        return sites
    for hook in hooks:
        try:
            body = hook.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for updirs, lib in _SIBLING_REF.findall(body):
            if lib != name:
                continue
            target = os.path.normpath(os.path.join(hook_dir, updirs, "bin", lib))
            if target in seen:
                continue
            seen.add(target)
            sites.append(ConsumedSite(repo_path, target,
                                      ORCHESTRATION_INSTALL, AUTH_ORCHESTRATION))
    return sites


def _skill_name(repo: str, skill_dir: str) -> str:
    """A skill installs under its frontmatter `name:`, which may differ from its
    repo directory (bin/sable-orchestration-install:638)."""
    try:
        for line in Path(repo, skill_dir, "SKILL.md").read_text(
                encoding="utf-8", errors="replace").splitlines():
            if line.startswith("name:"):
                stripped = line.split(":", 1)[1].strip()
                if stripped:
                    return stripped
    except OSError:
        pass
    return os.path.basename(skill_dir.rstrip("/"))


def consumed_sites(repo: str, repo_path: str, roots: Roots | None = None) -> list[ConsumedSite]:
    """Every place `repo_path` is consumed from. Empty means nothing in the
    table claims it — see the bound in the module docstring."""
    roots = roots_from_env() if roots is None else roots
    rel = repo_path.strip().lstrip("./")

    # skills/<dir>/<file> -> {claude}/skills/<frontmatter name>/<file>
    parts = rel.split("/")
    if len(parts) == 3 and parts[0] == "skills":
        name = _skill_name(repo, "skills/" + parts[1])
        return [ConsumedSite(rel,
                             os.path.join(roots.claude, "skills", name, parts[2]),
                             ORCHESTRATION_INSTALL, AUTH_ORCHESTRATION)]

    if rel.startswith("bin/") and rel.endswith(".py"):
        return _sibling_lib_sites(repo, rel, roots)

    for rule in OWNERSHIP:
        if not fnmatch.fnmatch(rel, rule.pattern):
            continue
        # bin/sable-* is the shell-entrypoint rule; *.py is skill-bundled and
        # explicitly skipped by sable-bin-install (bin/sable-bin-install:87).
        if rule.installer == BIN_INSTALL and rel.endswith(".py"):
            continue
        consumed = rule.consumed.format(name=os.path.basename(rel))
        return [ConsumedSite(rel, os.path.join(roots.resolve(rule.root), consumed),
                             rule.installer, rule.authorization)]
    return []


# --------------------------------------------------------------------------
# Classification — measured at the consumed path, never inferred from the name
# --------------------------------------------------------------------------

def live_roots(repo: str) -> tuple[str, ...]:
    """Every working tree of this repo, realpath'd.

    NOT just `repo`. The live symlink that makes a path hot-swappable points at
    whichever checkout the operator installed from — measured live,
    ~/.local/bin/sable-hook-matrix -> /home/ddc/dev-env/SABLE/bin/, while the
    worker deciding landability sits in a wk-* worktree. Comparing against the
    caller's own tree alone would call that a plain copy and report an install
    owed for a file that activates on merge: wrong class, wrong tool, and wrong
    answer to "can this land tonight".
    """
    try:
        cp = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return (os.path.realpath(repo),)
    if cp.returncode != 0:
        return (os.path.realpath(repo),)
    found = [os.path.realpath(line[len("worktree "):].strip())
             for line in cp.stdout.splitlines() if line.startswith("worktree ")]
    return tuple(dict.fromkeys(found)) or (os.path.realpath(repo),)


def classify_site(site: ConsumedSite, roots_live: tuple[str, ...]) -> ActivationClass:
    """The `readlink -f` comparison the bead prescribes, and NOT the shape check
    beside it.

    "Is it a symlink?" is the tempting cheap probe and it is wrong in the
    reporting direction: sable-bin-install can PIN a tool as a symlink into a
    ~/.local/lib/sable-<sha>/ snapshot (bin/sable-bin-install:266), which is a
    symlink that does NOT hot-swap — a landing leaves it serving the pinned sha.
    Resolving the link and asking whether it lands inside a live checkout tells
    the two apart; asking whether it IS a link does not.
    """
    try:
        if not os.path.lexists(site.consumed_path):
            # Claimed by an installer, nothing there yet — and the two reasons
            # for that need OPPOSITE answers.
            #   The layer IS installed at this scope and this ONE file is
            #   missing: a brand-new artifact whose first install is genuinely
            #   owed. That is the case the ledger most needs to catch, so it
            #   must not read as INERT merely because nothing is there to
            #   compare.
            #   The layer was NEVER installed at this scope at all (a consumer
            #   project, a fresh machine): nothing consumes these paths, so
            #   every one of them would stamp debt that no install is actually
            #   owed for. That is a ledger firing on ordinary landings until a
            #   seat learns to skip it — the SABLE-r5pfw erosion, worse than no
            #   ledger.
            # The install DIRECTORY tells them apart. A new artifact under a
            # directory that does not exist yet lands on the INERT side; that
            # is the known optimistic edge of this rule, and it is named in
            # describe_bounds() rather than left to be rediscovered.
            if os.path.isdir(os.path.dirname(site.consumed_path)):
                return ActivationClass.INSTALL_OWED
            return ActivationClass.INERT
        resolved = os.path.realpath(site.consumed_path)
    except OSError:
        return ActivationClass.UNRESOLVED
    for root in roots_live:
        if resolved == os.path.realpath(os.path.join(root, site.repo_path)):
            return ActivationClass.HOT_SWAP
    return ActivationClass.INSTALL_OWED


@dataclass(frozen=True)
class PathVerdict:
    repo_path: str
    activation: ActivationClass
    sites: tuple[ConsumedSite, ...] = ()

    @property
    def installers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(s.installer for s in self.sites))


def classify_path(repo: str, repo_path: str, roots: Roots | None = None,
                  roots_live: tuple[str, ...] | None = None) -> PathVerdict:
    """One path's activation class, folded over all of its consumed sites."""
    sites = consumed_sites(repo, repo_path, roots)
    if not sites:
        return PathVerdict(repo_path.strip().lstrip("./"), ActivationClass.INERT, ())
    live = live_roots(repo) if roots_live is None else roots_live
    classes = [classify_site(s, live) for s in sites]
    worst = max(classes, key=lambda c: _CLASS_SEVERITY[c])
    return PathVerdict(sites[0].repo_path, worst, tuple(sites))


# --------------------------------------------------------------------------
# P2: acceptance — a behaviour check whose truth the install cannot fake
# --------------------------------------------------------------------------

class AcceptanceKind(str, Enum):
    """The five kinds, INCLUDING the two that are refused.

    Naming the refused kinds is the point: SHAPE and HASH_EQUALITY are the
    cheap ones a hurried seat reaches for, and both report success while the
    fix is inert. A vocabulary that omitted them would leave the distinction in
    prose, which is where it was when it failed.
    """
    PROPERTY_GREP = "property-grep"      # names the content the fix adds
    BEHAVIOUR_EXEC = "behaviour-exec"    # runs the installed artifact
    HASH_EQUALITY = "hash-equality"      # shares the install's reference
    SHAPE = "shape"                      # exists / is-a-symlink / is-executable
    MISSING = "missing"                  # nothing derivable — permanently open


# Ref-independent: their truth does not depend on which tree anybody copied
# from, because they name the PROPERTY rather than a reference.
REF_INDEPENDENT = frozenset({AcceptanceKind.PROPERTY_GREP,
                             AcceptanceKind.BEHAVIOUR_EXEC})


@dataclass(frozen=True)
class Acceptance:
    """A check recorded as the argv that will actually run it.

    The command IS the check — `acceptance_command` builds the argv and
    `run_acceptance` executes that same argv — so the line a seat reads in the
    report cannot drift from the line the ledger judged by.
    """
    kind: AcceptanceKind
    consumed_path: str
    prop: str = ""                          # PROPERTY_GREP: the literal content
    argv: tuple[str, ...] = ()              # BEHAVIOUR_EXEC: what to run
    expect: str = ""                        # BEHAVIOUR_EXEC: expected in output
    expect_rc: int | None = 0
    ref: str = ""                           # HASH_EQUALITY: the NAMED ref
    paired: "Acceptance | None" = None      # HASH_EQUALITY: its property check

    @property
    def is_behaviour_check(self) -> bool:
        """A behaviour check asks what the artifact DOES or CONTAINS. A shape
        check asks what it IS. Only the first can distinguish an activated fix
        from an inert one."""
        return self.kind in REF_INDEPENDENT

    @property
    def is_ref_independent(self) -> bool:
        """True when the check's truth does not depend on the reference the
        install used. HASH_EQUALITY is false here even when it names a ref —
        naming the ref narrows which staleness it can share, it does not remove
        the sharing."""
        return self.kind in REF_INDEPENDENT


def validate_acceptance(acceptance: Acceptance) -> Acceptance:
    """Refuse every acceptance that can pass while the fix is inert.

    SHAPE and MISSING are refused outright. HASH_EQUALITY is refused ALONE and
    accepted only when paired with a ref-independent property check, and only
    against a NAMED ref — never an unqualified "HEAD", which is whatever the
    local checkout happens to be and is precisely the variable that went wrong
    in the live instance.
    """
    if acceptance.kind is AcceptanceKind.SHAPE:
        raise AcceptanceTooWeak(
            "a shape check (exists / is-a-symlink / is-executable) reports the "
            "activation MODEL, not whether the fix activated")
    if acceptance.kind is AcceptanceKind.MISSING:
        raise AcceptanceTooWeak(
            "no acceptance was derivable; the obligation stays open rather "
            "than being discharged by an unstated check")
    if not acceptance.consumed_path:
        raise AcceptanceTooWeak("acceptance must name the CONSUMED path")
    if acceptance.kind is AcceptanceKind.PROPERTY_GREP:
        if len(acceptance.prop) < MIN_PROPERTY_LEN:
            raise AcceptanceTooWeak(
                f"property {acceptance.prop!r} is under {MIN_PROPERTY_LEN} "
                "chars — too common to distinguish an activated file from any "
                "other file")
    if acceptance.kind is AcceptanceKind.BEHAVIOUR_EXEC and not acceptance.argv:
        raise AcceptanceTooWeak("a behaviour-exec acceptance must name argv")
    if acceptance.kind is AcceptanceKind.HASH_EQUALITY:
        if acceptance.paired is None or not acceptance.paired.is_ref_independent:
            raise AcceptanceTooWeak(
                "a hash-equality acceptance cannot detect staleness it SHARES "
                "with the install; pair it with a ref-independent property check")
        if not acceptance.ref or acceptance.ref.strip().upper() == "HEAD":
            raise AcceptanceTooWeak(
                "compare against the SPINE and NAME THE REF — 'HEAD' is "
                "whatever the local checkout happens to be")
        validate_acceptance(acceptance.paired)
    return acceptance


def acceptance_command(acceptance: Acceptance) -> list[str]:
    """The argv a seat can paste, and the argv `run_acceptance` executes."""
    if acceptance.kind is AcceptanceKind.PROPERTY_GREP:
        return ["grep", "-c", "-F", "-e", acceptance.prop, "--",
                acceptance.consumed_path]
    if acceptance.kind is AcceptanceKind.BEHAVIOUR_EXEC:
        return list(acceptance.argv)
    raise AcceptanceTooWeak(
        f"{acceptance.kind.value} acceptances are not executable checks")


@dataclass(frozen=True)
class AcceptanceResult:
    passed: bool
    detail: str


def run_acceptance(acceptance: Acceptance) -> AcceptanceResult:
    """Execute the acceptance at the consumed path. Never raises: an
    unrunnable check is NOT PASSED, because the only safe reading of "I could
    not tell" is that the debt is still owed."""
    if not acceptance.is_behaviour_check:
        return AcceptanceResult(False, f"{acceptance.kind.value} is not a "
                                       "sufficient acceptance on its own")
    try:
        argv = acceptance_command(acceptance)
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError, AcceptanceTooWeak) as exc:
        return AcceptanceResult(False, f"acceptance could not run: {exc}")

    if acceptance.kind is AcceptanceKind.PROPERTY_GREP:
        # grep -c exits 1 on zero matches, so the count is the assertion and
        # the exit code is only corroboration.
        count = 0
        if cp.stdout.strip().isdigit():
            count = int(cp.stdout.strip())
        return AcceptanceResult(
            count > 0,
            f"grep -c -F {acceptance.prop!r} {acceptance.consumed_path} = {count}")

    output = (cp.stdout or "") + (cp.stderr or "")
    rc_ok = acceptance.expect_rc is None or cp.returncode == acceptance.expect_rc
    seen = acceptance.expect in output
    return AcceptanceResult(rc_ok and seen,
                            f"rc={cp.returncode} expect={acceptance.expect!r} "
                            f"present={seen}")


# --- deriving the property mechanically from the diff -----------------------

def added_lines(repo: str, from_sha: str, to_sha: str, path: str) -> list[str]:
    """The lines this landing ADDS to `path` — the raw material for a
    ref-independent acceptance, since "the content the fix adds" is exactly the
    property the install exists to deliver."""
    try:
        cp = subprocess.run(
            ["git", "-C", repo, "diff", "--unified=0", from_sha, to_sha, "--", path],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    if cp.returncode != 0:
        return []
    return [line[1:].strip() for line in cp.stdout.splitlines()
            if line.startswith("+") and not line.startswith("+++")]


def distinctive_property(lines) -> str:
    """Pick the added line most likely to be unique to this fix.

    Longest-wins among lines that clear MIN_PROPERTY_LEN and carry an
    alphanumeric character. Returns "" rather than a short line: an acceptance
    built on a common fragment is a green that could not have failed, which is
    the failure mode arriving through the verification step instead of the
    test suite.
    """
    stripped = (line.strip() for line in lines)
    candidates = [line for line in stripped
                  if len(line) >= MIN_PROPERTY_LEN and any(c.isalnum() for c in line)]
    if not candidates:
        return ""
    return max(candidates, key=len)


def derive_acceptance(repo: str, from_sha: str, to_sha: str,
                      site: ConsumedSite) -> Acceptance:
    """Build the acceptance for a site with no seat input at all (P1). Falls
    back to MISSING — which can never pass — rather than to a weaker check."""
    prop = distinctive_property(added_lines(repo, from_sha, to_sha, site.repo_path))
    if not prop:
        return Acceptance(AcceptanceKind.MISSING, site.consumed_path)
    return Acceptance(AcceptanceKind.PROPERTY_GREP, site.consumed_path, prop=prop)


# --------------------------------------------------------------------------
# P1/P4: obligations
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Obligation:
    """One install owed. `bead` is PROVENANCE ONLY — it says which landing
    incurred the debt and has no part in clearing it (P4)."""
    bead: str
    repo_path: str
    consumed_path: str
    installer: str
    authorization: str
    acceptance: Acceptance
    landed_sha: str = ""

    @property
    def key(self) -> str:
        return f"{self.bead}::{self.repo_path}::{self.consumed_path}"


def obligations_for_landing(repo: str, paths, bead: str, from_sha: str = "",
                            to_sha: str = "", roots: Roots | None = None,
                            roots_live: tuple[str, ...] | None = None,
                            acceptances: dict | None = None) -> list[Obligation]:
    """Every install owed by this landing, derived from the diff and the
    ownership table (P1).

    A HOT_SWAP path yields NO obligation — the pull is the activation, and
    stamping debt for it would fire the ledger on ordinary landings until it
    became noise a seat learned to skip (the SABLE-r5pfw erosion, which leaves
    the fleet worse off than no ledger at all). An INERT path yields none
    either. Only a consumed COPY is a debt.
    """
    roots = roots_from_env() if roots is None else roots
    live = live_roots(repo) if roots_live is None else roots_live
    acceptances = acceptances or {}
    owed: list[Obligation] = []
    for path in paths:
        for site in consumed_sites(repo, path, roots):
            if classify_site(site, live) is not ActivationClass.INSTALL_OWED:
                continue
            acceptance = acceptances.get(site.repo_path)
            if acceptance is None:
                acceptance = derive_acceptance(repo, from_sha, to_sha, site)
            owed.append(Obligation(bead=bead, repo_path=site.repo_path,
                                   consumed_path=site.consumed_path,
                                   installer=site.installer,
                                   authorization=site.authorization,
                                   acceptance=acceptance, landed_sha=to_sha))
    return owed


# --- persistence: the ledger outlives the bead ------------------------------

def ledger_path(repo: str = ".") -> Path:
    """Beside the merge-gate state, so every worktree of this repo sees one
    ledger and no other repo's."""
    return snapshot_lib.state_dir(repo) / LEDGER_FILE


def _acceptance_to_json(acceptance: Acceptance) -> dict:
    payload = {"kind": acceptance.kind.value,
               "consumed_path": acceptance.consumed_path,
               "prop": acceptance.prop, "argv": list(acceptance.argv),
               "expect": acceptance.expect, "expect_rc": acceptance.expect_rc,
               "ref": acceptance.ref}
    if acceptance.paired is not None:
        payload["paired"] = _acceptance_to_json(acceptance.paired)
    return payload


def _acceptance_from_json(payload: dict) -> Acceptance:
    paired = payload.get("paired")
    return Acceptance(
        kind=AcceptanceKind(payload["kind"]),
        consumed_path=payload.get("consumed_path", ""),
        prop=payload.get("prop", ""), argv=tuple(payload.get("argv") or ()),
        expect=payload.get("expect", ""), expect_rc=payload.get("expect_rc", 0),
        ref=payload.get("ref", ""),
        paired=_acceptance_from_json(paired) if paired else None)


def record_obligations(repo: str, obligations) -> Path:
    """Append to the ledger, keyed so a re-landing does not duplicate a row.
    Rewrites in full because the file is small and a partial append that lost
    the tail would silently shrink the debt."""
    path = ledger_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {o.key: o for o in load_obligations(repo)}
    for obligation in obligations:
        existing[obligation.key] = obligation
    lines = []
    for obligation in existing.values():
        lines.append(json.dumps({
            "bead": obligation.bead, "repo_path": obligation.repo_path,
            "consumed_path": obligation.consumed_path,
            "installer": obligation.installer,
            "authorization": obligation.authorization,
            "landed_sha": obligation.landed_sha,
            "acceptance": _acceptance_to_json(obligation.acceptance)}))
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return path


def load_obligations(repo: str = ".") -> list[Obligation]:
    """Every recorded obligation. A corrupt row is SKIPPED but never treated as
    an empty ledger — see `load_report` for the count that says so."""
    obligations, _ = load_report(repo)
    return obligations


def load_report(repo: str = ".") -> tuple[list[Obligation], int]:
    """(obligations, unreadable_row_count). The second value exists because a
    ledger that fails to parse and a ledger with no debt render identically,
    and only one of them is good news."""
    path = ledger_path(repo)
    out: list[Obligation] = []
    bad = 0
    try:
        text = path.read_text()
    except OSError:
        return out, bad
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            out.append(Obligation(
                bead=row["bead"], repo_path=row["repo_path"],
                consumed_path=row["consumed_path"], installer=row["installer"],
                authorization=row.get("authorization", ""),
                acceptance=_acceptance_from_json(row["acceptance"]),
                landed_sha=row.get("landed_sha", "")))
        except (ValueError, KeyError):
            bad += 1
    return out, bad


# --------------------------------------------------------------------------
# P3/P4: discharge — always by acceptance, never by an installer's exit code
# --------------------------------------------------------------------------

class ObligationState(str, Enum):
    OPEN = "open"
    DISCHARGED = "discharged"


NOT_IN_SCOPE = "not-in-scope"


@dataclass(frozen=True)
class ObligationStatus:
    obligation: Obligation
    state: ObligationState
    detail: str
    in_scope_of_run: bool = True


def discharge_scan(obligations, ran_installer: str | None = None
                   ) -> list[ObligationStatus]:
    """Judge every obligation by RUNNING its acceptance.

    `ran_installer` annotates which rows a given install run could even have
    touched; it NEVER decides state. That separation is the whole lesson of the
    live instance: a successful sable-orchestration-install run says nothing
    about hooks/tdd-evidence.sh, which install.sh owns — a ledger that marked
    rows discharged from an installer's exit code would have shown three green
    rows that night and one silently-still-red, with no way to see that the red
    one was never in the running.
    """
    out: list[ObligationStatus] = []
    for obligation in obligations:
        in_scope = ran_installer is None or ran_installer == obligation.installer
        result = run_acceptance(obligation.acceptance)
        detail = result.detail
        if not in_scope:
            detail = (f"{NOT_IN_SCOPE}: a {ran_installer} run cannot discharge a "
                      f"{obligation.installer} obligation — {detail}")
        out.append(ObligationStatus(
            obligation,
            ObligationState.DISCHARGED if result.passed else ObligationState.OPEN,
            detail, in_scope))
    return out


def open_obligations(obligations, bead_status: dict | None = None) -> list[Obligation]:
    """The debt that is still owed.

    `bead_status` is accepted AND DELIBERATELY UNUSED. It is here so a test can
    pass a map marking every bead closed and assert the output is byte-identical
    — the defect this module exists for is that all three founding instances had
    CLOSED beads while their fixes did nothing where they were consumed. An
    argument that is structurally ignored proves more than a comment saying it
    should be.
    """
    del bead_status
    return [s.obligation for s in discharge_scan(obligations)
            if s.state is ObligationState.OPEN]


# --------------------------------------------------------------------------
# The dispatch-time leg: can this branch land at all right now?
# --------------------------------------------------------------------------

class Landability(str, Enum):
    FREE = "free"                    # activates nowhere; lands under any regime
    INSTALL_OWED = "install-owed"    # lands now; an install is owed afterwards
    HOT_SWAP = "hot-swap"            # the merge IS the activation; held when
                                     # hot-swaps need an operator-present window
    UNRESOLVED = "unresolved"


_LANDABILITY_FOR = {
    ActivationClass.INERT: Landability.FREE,
    ActivationClass.INSTALL_OWED: Landability.INSTALL_OWED,
    ActivationClass.HOT_SWAP: Landability.HOT_SWAP,
    ActivationClass.UNRESOLVED: Landability.UNRESOLVED,
}


@dataclass(frozen=True)
class LandabilityReport:
    verdict: Landability
    paths: tuple[PathVerdict, ...] = field(default_factory=tuple)


def landability(repo: str, paths, roots: Roots | None = None,
                roots_live: tuple[str, ...] | None = None) -> LandabilityReport:
    """What activation class a candidate footprint would be, without
    hand-running readlink over every path.

    A footprint of pure test files and repo-only paths comes back FREE. That
    negative control is load-bearing in the same way the ledger's is: a
    predicate that answered HOT_SWAP for everything would stop all dispatch
    under a constrained regime, which is worse than the gap it replaces.
    """
    roots = roots_from_env() if roots is None else roots
    live = live_roots(repo) if roots_live is None else roots_live
    verdicts = tuple(classify_path(repo, p, roots, live) for p in paths)
    if not verdicts:
        return LandabilityReport(Landability.FREE, ())
    worst = max((v.activation for v in verdicts), key=lambda c: _CLASS_SEVERITY[c])
    return LandabilityReport(_LANDABILITY_FOR[worst], verdicts)


# --------------------------------------------------------------------------
# The cadence report
# --------------------------------------------------------------------------

def describe_bounds() -> str:
    """What this module can and cannot see. Printed with every report because
    an empty ledger and an unexamined one render identically otherwise."""
    lines = ["Ownership table (an unlisted path reports INERT — the optimistic "
             "direction, so audit this first when the ledger looks empty):"]
    for rule in OWNERSHIP:
        lines.append(f"  {rule.pattern:<38} -> {rule.installer}")
    lines.append(f"  {'skills/*/*':<38} -> {ORCHESTRATION_INSTALL}")
    lines.append(f"  {'bin/*.py (hook sibling libs)':<38} -> {ORCHESTRATION_INSTALL}")
    lines.append("A claimed path whose install DIRECTORY does not exist reports "
                 "INERT (the layer was never installed at this scope) — so a "
                 "brand-new artifact in a brand-new directory is the one owed "
                 "install this table misses.")
    return "\n".join(lines)


def format_ledger_report(statuses, unreadable: int = 0) -> str:
    """The block a seat reads every cadence.

    Every row names its INSTALLER and that installer's AUTHORIZATION, because
    "an install is owed" is not actionable and is actively misleading when
    several installers with different blast radii exist — it is how a
    correctly-unmet acceptance gets misread as an install failure.
    """
    lines = ["ACTIVATION DEBT (SABLE-3zjc1) — landed but not yet running"]
    open_rows = [s for s in statuses if s.state is ObligationState.OPEN]
    done_rows = [s for s in statuses if s.state is ObligationState.DISCHARGED]
    if unreadable:
        lines.append(f"  !! {unreadable} unreadable ledger row(s) — the debt "
                     "below is a LOWER BOUND")
    if not statuses:
        lines.append("  no obligations recorded")
        lines.append(describe_bounds())
        return "\n".join(lines)

    lines.append(f"  OPEN: {len(open_rows)}   DISCHARGED: {len(done_rows)}")
    for status in open_rows:
        obligation = status.obligation
        lines.append(f"  OPEN  {obligation.repo_path}")
        lines.append(f"        consumed at : {obligation.consumed_path}")
        lines.append(f"        discharged by: {obligation.installer}")
        lines.append(f"        authorization: {obligation.authorization}")
        try:
            argv = " ".join(acceptance_command(obligation.acceptance))
        except AcceptanceTooWeak as exc:
            argv = f"(no runnable acceptance: {exc})"
        lines.append(f"        acceptance   : {argv}")
        lines.append(f"        measured     : {status.detail}")
        lines.append(f"        incurred by  : {obligation.bead} "
                     f"(CLOSED OR NOT — the bead does not clear this)")
    for status in done_rows:
        lines.append(f"  DONE  {status.obligation.repo_path} "
                     f"[{status.obligation.installer}] — {status.detail}")
    lines.append(describe_bounds())
    return "\n".join(lines)


def format_landability(report: LandabilityReport) -> str:
    lines = [f"LANDABILITY: {report.verdict.value}"]
    for verdict in report.paths:
        installers = ", ".join(verdict.installers) or "-"
        lines.append(f"  {verdict.activation.value:<14} {verdict.repo_path} "
                     f"[{installers}]")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="sable_activation_debt_lib",
        description="Install/activation obligation ledger (SABLE-3zjc1): what "
                    "we landed today that is not actually running, which tool "
                    "discharges it, and what authorization that tool needs.")
    parser.add_argument("--repo", default=os.getcwd())
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("report", help="open activation debt (the cadence report)")

    stamp = sub.add_parser("stamp", help="record obligations for a landing")
    stamp.add_argument("--bead", required=True)
    stamp.add_argument("--from-sha", required=True)
    stamp.add_argument("--to-sha", required=True)
    stamp.add_argument("paths", nargs="+")

    land = sub.add_parser("landability",
                          help="what activation class would this footprint be?")
    land.add_argument("paths", nargs="+")

    args = parser.parse_args(argv)
    repo = os.path.abspath(args.repo)

    if args.cmd == "report":
        obligations, bad = load_report(repo)
        print(format_ledger_report(discharge_scan(obligations), bad))
        return 0

    if args.cmd == "stamp":
        owed = obligations_for_landing(repo, args.paths, args.bead,
                                       args.from_sha, args.to_sha)
        record_obligations(repo, owed)
        print(format_ledger_report(discharge_scan(owed)))
        return 0

    report = landability(repo, args.paths)
    print(format_landability(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
