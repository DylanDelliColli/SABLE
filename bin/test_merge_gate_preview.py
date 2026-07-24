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
import json
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

import sable_batch_fold_lib as fold_lib
import sable_batch_key_lib as batch_key

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
                 base_sha=BASE_SHA, branch_sha=BRANCH_SHA,
                 remote_url="/fixtures/origin.git"):
        self.calls = []
        self.pushes = []
        self.push_remotes = []
        self.previews_built = 0
        self.remote_refs = dict(remote_refs or {})
        self.conflict = conflict
        self.parents = dict(parents or {})
        self.base_sha = base_sha
        self.branch_sha = branch_sha
        # What `git remote get-url <name>` resolves to (SABLE-ck05): pushes must
        # address this path, not the CWD-sensitive remote NAME.
        self.remote_url = remote_url

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
        if cmd == "remote" and len(args) >= 2 and args[1] == "get-url":
            # Resolve a remote NAME to its configured path; a non-name (already a
            # path) or unknown remote errors, matching real `git remote get-url`.
            if args[-1] == REMOTE:
                return _cp(0, self.remote_url)
            return _cp(2, "error: No such remote")
        if cmd == "push":
            self.push_remotes.append(args[1])
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


# --- SABLE-ck05: pushes address the resolved URL, never the remote NAME -------

def test_kick_preview_pushes_resolved_url(monkeypatch, no_waiting):
    """The kick's ref push must name the resolved remote URL/path, not the literal
    'origin' — a CWD-sensitive name can follow a working-dir escape to the wrong
    upstream (the ck05 bare-origin escape), a resolved path cannot. The fetch that
    precedes it stays name-based (read-only; the ck05 harness intercepts push
    alone)."""
    fake = FakeGit(remote_url="/fixtures/wk-x-origin.git")
    monkeypatch.setattr(git_lib, "_git", fake)
    smg.kick_preview(BRANCH, BASE, REPO, REMOTE)
    assert fake.push_remotes == ["/fixtures/wk-x-origin.git"]
    assert REMOTE not in fake.push_remotes  # never the CWD-sensitive name
    # Negative control: fetch may — and must — still use the remote NAME.
    fetches = [c for c in fake.calls if c[0] == "fetch"]
    assert fetches, "kick must still fetch base/branch before building the preview"
    assert any(REMOTE in c for c in fetches)


def test_materialize_preview_pushes_resolved_url(monkeypatch):
    """The fresh-build push in materialize_preview (the pre-kick path promote
    takes when nothing was kicked) must likewise address the resolved path, not
    the name."""
    fake = FakeGit(remote_url="/fixtures/wk-x-origin.git")
    monkeypatch.setattr(git_lib, "_git", fake)
    preview_sha, ref, adopted = preview_lib.materialize_preview(
        REPO, REMOTE, BRANCH, BASE, BASE_SHA, BRANCH_SHA, "SABLE-x")
    assert adopted is False  # nothing kicked, so it built + pushed a fresh ref
    assert fake.push_remotes == ["/fixtures/wk-x-origin.git"]
    assert REMOTE not in fake.push_remotes


# --- adopt_kicked_preview ----------------------------------------------------

def _kicked_fake(parents=(BASE_SHA, BRANCH_SHA)):
    ref = smg.preview_kick_ref(BRANCH, BASE_SHA, BRANCH_SHA)
    return FakeGit(remote_refs={ref: PREVIEW_SHA}, parents={PREVIEW_SHA: list(parents)}), ref


def test_adopt_returns_the_kicked_preview_when_parents_match(monkeypatch):
    fake, ref = _kicked_fake()
    monkeypatch.setattr(git_lib, "_git", fake)
    assert smg.adopt_kicked_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) == (PREVIEW_SHA, ref)


# --- SABLE-be4lo.1 regression: keying-module consolidation changes nothing ---

def test_build_preview_commit_tree_parents_are_base_then_branch(monkeypatch):
    """The two-parent build (preview_lib.py:57) now orders its commit-tree
    parents via batch_key.pair_parents instead of the literal base_sha,
    branch_sha pair — the call must still be byte-identical: base first,
    branch second."""
    fake = FakeGit()
    monkeypatch.setattr(git_lib, "_git", fake)
    preview_lib.build_preview(REPO, BASE_SHA, BRANCH_SHA, "msg")
    ct_calls = [c for c in fake.calls if c[0] == "commit-tree"]
    assert len(ct_calls) == 1
    assert ct_calls[0][2:6] == ("-p", BASE_SHA, "-p", BRANCH_SHA)


def test_adoption_identity_check_stays_byte_identical_after_consolidation(monkeypatch):
    """The adoption identity check (preview_lib.py:83) now compares against
    batch_key.pair_parents(base_sha, branch_sha) instead of the literal
    [base_sha, branch_sha] — same comparison, same result both ways."""
    fake, ref = _kicked_fake(parents=(BASE_SHA, BRANCH_SHA))
    monkeypatch.setattr(git_lib, "_git", fake)
    assert smg.adopt_kicked_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) == (PREVIEW_SHA, ref)
    # Negative control: a swapped-order pair must still fail the identity check.
    fake2, _ = _kicked_fake(parents=(BRANCH_SHA, BASE_SHA))
    monkeypatch.setattr(git_lib, "_git", fake2)
    assert smg.adopt_kicked_preview(REPO, REMOTE, BRANCH, BASE_SHA, BRANCH_SHA) is None


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


# --- report_verdict (Chuck's non-blocking read-verdict CLI leg) --------------
#
# SABLE-fewih: unlike wait_for_ci, this leg has no timeout to converge through,
# so a gh non-answer must print something OTHER than 'pending' — otherwise a
# broken/missing gh is indistinguishable from a genuinely in-flight run.

def test_report_verdict_prints_pending_for_a_genuinely_in_flight_run(monkeypatch, capsys):
    fake, _ = _kicked_fake()
    monkeypatch.setattr(git_lib, "_git", fake)
    monkeypatch.setattr(preview_lib, "read_verdict",
                        lambda repo, ref, sha: classify.Verdict(
                            "pending", "", sha, ref, source="precomputed",
                            complete=False, answered=True))
    rc = preview_lib.report_verdict(BRANCH, BASE, REPO, REMOTE, as_json=True)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["state"] == "pending"
    assert out["promotable"] is False


def test_report_verdict_prints_unknown_when_gh_gave_no_answer(monkeypatch, capsys):
    """gh missing/erroring/hanging -> read_verdict's answered=False. This must
    read as 'unknown', NOT 'pending' — the bug this bead exists to fix."""
    fake, _ = _kicked_fake()
    monkeypatch.setattr(git_lib, "_git", fake)
    monkeypatch.setattr(preview_lib, "read_verdict",
                        lambda repo, ref, sha: classify.Verdict(
                            "pending", "", sha, ref, source="precomputed",
                            complete=False, answered=False))
    rc = preview_lib.report_verdict(BRANCH, BASE, REPO, REMOTE, as_json=True)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["state"] == "unknown"
    assert out["state"] != "pending"
    assert out["promotable"] is False


def test_report_verdict_prints_none_when_nothing_was_kicked(monkeypatch, capsys):
    """No ref at all (never kicked) stays its own distinct state — not
    conflated with either pending or unknown."""
    monkeypatch.setattr(git_lib, "_git", FakeGit())

    def _boom(*a, **kw):
        raise AssertionError("nothing was kicked; read_verdict must not be called")
    monkeypatch.setattr(preview_lib, "read_verdict", _boom)
    rc = preview_lib.report_verdict(BRANCH, BASE, REPO, REMOTE, as_json=True)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["state"] == "none"
    assert out["promotable"] is False


# --------------------------------------------------------------------------
# Local-runner verdict journal — the SECOND verdict source (SABLE-21rug.2)
#
# read_verdict now consults two independent sources behind the SAME
# interface: this journal (a plain file, checked first) and the Actions leg
# above (unchanged). Every case below points SABLE_MG_RUNNER_VERDICT_LOG at a
# throwaway tmp_path file, so none of it touches the real per-repo state dir
# _runner_verdict_log_path falls back to when the env var is unset.
# --------------------------------------------------------------------------

RUNNER_REF = "ci-verify/wk-x-ccccccc"


def _write_journal_line(path, **fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(fields) + "\n")


def test_read_verdict_actions_leg_stays_byte_identical_with_no_journal_entry(monkeypatch, tmp_path):
    """REGRESSION (priority 1): with no journal file at all (the state every
    existing fixture is in — none of them ever write to it), read_verdict must
    still answer purely from _gh_runs, with the exact fields the pre-seam code
    produced. This is what "the GitHub-Actions verdict path is byte-identical"
    means made concrete: same conclusion, same source, same completeness, same
    outcome."""
    monkeypatch.setenv("SABLE_MG_RUNNER_VERDICT_LOG", str(tmp_path / "absent-runner-verdicts.jsonl"))
    monkeypatch.setattr(preview_lib, "_gh_runs", lambda *a, **kw: [
        {"databaseId": 9, "headSha": PREVIEW_SHA, "status": "completed",
         "conclusion": "success", "url": "http://run/9"}])
    v = preview_lib.read_verdict(REPO, RUNNER_REF, PREVIEW_SHA)
    assert v.conclusion == "success"
    assert v.source == "precomputed"
    assert v.complete is True
    assert v.outcome == classify.GREEN
    assert v.run_url == "http://run/9"


def test_read_verdict_consumes_a_matching_sha_journal_verdict(monkeypatch, tmp_path):
    """A journal-backed verdict with the SAME sha as the one under decision is
    consumed, carries the local-runner producer, and never touches Actions."""
    log = tmp_path / "runner-verdicts.jsonl"
    monkeypatch.setenv("SABLE_MG_RUNNER_VERDICT_LOG", str(log))
    _write_journal_line(log, ref=RUNNER_REF, preview_sha=PREVIEW_SHA, conclusion="success",
                        run_url="http://hand-run/1", producer="local-runner")

    def _boom(*a, **kw):
        raise AssertionError("a consumed journal verdict must not fall through to Actions")
    monkeypatch.setattr(preview_lib, "_gh_runs", _boom)

    v = preview_lib.read_verdict(REPO, RUNNER_REF, PREVIEW_SHA)
    assert v.complete is True
    assert v.conclusion == "success"
    assert v.source == classify.VerdictSource.LOCAL_RUNNER
    assert v.source == "local-runner"
    assert v.outcome == classify.GREEN
    assert v.run_url == "http://hand-run/1"


def test_read_verdict_refuses_a_journal_verdict_for_the_wrong_sha(monkeypatch, tmp_path):
    """Refuse-on-SHA-mismatch, both polarities in one test: the SAME journal
    entry that test_read_verdict_consumes_a_matching_sha_journal_verdict
    admits is REFUSED — loudly, via GateError, never silently adapted or
    treated as absent — when asked about a DIFFERENT sha than the one it
    binds to."""
    log = tmp_path / "runner-verdicts.jsonl"
    monkeypatch.setenv("SABLE_MG_RUNNER_VERDICT_LOG", str(log))
    _write_journal_line(log, ref=RUNNER_REF, preview_sha=PREVIEW_SHA, conclusion="success",
                        run_url="http://hand-run/1", producer="local-runner")

    other_sha = "f" * 40
    with pytest.raises(classify.GateError) as exc:
        preview_lib.read_verdict(REPO, RUNNER_REF, other_sha)
    assert exc.value.code == classify.EXIT_INTEGRITY
    assert PREVIEW_SHA[:7] in str(exc.value)
    assert other_sha[:7] in str(exc.value)


def test_read_verdict_refuses_an_unrecognized_journal_producer(monkeypatch, tmp_path):
    """Producer values are drawn from the typed enum (classify.VerdictSource);
    a value outside it is refused, never passed through as if trustworthy."""
    log = tmp_path / "runner-verdicts.jsonl"
    monkeypatch.setenv("SABLE_MG_RUNNER_VERDICT_LOG", str(log))
    _write_journal_line(log, ref=RUNNER_REF, preview_sha=PREVIEW_SHA, conclusion="success",
                        run_url="http://hand-run/1", producer="a-typo-d-producer")

    with pytest.raises(classify.GateError) as exc:
        preview_lib.read_verdict(REPO, RUNNER_REF, PREVIEW_SHA)
    assert exc.value.code == classify.EXIT_INTEGRITY
    assert "a-typo-d-producer" in str(exc.value)


def test_parse_verdict_source_accepts_every_enum_member():
    for member in classify.VerdictSource:
        assert classify.parse_verdict_source(member.value) is member


def test_parse_verdict_source_rejects_an_unenumerated_value():
    with pytest.raises(classify.GateError) as exc:
        classify.parse_verdict_source("actions-robot")
    assert exc.value.code == classify.EXIT_INTEGRITY


def test_read_verdict_ignores_a_journal_entry_for_a_different_ref(monkeypatch, tmp_path):
    """The journal is keyed by ref, same axis Actions is queried on. A record
    for some OTHER ref must never leak into an answer for this one — falls
    through to Actions exactly as an empty journal would."""
    log = tmp_path / "runner-verdicts.jsonl"
    monkeypatch.setenv("SABLE_MG_RUNNER_VERDICT_LOG", str(log))
    _write_journal_line(log, ref="ci-verify/other-branch-1234567", preview_sha=PREVIEW_SHA,
                        conclusion="success", producer="local-runner")
    monkeypatch.setattr(preview_lib, "_gh_runs", lambda *a, **kw: [
        {"databaseId": 1, "headSha": PREVIEW_SHA, "status": "completed",
         "conclusion": "failure", "url": "http://run/1"}])

    v = preview_lib.read_verdict(REPO, RUNNER_REF, PREVIEW_SHA)
    assert v.source == "precomputed"
    assert v.outcome == classify.RED


# --------------------------------------------------------------------------
# Integration: real git sandbox (SABLE-be4lo.1)
#
# Everything above mocks git_lib._git. These two tests run build_preview and
# preview_kick_ref through REAL git, in a real repo, to prove the keying-
# module consolidation left the single-branch path byte-identical: same
# parent order, same ref shape, no mock standing in for the actual object
# git produces.
# --------------------------------------------------------------------------

def _real_repo_with_base_and_branch(tmp_path):
    """A real repo with two divergent branches — trunk (base) and wk-x
    (branch) — each one commit past a shared root, so their merge-tree is a
    clean two-way merge with no conflict."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "trunk"), ("config", "user.email", "t@sable.invalid"),
                 ("config", "user.name", "SABLE Test")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "root.txt").write_text("root\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "root"], check=True,
                   capture_output=True)
    root_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()

    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "on trunk"], check=True,
                   capture_output=True)
    base_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()

    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "wk-x", root_sha],
                   check=True, capture_output=True)
    (repo / "branch.txt").write_text("branch\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "on wk-x"], check=True,
                   capture_output=True)
    branch_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                capture_output=True, text=True).stdout.strip()
    return str(repo), base_sha, branch_sha


def test_real_git_sandbox_single_branch_preview_parents_and_ref_byte_match(tmp_path):
    """Build a single-branch preview through the refactored path with REAL
    git (build_preview -> batch_key.pair_parents -> commit-tree), then assert
    the built commit's actual parents and the ci-verify ref name it would be
    pushed under are exactly what the pre-consolidation code produced:
    parents == [base_sha, branch_sha], ref == ci-verify/<branch>-<sha7>."""
    repo, base_sha, branch_sha = _real_repo_with_base_and_branch(tmp_path)
    message = f"ci-verify merge-preview: {BRANCH} onto {BASE} (SABLE-x)"

    preview_sha = preview_lib.build_preview(repo, base_sha, branch_sha, message)

    assert git_lib.commit_parents(repo, preview_sha) == [base_sha, branch_sha]

    ref = classify.preview_ref_name(BRANCH, preview_sha)
    assert ref == f"ci-verify/{BRANCH}-{preview_sha[:7]}"

    kick_ref = smg.preview_kick_ref(BRANCH, base_sha, branch_sha)
    assert kick_ref.startswith(f"ci-verify/{BRANCH}-")
    assert kick_ref.count("/") == 1


def test_real_git_sandbox_materialize_preview_pushes_the_real_object(tmp_path):
    """materialize_preview's fresh-build path (no kick to adopt) end to end
    against a real bare remote: the pushed ref resolves to a commit whose
    parents are exactly [base_sha, branch_sha]."""
    repo, base_sha, branch_sha = _real_repo_with_base_and_branch(tmp_path)
    remote_path = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote_path)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", str(remote_path)],
                   check=True, capture_output=True)

    preview_sha, ref, adopted = preview_lib.materialize_preview(
        repo, "origin", BRANCH, BASE, base_sha, branch_sha, "SABLE-x")
    assert adopted is False

    remote_sha = subprocess.run(
        ["git", "-C", str(remote_path), "rev-parse", f"refs/heads/{ref}"],
        check=True, capture_output=True, text=True).stdout.strip()
    assert remote_sha == preview_sha
    subprocess.run(["git", "-C", repo, "fetch", "origin", f"refs/heads/{ref}"],
                   check=True, capture_output=True)
    assert git_lib.commit_parents(repo, preview_sha) == [base_sha, branch_sha]


# --------------------------------------------------------------------------
# Integration: real git sandbox + real bare remote (SABLE-be4lo.4)
#
# Experiment 1 (2026-07-23), hardened: does a pushed ci-verify/batch-<setkey7>
# ref land, and does the already-verified lookup shape (SHA + ci-verify/
# prefix — .github/ci/preview-already-verified.sh keys on exactly those two
# things, never on ref internal shape) recognize it? No live GitHub
# dependency: this asserts the ref that lands has the shape that lookup
# relies on, in a real sandbox with a real bare remote.
# --------------------------------------------------------------------------

def _repo_with_disjoint_members(tmp_path, n):
    """A real repo: root commit (root.txt), trunk gains base.txt (the base),
    and n branches off ROOT each adding their own file — mutually disjoint
    and disjoint from base.txt, so the fold is guaranteed clean."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "trunk"), ("config", "user.email", "t@sable.invalid"),
                 ("config", "user.name", "SABLE Test")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "root.txt").write_text("root\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "root"], check=True,
                   capture_output=True)
    root_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()

    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "on trunk"], check=True,
                   capture_output=True)
    base_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()

    members = []
    for i in range(1, n + 1):
        label = f"wk-member-{i}"
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", label, root_sha],
                       check=True, capture_output=True)
        (repo / f"member{i}.txt").write_text(f"member{i}\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", f"on {label}"], check=True,
                       capture_output=True)
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                             capture_output=True, text=True).stdout.strip()
        members.append(fold_lib.FoldMember(label=label, sha=sha, bead=f"SABLE-{label}"))
    return str(repo), base_sha, members


def test_real_git_sandbox_batch_fold_pushes_ci_verify_batch_ref(tmp_path):
    """push_batch_ref, end to end against a real bare remote: the pushed ref
    is ci-verify/batch-<setkey7> (setkey computed by the ONE owned keying
    module, sable_batch_key_lib.setkey — not re-derived here), it resolves on
    the remote to the fold's real tip SHA, and its shape is exactly what the
    already-verified lookup keys on: a SHA plus a ref starting with
    ci-verify/, indistinguishable in kind from a single-branch preview ref."""
    repo, base_sha, members = _repo_with_disjoint_members(tmp_path, 3)
    remote_path = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote_path)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", str(remote_path)],
                   check=True, capture_output=True)

    tip, ref = fold_lib.push_batch_ref(repo, "origin", base_sha, members)

    # Ref shape: ci-verify/batch-<setkey7>, computed from the owned module.
    expected_key = batch_key.setkey(base_sha, [m.sha for m in members])
    assert ref == f"ci-verify/batch-{expected_key[:7]}"
    # The already-verified lookup's two inputs: a ref under ci-verify/, and
    # the landed SHA at that ref matching what was tested.
    assert ref.startswith("ci-verify/")
    remote_sha = subprocess.run(
        ["git", "-C", str(remote_path), "rev-parse", f"refs/heads/{ref}"],
        check=True, capture_output=True, text=True).stdout.strip()
    assert remote_sha == tip

    # The landed tip is the real fold chain's tip: every member is a parent
    # somewhere in the chain, and the first-parent lineage reaches base.
    subprocess.run(["git", "-C", repo, "fetch", "origin", f"refs/heads/{ref}"],
                   check=True, capture_output=True)
    walk = tip
    for _ in range(len(members)):
        walk = git_lib.commit_parents(repo, walk)[0]
    assert walk == base_sha

    # Sorted-input identity (SABLE-be4lo.1's contract): the ref this real
    # fold landed under is exactly the ref a different admission order of
    # the SAME member set would also resolve to — proven against the real
    # member tip SHAs from this sandbox, not synthetic ones.
    reordered_key = batch_key.setkey(base_sha, [m.sha for m in reversed(members)])
    assert f"ci-verify/batch-{reordered_key[:7]}" == ref


# --------------------------------------------------------------------------
# Integration: real journal file + real Actions leg in the same run
# (SABLE-21rug.2) — the two verdict sources actually coexisting, not just
# each individually mocked. write_runner_verdict is the seat-side writer a
# real tier-runner (SABLE-21rug.3) will call; this proves read_verdict reads
# its own output back end to end, on a real filesystem, with no mock standing
# in for either the journal or the file I/O around it.
# --------------------------------------------------------------------------

def test_seat_side_read_of_a_runner_written_journal_verdict_end_to_end(monkeypatch, tmp_path):
    """A real journal file, written by write_runner_verdict exactly as a
    runner would, read back by read_verdict with no mocking of the journal
    path or its I/O — then, in the SAME run, an ordinary Actions-only verdict
    is read for a completely different ref+sha, proving the two sources
    coexist rather than one seam having quietly replaced the other."""
    log = tmp_path / "runner-verdicts.jsonl"
    monkeypatch.setenv("SABLE_MG_RUNNER_VERDICT_LOG", str(log))
    assert not log.exists()

    runner_ref = "ci-verify/wk-hand-run-1112223"
    preview_lib.write_runner_verdict(REPO, runner_ref, PREVIEW_SHA, "success",
                                     run_url="http://hand-run/42")
    assert log.is_file()

    runner_verdict = preview_lib.read_verdict(REPO, runner_ref, PREVIEW_SHA)
    assert runner_verdict.complete is True
    assert runner_verdict.source == classify.VerdictSource.LOCAL_RUNNER
    assert runner_verdict.outcome == classify.GREEN
    assert runner_verdict.run_url == "http://hand-run/42"

    # Coexistence: a DIFFERENT ref+sha, answered purely by the Actions leg,
    # still works in this same process — the journal for the ref above did
    # not become a global override of every read_verdict call.
    actions_ref = "ci-verify/wk-x-9998887"
    actions_sha = "e" * 40
    monkeypatch.setattr(preview_lib, "_gh_runs", lambda *a, **kw: [
        {"databaseId": 5, "headSha": actions_sha, "status": "completed",
         "conclusion": "failure", "url": "http://run/5"}])
    actions_verdict = preview_lib.read_verdict(REPO, actions_ref, actions_sha)
    assert actions_verdict.source == "precomputed"
    assert actions_verdict.outcome == classify.RED

    # A second hand-run write for the SAME ref (a re-run) appends rather than
    # rewriting, and the newest line is what gets consumed.
    preview_lib.write_runner_verdict(REPO, runner_ref, PREVIEW_SHA, "failure",
                                     run_url="http://hand-run/43")
    assert len(log.read_text().splitlines()) == 2
    rerun_verdict = preview_lib.read_verdict(REPO, runner_ref, PREVIEW_SHA)
    assert rerun_verdict.outcome == classify.RED
    assert rerun_verdict.run_url == "http://hand-run/43"
