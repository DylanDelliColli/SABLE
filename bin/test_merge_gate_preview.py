#!/usr/bin/env python3
"""Unit tests for the push-time preview kick (SABLE-jd5fj.1).

The kick is the half of the gate that runs in a hook's shadow: build the
merge-preview, push its ci-verify ref (the CI trigger), and RETURN — no polling,
no verdict, no bead writes. These tests pin the two properties that make it safe
to fire from post-push-merge-notify.sh:

  * it returns after the ref push WITHOUT waiting for CI (wait_for_ci and the
    raw subprocess seam are booby-trapped here — touching either fails the test)
  * it kicks EXACTLY ONCE per (base, branch) merge state, because the ref name is
    the shared idempotency key; a second kick for the same state pushes nothing

plus the promote-side counterpart — adopt_kicked_preview, which lets promote wait
on the kicked run instead of starting a second one, and which must fall through
to the ordinary build on every kind of absence or drift.

Real git composition (a temp repo + stub gh, and the promote exit-code taxonomy
regression) lives in hooks/test/test-preview-kick.sh.
"""
import importlib.util
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_merge_gate", str(Path(__file__).resolve().parent / "sable-merge-gate")
)
_SPEC = importlib.util.spec_from_loader("sable_merge_gate", _LOADER)
smg = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(smg)

# SABLE-jd5fj.3 split: the gate now lives in three modules beside the CLI, and
# each seam is patched on the module that DEFINES it (every caller invokes them
# module-qualified, so one patch reaches the whole gate). These aliases are the
# only plumbing change the split required here — no assertion below moved.
classify = smg.classify
git_lib = smg.git_lib
preview_lib = smg.preview_lib
promote_lib = smg.promote_lib

REPO = "/repo"
REMOTE = "origin"
BASE = "trunk"
BRANCH = "wk-x"
BASE_SHA = "a" * 40
BRANCH_SHA = "b" * 40
PREVIEW_SHA = "c" * 40
TREE_SHA = "d" * 40


def _cp(returncode=0, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


class FakeGit:
    """Stand-in for smg._git covering every subcommand the kick/adopt paths use.
    Records pushes so 'exactly once' is assertable, and models the remote's ref
    namespace so a push made by one call is visible to the next."""

    def __init__(self, *, remote_refs=None, conflict=False, parents=None,
                 base_sha=BASE_SHA, branch_sha=BRANCH_SHA):
        self.calls = []
        self.pushes = []
        self.previews_built = 0
        self.remote_refs = dict(remote_refs or {})
        self.conflict = conflict
        self.parents = dict(parents or {})
        self.base_sha = base_sha
        self.branch_sha = branch_sha

    def __call__(self, repo, *args, check=True):
        self.calls.append(args)
        cmd = args[0]
        if cmd == "fetch":
            if len(args) == 3 and args[2].startswith("refs/heads/"):
                ref = args[2][len("refs/heads/"):]
                return _cp(0 if ref in self.remote_refs else 1)
            return _cp(0)
        if cmd == "rev-parse":
            ref = args[-1]
            if ref.endswith(f"{BASE}^{{commit}}"):
                return _cp(0, self.base_sha)
            if ref.endswith(f"{BRANCH}^{{commit}}"):
                return _cp(0, self.branch_sha)
            return _cp(1, "unknown ref")
        if cmd == "ls-remote":
            ref = args[-1][len("refs/heads/"):]
            sha = self.remote_refs.get(ref)
            return _cp(0, f"{sha}\trefs/heads/{ref}\n") if sha else _cp(2)
        if cmd == "rev-list":
            sha = args[-1]
            if sha not in self.parents:
                return _cp(1, "bad object")
            return _cp(0, " ".join([sha, *self.parents[sha]]))
        if cmd == "merge-tree":
            if self.conflict:
                return _cp(1, "CONFLICT (content): shared.txt")
            return _cp(0, f"{TREE_SHA}\n")
        if cmd == "commit-tree":
            self.previews_built += 1
            return _cp(0, PREVIEW_SHA)
        if cmd == "push":
            spec = args[-1]
            self.pushes.append(spec)
            if ":" in spec and spec.startswith(("--delete",)) is False:
                sha, dest = spec.split(":", 1)
                if dest.startswith("refs/heads/"):
                    self.remote_refs[dest[len("refs/heads/"):]] = sha
            return _cp(0)
        return _cp(0)


@pytest.fixture
def no_waiting(monkeypatch):
    """Booby-trap every path that could block on CI. The kick's whole contract is
    that it returns at the ref push, so any poll/subprocess escape is a failure,
    not a slow test."""
    def _boom(*a, **kw):
        raise AssertionError("preview kick must not wait for CI / shell out")
    monkeypatch.setattr(preview_lib, "wait_for_ci", _boom)
    monkeypatch.setattr(git_lib, "_run", _boom)


# --- shared idempotency key --------------------------------------------------

def test_kick_key_is_a_pure_function_of_the_two_parents():
    assert smg.preview_kick_key(BASE_SHA, BRANCH_SHA) == smg.preview_kick_key(BASE_SHA, BRANCH_SHA)
    assert smg.preview_kick_key(BASE_SHA, BRANCH_SHA) != smg.preview_kick_key(BRANCH_SHA, BASE_SHA)
    assert smg.preview_kick_key(BASE_SHA, "e" * 40) != smg.preview_kick_key(BASE_SHA, BRANCH_SHA)


def test_kick_key_rejects_missing_parent():
    with pytest.raises(ValueError):
        smg.preview_kick_key("", BRANCH_SHA)


def test_kick_ref_is_ci_verify_prefixed_and_branch_keyed():
    ref = smg.preview_kick_ref(BRANCH, BASE_SHA, BRANCH_SHA)
    assert ref.startswith(f"ci-verify/{BRANCH}-")
    # Sweep lists ci-verify/* flat, so the key must never nest another level.
    assert ref.count("/") == 1


def test_kick_ref_sanitizes_a_slashed_branch_name():
    ref = smg.preview_kick_ref("feat/thing", BASE_SHA, BRANCH_SHA)
    assert ref.count("/") == 1


# --- kick_preview ------------------------------------------------------------

def test_kick_pushes_the_ref_and_returns_without_waiting(monkeypatch, no_waiting):
    fake = FakeGit()
    monkeypatch.setattr(git_lib, "_git", fake)
    assert smg.kick_preview(BRANCH, BASE, REPO, REMOTE) == 0
    ref = smg.preview_kick_ref(BRANCH, BASE_SHA, BRANCH_SHA)
    assert fake.pushes == [f"{PREVIEW_SHA}:refs/heads/{ref}"]


def test_kick_is_exactly_once_per_merge_state(monkeypatch, no_waiting):
    """The second kick for the same (base, branch) sees its own ref and pushes
    nothing — re-pushing would re-trigger CI and cancel the run already underway
    (ci-verify.yml is cancel-in-progress)."""
    fake = FakeGit()
    monkeypatch.setattr(git_lib, "_git", fake)
    smg.kick_preview(BRANCH, BASE, REPO, REMOTE)
    assert smg.kick_preview(BRANCH, BASE, REPO, REMOTE) == 0
    assert len(fake.pushes) == 1
    assert fake.previews_built == 1


def test_kick_after_the_branch_moves_kicks_again(monkeypatch, no_waiting):
    fake = FakeGit()
    monkeypatch.setattr(git_lib, "_git", fake)
    smg.kick_preview(BRANCH, BASE, REPO, REMOTE)
    fake.branch_sha = "e" * 40  # worker pushed a follow-up commit
    smg.kick_preview(BRANCH, BASE, REPO, REMOTE)
    assert len(fake.pushes) == 2
    assert fake.pushes[0] != fake.pushes[1]


def test_kick_on_conflict_exits_22_and_pushes_nothing(monkeypatch, no_waiting):
    fake = FakeGit(conflict=True)
    monkeypatch.setattr(git_lib, "_git", fake)
    with pytest.raises(smg.GateError) as exc:
        smg.kick_preview(BRANCH, BASE, REPO, REMOTE)
    assert exc.value.code == 22
    assert fake.pushes == []


def test_kick_writes_no_bead_evidence_and_notifies_nobody(monkeypatch, no_waiting):
    def _boom(*a, **kw):
        raise AssertionError("a kick has no verdict to report")
    monkeypatch.setattr(git_lib, "_git", FakeGit())
    monkeypatch.setattr(promote_lib, "_append_evidence", _boom)
    monkeypatch.setattr(promote_lib, "_notify", _boom)
    assert smg.kick_preview(BRANCH, BASE, REPO, REMOTE) == 0


def test_main_preview_subcommand_routes_to_the_kick(monkeypatch, no_waiting):
    fake = FakeGit()
    monkeypatch.setattr(git_lib, "_git", fake)
    monkeypatch.setenv("SABLE_MG_BASE", BASE)
    rc = smg.main(["preview", "--branch", BRANCH, "--repo", REPO, "--remote", REMOTE])
    assert rc == 0
    assert len(fake.pushes) == 1


# --- adopt_kicked_preview ----------------------------------------------------

def _kicked_fake(parents=(BASE_SHA, BRANCH_SHA)):
    ref = smg.preview_kick_ref(BRANCH, BASE_SHA, BRANCH_SHA)
    return FakeGit(remote_refs={ref: PREVIEW_SHA}, parents={PREVIEW_SHA: list(parents)}), ref


def test_adopt_returns_the_kicked_preview_when_parents_match(monkeypatch):
    fake, ref = _kicked_fake()
    monkeypatch.setattr(git_lib, "_git", fake)
    assert smg.adopt_kicked_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) == (PREVIEW_SHA, ref)


def test_adopt_declines_when_no_kick_happened(monkeypatch):
    monkeypatch.setattr(git_lib, "_git", FakeGit())
    assert smg.adopt_kicked_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) is None


def test_adopt_declines_when_the_kicked_commit_has_drifted_parents(monkeypatch):
    fake, _ = _kicked_fake(parents=("f" * 40, BRANCH_SHA))
    monkeypatch.setattr(git_lib, "_git", fake)
    assert smg.adopt_kicked_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) is None


def test_adopt_declines_when_the_object_is_unfetchable(monkeypatch):
    fake, ref = _kicked_fake()

    def _no_fetch(repo, *args, check=True):
        if args[0] == "fetch" and len(args) == 3:
            return _cp(1, "couldn't find remote ref")
        return fake(repo, *args, check=check)

    monkeypatch.setattr(git_lib, "_git", _no_fetch)
    assert smg.adopt_kicked_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) is None


def test_adopt_never_raises_into_the_promote_flow(monkeypatch):
    def _explode(*a, **kw):
        raise RuntimeError("git blew up")
    monkeypatch.setattr(git_lib, "_git", _explode)
    assert smg.adopt_kicked_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) is None


# --- promote adopts instead of building a second preview ---------------------

@pytest.fixture
def quiet_promote(monkeypatch):
    # No verdict is stored yet, so these cases exercise promote's wait_for_ci
    # fall-through — the pre-split path, unchanged by the SABLE-jd5fj.3 split.
    # Stated explicitly so the case says which verdict source it is testing.
    monkeypatch.setattr(preview_lib, "read_verdict",
                        lambda repo, ref, sha: classify.Verdict(
                            "pending", "", sha, ref, source="precomputed", complete=False))
    monkeypatch.setattr(promote_lib, "_append_evidence", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "_notify", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "cleanup_after_merge", lambda *a, **kw: None)


def test_promote_adopts_the_kicked_preview_and_never_builds_a_second(monkeypatch, quiet_promote):
    fake, ref = _kicked_fake()
    # After the green promote, base resolves to the promoted preview SHA.
    fake.base_sha = BASE_SHA

    def _resolve(repo, r):
        if r.endswith(BASE) and fake.pushes and any(
                p.endswith(f"refs/heads/{BASE}") for p in fake.pushes):
            return PREVIEW_SHA
        return BASE_SHA if r.endswith(BASE) else BRANCH_SHA

    monkeypatch.setattr(git_lib, "_git", fake)
    monkeypatch.setattr(git_lib, "resolve_commit", _resolve)
    monkeypatch.setattr(preview_lib, "build_preview",
                        lambda *a, **kw: pytest.fail("promote rebuilt an already-kicked preview"))
    monkeypatch.setattr(preview_lib, "wait_for_ci", lambda *a, **kw: ("success", "http://run/1"))

    assert smg.promote("SABLE-x", BRANCH, BASE, REPO, REMOTE, "optimus", None) == 0
    assert f"{PREVIEW_SHA}:refs/heads/{BASE}" in fake.pushes
    assert f"--delete" in [p for p in fake.calls if p[0] == "push"][-1]
    assert ref in [p[-1] for p in fake.calls if p[0] == "push"]


def test_promote_still_builds_its_own_preview_when_nothing_was_kicked(monkeypatch, quiet_promote):
    fake = FakeGit()
    monkeypatch.setattr(git_lib, "_git", fake)
    monkeypatch.setattr(git_lib, "resolve_commit",
                        lambda repo, r: BASE_SHA if r.endswith(BASE) else BRANCH_SHA)
    monkeypatch.setattr(preview_lib, "wait_for_ci", lambda *a, **kw: ("failure", "http://run/2"))
    assert smg.promote("SABLE-x", BRANCH, BASE, REPO, REMOTE, "optimus", None) == 20
    assert fake.previews_built == 1


def test_promote_taxonomy_is_untouched_by_an_adopted_preview(monkeypatch, quiet_promote):
    """RED stays 20 whether the preview was kicked or built — adoption changes
    which object is verified, never what a verdict means."""
    fake, _ = _kicked_fake()
    monkeypatch.setattr(git_lib, "_git", fake)
    monkeypatch.setattr(git_lib, "resolve_commit",
                        lambda repo, r: BASE_SHA if r.endswith(BASE) else BRANCH_SHA)
    monkeypatch.setattr(preview_lib, "wait_for_ci", lambda *a, **kw: ("failure", "http://run/3"))
    assert smg.promote("SABLE-x", BRANCH, BASE, REPO, REMOTE, "optimus", None) == 20
    assert fake.previews_built == 0


# --- find_stale_green_preview: the ADOPTION MISS the serial queue creates -----
#
# SABLE-kzi1a. adopt_kicked_preview answers "is there a preview for THIS exact
# (base, branch) pair?" — and under a serial merge lane the answer is no for
# every branch after the first, because each merge moves the base and invalidates
# the push-time preview of everything still queued. The green run those previews
# already paid for is then discarded and a fresh one started. This is the
# discovery half of stopping that: find the completed-GREEN preview of THIS
# branch tip onto an OLDER base, so promote can take it to the same footprint
# assessment a mid-gate base move takes.
#
# It reads only. It decides nothing — whether that preview may be promoted is
# entirely sable_gate_promote_lib's question, and the answer still runs the
# impact tier on the real combined tree.

OLD_BASE_SHA = "9" * 40
OLDER_BASE_SHA = "8" * 40
STALE_PREVIEW_SHA = "5" * 40
OLDER_PREVIEW_SHA = "6" * 40


class StaleRemote:
    """A remote holding kicked ci-verify refs for BRANCH, as a serial merge queue
    leaves them: previews built against bases that have since moved on. Models
    exactly what the discovery reads — a ref listing, the object fetch, commit
    parents, and base ancestry — and nothing else."""

    def __init__(self, refs, *, ancestors=(), fetchable=True):
        self.refs = dict(refs)              # ref -> (preview_sha, [base_parent, branch_parent])
        self.ancestors = set(ancestors)     # (older, newer) pairs that ARE ancestral
        self.fetchable = fetchable
        self.calls = []

    def __call__(self, repo, *args, check=True):
        self.calls.append(args)
        cmd = args[0]
        if cmd == "ls-remote":
            pattern = args[-1][len("refs/heads/"):]
            if not pattern.endswith("*"):           # the exact-ref adoption probe
                hit = self.refs.get(pattern)
                return _cp(0, f"{hit[0]}\trefs/heads/{pattern}\n") if hit else _cp(2)
            prefix = pattern[:-1]
            lines = [f"{sha}\trefs/heads/{ref}"
                     for ref, (sha, _p) in sorted(self.refs.items()) if ref.startswith(prefix)]
            return _cp(0, "".join(f"{ln}\n" for ln in lines))
        if cmd == "fetch":
            return _cp(0 if self.fetchable else 1)
        if cmd == "rev-list":
            sha = args[-1]
            for _ref, (preview, parents) in self.refs.items():
                if preview == sha:
                    return _cp(0, " ".join([sha, *parents]))
            return _cp(1, "bad object")
        if cmd == "merge-base":
            older, newer = args[-2], args[-1]
            return _cp(0 if (older, newer) in self.ancestors else 1)
        return _cp(0)


def _queued(*, base=OLD_BASE_SHA, branch=BRANCH_SHA, preview=STALE_PREVIEW_SHA, extra=None):
    """A remote whose only kicked preview for BRANCH is one built on `base`,
    plus whatever `extra` refs the case needs."""
    refs = {smg.preview_kick_ref(BRANCH, base, branch): (preview, [base, branch])}
    refs.update(extra or {})
    return StaleRemote(refs, ancestors={(OLD_BASE_SHA, BASE_SHA), (OLDER_BASE_SHA, BASE_SHA),
                                        (OLDER_BASE_SHA, OLD_BASE_SHA)})


def _verdict(monkeypatch, conclusion, *, complete=True, seen=None):
    def _read(repo, ref, sha):
        if seen is not None:
            seen.append((ref, sha))
        return classify.Verdict(conclusion, "http://run/9", sha, ref,
                                source="precomputed", complete=complete)
    monkeypatch.setattr(preview_lib, "read_verdict", _read)


def test_find_stale_green_preview_finds_the_queued_branchs_green_preview(monkeypatch):
    monkeypatch.setattr(git_lib, "_git", _queued())
    _verdict(monkeypatch, "success")
    found = preview_lib.find_stale_green_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA)
    assert found is not None
    assert (found.preview_sha, found.base_sha) == (STALE_PREVIEW_SHA, OLD_BASE_SHA)
    assert found.ref == smg.preview_kick_ref(BRANCH, OLD_BASE_SHA, BRANCH_SHA)


def test_find_stale_green_preview_declines_when_adoption_will_hit(monkeypatch):
    """A preview for the CURRENT base exists, so the ordinary adoption path owns
    this promote and there is nothing stale to reason about."""
    current = smg.preview_kick_ref(BRANCH, BASE_SHA, BRANCH_SHA)
    fake = _queued(extra={current: (PREVIEW_SHA, [BASE_SHA, BRANCH_SHA])})
    monkeypatch.setattr(git_lib, "_git", fake)
    seen = []
    _verdict(monkeypatch, "success", seen=seen)
    assert preview_lib.find_stale_green_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) is None
    assert seen == [], "no verdict should be read when adoption already has an answer"


@pytest.mark.parametrize("conclusion,complete", [
    ("failure", True), ("cancelled", True), ("pending", False), ("timeout", True),
])
def test_find_stale_green_preview_declines_anything_but_a_stored_green(monkeypatch, conclusion, complete):
    """Only a COMPLETED green preview is worth anything here. A red one says
    nothing about the new base, and a pending one is a wait — and paying a wait
    on a stale object is the opposite of the point."""
    monkeypatch.setattr(git_lib, "_git", _queued())
    _verdict(monkeypatch, conclusion, complete=complete)
    assert preview_lib.find_stale_green_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) is None


def test_find_stale_green_preview_declines_a_preview_of_a_stale_branch_tip(monkeypatch):
    """The branch was re-pushed after the kick. That preview verified an object
    that no longer exists on the branch — it is not evidence about this promote."""
    monkeypatch.setattr(git_lib, "_git", _queued(branch="f" * 40))
    _verdict(monkeypatch, "success")
    assert preview_lib.find_stale_green_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) is None


def test_find_stale_green_preview_requires_the_base_to_have_moved_FORWARD(monkeypatch):
    """A preview base that is not an ancestor of the current base means the
    integration branch was reset or force-pushed, not advanced by a merge. That
    is not the queued-branch case; it takes the pre-kzi1a full re-preview."""
    fake = _queued()
    fake.ancestors = set()
    monkeypatch.setattr(git_lib, "_git", fake)
    _verdict(monkeypatch, "success")
    assert preview_lib.find_stale_green_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) is None


def test_find_stale_green_preview_declines_when_the_object_is_unfetchable(monkeypatch):
    fake = _queued()
    fake.fetchable = False
    monkeypatch.setattr(git_lib, "_git", fake)
    _verdict(monkeypatch, "success")
    assert preview_lib.find_stale_green_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) is None


def test_find_stale_green_preview_prefers_the_preview_closest_to_the_current_base(monkeypatch):
    """Two queued previews for the same branch tip: the one built on the LATER
    base wins, because its base-move footprint is the smaller of the two and a
    narrower footprint is the one more likely to be provably disjoint."""
    older = smg.preview_kick_ref(BRANCH, OLDER_BASE_SHA, BRANCH_SHA)
    fake = _queued(extra={older: (OLDER_PREVIEW_SHA, [OLDER_BASE_SHA, BRANCH_SHA])})
    monkeypatch.setattr(git_lib, "_git", fake)
    _verdict(monkeypatch, "success")
    found = preview_lib.find_stale_green_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA)
    assert found is not None and found.base_sha == OLD_BASE_SHA


def test_find_stale_green_preview_never_raises_into_the_promote_flow(monkeypatch):
    """Same contract as adopt_kicked_preview: this is a strict optimization, so
    every failure mode must be a None, never an exception that changes an exit
    code the gate's callers branch on."""
    def _explode(*a, **kw):
        raise RuntimeError("git blew up")
    monkeypatch.setattr(git_lib, "_git", _explode)
    assert preview_lib.find_stale_green_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) is None


def test_find_stale_green_preview_reads_only(monkeypatch):
    """The discovery half must not push, delete, or build anything — promote
    decides, and the module boundary says this side never writes."""
    fake = _queued()
    monkeypatch.setattr(git_lib, "_git", fake)
    _verdict(monkeypatch, "success")
    preview_lib.find_stale_green_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA)
    assert [c for c in fake.calls if c[0] in ("push", "commit-tree", "merge-tree")] == []
