#!/usr/bin/env python3
"""Unit tests for the merge-gate module split (SABLE-jd5fj.3).

Three properties, one per reason the split was made:

  1. PROMOTE CONSUMES A STORED VERDICT. When a run for the preview SHA has
     already completed — the normal case once the push-time kick (jd5fj.1) has
     had a head start — promote reads it in one call and NEVER polls. wait_for_ci
     is booby-trapped in those cases, so "it didn't wait" is asserted, not hoped
     for. The fall-through (no stored verdict yet -> wait exactly as before) is
     asserted alongside it, because an optimization that changes the outcome when
     it misses is not an optimization.

  2. MODULE BOUNDARIES HOLD. The preview module answers "what does CI say"; the
     promote module is the only writer to the integration branch. These are
     checked against the SOURCE of each module, not a comment, so the boundary
     cannot rot back into a god-module the way bin/sable-merge-gate did (locked
     smell risk, relates SABLE-hhw7t).

  3. THE TAXONOMY IS UNCHANGED. 0/20/21/22/23/24/4, and the conclusion ->
     outcome mapping behind them, pinned as a table. This is the IRON RULE the
     split was allowed to touch least.

Plus the concurrency regression this bead exists for: ci-verify.yml's group must
be keyed on github.ref so N previews on N distinct ci-verify refs never cancel
each other. Real-git composition lives in hooks/test/test-parallel-previews.sh.
"""
import importlib.util
import inspect
import re
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent
_LOADER = SourceFileLoader("sable_merge_gate", str(_BIN / "sable-merge-gate"))
_SPEC = importlib.util.spec_from_loader("sable_merge_gate", _LOADER)
smg = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(smg)

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
REF = "ci-verify/wk-x-abcdef1"


def _completed_run(conclusion="success", sha=PREVIEW_SHA, url="http://run/9"):
    return [{"databaseId": 9, "headSha": sha, "status": "completed",
             "conclusion": conclusion, "url": url}]


@pytest.fixture
def never_waits(monkeypatch):
    """wait_for_ci is a trap: any case using this fixture claims the verdict was
    already available, and polling would mean it wasn't."""
    def _boom(*a, **kw):
        raise AssertionError("promote waited for CI despite a stored verdict")
    monkeypatch.setattr(preview_lib, "wait_for_ci", _boom)


# --- 1. promote consumes a stored verdict ------------------------------------

def test_read_verdict_returns_a_completed_run_without_polling(monkeypatch, never_waits):
    monkeypatch.setattr(preview_lib, "_gh_runs", lambda *a, **kw: _completed_run())
    v = preview_lib.read_verdict(REPO, REF, PREVIEW_SHA)
    assert v.complete is True
    assert v.conclusion == "success"
    assert v.source == "precomputed"
    assert v.outcome == classify.GREEN


def test_read_verdict_makes_exactly_one_api_call(monkeypatch, never_waits):
    calls = []
    monkeypatch.setattr(preview_lib, "_gh_runs",
                        lambda *a, **kw: calls.append(a) or _completed_run())
    preview_lib.read_verdict(REPO, REF, PREVIEW_SHA)
    assert len(calls) == 1, "a verdict read must be one call, not a poll loop"


def test_read_verdict_reports_pending_for_a_run_still_in_flight(monkeypatch, never_waits):
    runs = [{"headSha": PREVIEW_SHA, "status": "in_progress", "conclusion": None, "url": "u"}]
    monkeypatch.setattr(preview_lib, "_gh_runs", lambda *a, **kw: runs)
    v = preview_lib.read_verdict(REPO, REF, PREVIEW_SHA)
    assert v.complete is False and v.conclusion == "pending"


def test_read_verdict_never_invents_a_conclusion_from_a_non_answer(monkeypatch, never_waits):
    """gh hung / errored / is not installed -> None from _gh_runs. That is the
    absence of an answer, and must NOT be read as 'no runs, therefore down' here:
    read_verdict says pending and lets the caller decide (promote falls through
    to wait_for_ci, which owns the grace/actions_down decision)."""
    monkeypatch.setattr(preview_lib, "_gh_runs", lambda *a, **kw: None)
    v = preview_lib.read_verdict(REPO, REF, PREVIEW_SHA)
    assert v.complete is False and v.conclusion == "pending"


def test_read_verdict_ignores_a_completed_run_for_a_different_sha(monkeypatch, never_waits):
    monkeypatch.setattr(preview_lib, "_gh_runs",
                        lambda *a, **kw: _completed_run(sha="f" * 40))
    v = preview_lib.read_verdict(REPO, REF, PREVIEW_SHA)
    assert v.complete is False, "a verdict for another object is not this object's verdict"


def test_acquire_verdict_consumes_the_stored_verdict_and_does_not_wait(monkeypatch, never_waits):
    monkeypatch.setattr(preview_lib, "_gh_runs", lambda *a, **kw: _completed_run("failure"))
    v = preview_lib.acquire_verdict(REPO, REF, PREVIEW_SHA)
    assert v.source == "precomputed"
    assert v.outcome == classify.RED


def test_acquire_verdict_falls_through_to_waiting_when_nothing_is_stored(monkeypatch):
    monkeypatch.setattr(preview_lib, "_gh_runs", lambda *a, **kw: [])
    monkeypatch.setattr(preview_lib, "wait_for_ci", lambda *a, **kw: ("success", "http://run/w"))
    v = preview_lib.acquire_verdict(REPO, REF, PREVIEW_SHA)
    assert v.source == "waited"
    assert v.outcome == classify.GREEN


def test_promote_on_a_stored_green_verdict_never_polls(monkeypatch, never_waits):
    """The whole point: Chuck's merge pays a read, not a wait. Promote adopts the
    kicked ref, reads the stored verdict, fast-forwards, and returns 0."""
    kick_ref = classify.preview_kick_ref(BRANCH, BASE_SHA, BRANCH_SHA)
    pushes = []

    def fake_git(repo, *args, check=True):
        import subprocess
        head = args[0] if args else ""
        if head == "push":
            pushes.append(args[-1])
        if head == "ls-remote":
            return subprocess.CompletedProcess(args, 0,
                                               stdout=f"{PREVIEW_SHA}\trefs/heads/{kick_ref}\n")
        if head == "rev-list":
            return subprocess.CompletedProcess(args, 0,
                                               stdout=f"{PREVIEW_SHA} {BASE_SHA} {BRANCH_SHA}\n")
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(git_lib, "_git", fake_git)
    monkeypatch.setattr(git_lib, "resolve_commit", lambda repo, ref: (
        BASE_SHA if ref.endswith(BASE) and not pushes else
        PREVIEW_SHA if ref.endswith(BASE) else BRANCH_SHA))
    monkeypatch.setattr(preview_lib, "_gh_runs", lambda *a, **kw: _completed_run())
    monkeypatch.setattr(preview_lib, "build_preview",
                        lambda *a, **kw: pytest.fail("promote rebuilt an adopted preview"))
    monkeypatch.setattr(promote_lib, "_append_evidence", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "_notify", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "cleanup_after_merge", lambda *a, **kw: None)

    assert promote_lib.promote("SABLE-x", BRANCH, BASE, REPO, REMOTE, "chuck", None) == 0
    assert f"{PREVIEW_SHA}:refs/heads/{BASE}" in pushes


def test_promote_does_not_repush_an_adopted_ref(monkeypatch, never_waits):
    """An adopted ref is ALREADY on the remote at this SHA. Re-pushing it is a
    round-trip that changes nothing — and skipping it is part of what makes a
    promote-on-stored-verdict a seconds-long operation."""
    kick_ref = classify.preview_kick_ref(BRANCH, BASE_SHA, BRANCH_SHA)
    import subprocess
    pushes = []

    def fake_git(repo, *args, check=True):
        head = args[0] if args else ""
        if head == "push":
            pushes.append(list(args))
        if head == "ls-remote":
            return subprocess.CompletedProcess(args, 0,
                                               stdout=f"{PREVIEW_SHA}\trefs/heads/{kick_ref}\n")
        if head == "rev-list":
            return subprocess.CompletedProcess(args, 0,
                                               stdout=f"{PREVIEW_SHA} {BASE_SHA} {BRANCH_SHA}\n")
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(git_lib, "_git", fake_git)
    monkeypatch.setattr(git_lib, "resolve_commit",
                        lambda repo, ref: BASE_SHA if ref.endswith(BASE) else BRANCH_SHA)
    monkeypatch.setattr(preview_lib, "_gh_runs", lambda *a, **kw: _completed_run("failure"))
    monkeypatch.setattr(promote_lib, "_append_evidence", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "_notify", lambda *a, **kw: None)

    assert promote_lib.promote("SABLE-x", BRANCH, BASE, REPO, REMOTE, "chuck", None) == 20
    to_ci_ref = [p for p in pushes if any(a.endswith(f"refs/heads/{kick_ref}") for a in p)
                 and "--delete" not in p]
    assert to_ci_ref == [], f"adopted ref was re-pushed: {to_ci_ref}"


# --- 2. module boundaries ----------------------------------------------------

def _source(mod):
    return inspect.getsource(mod)


def test_preview_module_contains_no_promote_logic():
    """The preview module must not learn to promote. Checked against its source
    because a boundary that lives only in a docstring is a boundary that erodes:
    the next hand to add "just one" integration-branch write here should red the
    gate, not pass review on the strength of a comment."""
    src = _source(preview_lib)
    forbidden = {
        "refs/heads/{base}": "pushes to an integration branch",
        "integrity abort": "carries the promote-only integrity assertion",
        "cleanup_after_merge": "reaps a merged worker's branches",
        "--append-notes": "writes bead evidence",
        "SABLE_MG_NOTIFY": "notifies a lane manager",
    }
    found = [f"{needle} ({why})" for needle, why in forbidden.items() if needle in src]
    assert found == [], f"promote logic leaked into the preview module: {found}"


def test_promote_module_does_not_poll_or_build_previews_itself():
    """Symmetrically: promote consumes a verdict, it does not compute one. It may
    CALL the preview module (module-qualified), but must not carry its own
    polling loop or merge-tree construction."""
    src = _source(promote_lib)
    assert "merge-tree" not in src, "promote module builds its own preview"
    assert "gh" not in re.findall(r'"([^"]*gh[^"]*)"', src) or "run list" not in src, \
        "promote module polls Actions directly"
    assert "time.sleep" not in src, "promote module carries a poll loop"


def test_classify_module_is_pure():
    """No subprocess, no os.environ, no network — so the taxonomy can be
    imported and reasoned about (by a test, by Chuck's tooling, by a human)
    without a repo."""
    src = _source(classify)
    for banned in ("subprocess", "os.environ", "_git(", "open("):
        assert banned not in src, f"classify module is not pure: found {banned}"


def test_module_dependency_graph_is_a_dag():
    """classify <- git <- preview <- promote. Nothing imports upward, so any
    module can be loaded (and tested) without dragging the writer path in."""
    assert "sable_gate_git_lib" not in _source(classify)
    assert "sable_gate_preview_lib" not in _source(classify)
    assert "sable_gate_promote_lib" not in _source(git_lib)
    assert "sable_gate_promote_lib" not in _source(preview_lib)


def test_cli_is_thin():
    """bin/sable-merge-gate is argparse + dispatch + re-exports. The 593-line
    monolith this bead split is the thing being prevented from growing back."""
    body = [ln for ln in (_BIN / "sable-merge-gate").read_text().splitlines()]
    # strip the module docstring (the contract lives there and is meant to be long)
    end = next(i for i, ln in enumerate(body[1:], 1) if ln.rstrip().endswith('"""'))
    code = [ln for ln in body[end + 1:] if ln.strip() and not ln.strip().startswith("#")]
    assert len(code) < 150, f"CLI has {len(code)} lines of code — logic is creeping back in"


# --- 3. the exit-code taxonomy is UNCHANGED ----------------------------------

@pytest.mark.parametrize("conclusion,outcome,code", [
    ("success", classify.GREEN, 0),
    ("override", classify.GREEN, 0),
    ("failure", classify.RED, 20),
    ("startup_failure", classify.RED, 20),
    ("neutral", classify.RED, 20),
    ("actions_down", classify.BLOCKED, 21),
    ("timeout", classify.BLOCKED, 21),
    ("cancelled", classify.RETRY, 24),
])
def test_conclusion_taxonomy(conclusion, outcome, code):
    assert classify.classify_conclusion(conclusion) == outcome
    assert classify.OUTCOME_EXIT[outcome] == code
    assert classify.Verdict(conclusion).exit_code == code


def test_unknown_conclusions_are_red_not_green():
    """The conservative default: an Actions conclusion nobody anticipated must
    block the promotion, never slide through."""
    for weird in ("skipped", "action_required", "stale", "", "SUCCESS"):
        assert classify.classify_conclusion(weird) == classify.RED


def test_exit_code_constants_match_the_documented_contract():
    assert (classify.EXIT_OK, classify.EXIT_USAGE, classify.EXIT_PRECONDITION,
            classify.EXIT_INTEGRITY, classify.EXIT_RED, classify.EXIT_BLOCKED,
            classify.EXIT_CONFLICT, classify.EXIT_BASE_MOVED,
            classify.EXIT_CANCELLED) == (0, 2, 3, 4, 20, 21, 22, 23, 24)


def test_the_gate_docstring_still_documents_every_code():
    doc = smg.__doc__
    block = doc.split("Exit codes:", 1)[1]
    for code in ("0", "2", "3", "4", "20", "21", "22", "23", "24"):
        assert re.search(rf"(?<![\d.]){code}(?![\d.]) ", block), \
            f"exit code {code} left the documented contract"


# --- the parallel-preview concurrency regression -----------------------------

_CI_VERIFY = _BIN.parent / ".github" / "workflows" / "ci-verify.yml"


def test_ci_verify_concurrency_group_is_keyed_on_the_ref():
    """THE regression case for this bead. ci-verify.yml sets
    cancel-in-progress: true, which is correct — a re-push to the SAME ref should
    cancel its own stale run. It is only safe because the group is keyed on
    github.ref: N previews on N distinct ci-verify/<bead>-<sha7> refs are N
    distinct groups, so none cancels another. A group keyed on anything coarser
    (a constant, the workflow name, github.workflow alone) would collapse every
    concurrent preview into one group and each new kick would cancel the run
    before it — SABLE-sc24's spurious-cancel shape, fleet-wide."""
    text = _CI_VERIFY.read_text()
    group = re.search(r"(?m)^concurrency:\n(?:.*\n)*?\s*group:\s*(.+)$", text)
    assert group, "ci-verify.yml has no concurrency group"
    assert "github.ref" in group.group(1), \
        f"concurrency group {group.group(1)!r} is not per-ref — concurrent previews would cancel each other"


def test_ci_verify_still_triggers_on_ci_verify_refs():
    """Concurrency is moot if the refs stop triggering runs at all (SABLE-ad21)."""
    assert "'ci-verify/**'" in _CI_VERIFY.read_text()


def test_the_split_makes_the_gate_a_snapshot_pinned_tool():
    """A CONSEQUENCE of the split that must not go unnoticed: the gate now
    imports sibling modules, so a plain per-file pin of bin/sable-merge-gate
    severs those imports. sable-bin-install must classify it 'snapshot'
    (SABLE-9boz4's versioned snapshot DIRECTORY — the pin unit is coextensive
    with the implementation unit, which is exactly why a module split is allowed
    to land inside it). If this ever reads 'plain' again, an operator's pin
    refresh would install a gate that cannot import itself."""
    import subprocess
    out = subprocess.run(["bash", str(_BIN / "sable-bin-install"), "--classify", "sable-merge-gate"],
                         capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "snapshot", out.stdout + out.stderr


def test_distinct_previews_get_distinct_refs():
    """The other half of the concurrency property, on our side of the boundary:
    two different merges must never name the same ci-verify ref, or GitHub would
    put them in one group no matter how the group is keyed."""
    a = classify.preview_kick_ref("wk-one", BASE_SHA, BRANCH_SHA)
    b = classify.preview_kick_ref("wk-two", BASE_SHA, BRANCH_SHA)
    c = classify.preview_kick_ref("wk-one", BASE_SHA, "e" * 40)
    assert len({a, b, c}) == 3
