#!/usr/bin/env python3
"""Unit tests for bin/sable-reconcile-handoffs (SABLE-jfg6.3 / D3).

The pull-based reconciliation floor: query origin + beads, classify each origin
`wk-*` branch against the four-part stranded predicate, and file ONE for-chuck
bead per stranded branch (idempotent, --dry-run files nothing). These are the
UNIT tests — pure predicates plus subprocess seams injected via a monkeypatched
_git / _bd (the _fake_git_factory pattern from test_sable_merge_gate.py). Real
composition against scratch origin.git + a real sandbox bd lives in the
integration variant.

Matrix S1-U5..U10:
  U5  predicate true-positive              -> STRANDED
  U6  in-flight open-bead guard (P2 + P3)  -> NOT stranded (work open / handoff filed)
  U7  already-merged guard (P1)            -> NOT stranded
  U8  age boundary both sides (P4)         -> strict-> only
  U9  idempotency vs existing open bead    -> our own filed title suppresses
  U10 --dry-run files zero                 -> reconcile() issues no bd create
"""
import importlib.util
import json
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_reconcile_handoffs", str(Path(__file__).resolve().parent / "sable-reconcile-handoffs")
)
_SPEC = importlib.util.spec_from_loader("sable_reconcile_handoffs", _LOADER)
smrh = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(smrh)


@pytest.fixture(autouse=True)
def _no_live_panes(monkeypatch):
    """SABLE-4709h: reconcile() calls the real live_pane_bead_ids(repo), which
    shells out to the REAL `tmux` on PATH — this dev host runs an actual SABLE
    fleet, so an un-neutered call would classify against the CURRENT live
    fleet's panes, coupling this unit suite's outcome to whatever real work is
    running at test time (SABLE_RC_GIT/SABLE_RC_BD already get this hermetic
    treatment via _fake_git/_fake_bd; tmux gets it here, once, for every test).
    Autouse so every pre-existing reconcile()-level test keeps its exact prior
    'no live panes' behavior without individually opting in. Tests that WANT a
    live pane override this per-test with their own monkeypatch."""
    monkeypatch.setattr(smrh, "live_pane_bead_ids", lambda repo: set())


def _cp(args, rc, out):
    return subprocess.CompletedProcess(args, rc, stdout=out, stderr="")


# ===========================================================================
# Pure predicates
# ===========================================================================

# --- is_wk_branch / ancestor_rc_is_unmerged / work_bead_qualifies -----------

def test_is_wk_branch():
    assert smrh.is_wk_branch("wk-foo") is True
    assert smrh.is_wk_branch("tmux-only") is False


@pytest.mark.parametrize("rc,expected", [
    (0, False),   # ancestor -> merged
    (1, True),    # not ancestor -> unmerged
    (128, False), # git error -> conservatively NOT unmerged (never invent work off an error)
])
def test_ancestor_rc_is_unmerged(rc, expected):
    assert smrh.ancestor_rc_is_unmerged(rc) is expected


@pytest.mark.parametrize("status,expected", [
    ("closed", True),
    ("in_progress", True),
    ("open", False),
    ("blocked", False),
    (None, False),
])
def test_work_bead_qualifies(status, expected):
    assert smrh.work_bead_qualifies(status) is expected


# --- title_names_branch: delimited-token boundary (idempotency key) ----------

def test_title_names_branch_matches_delimited():
    assert smrh.title_names_branch("[RECONCILE] stranded merge: wk-foo — x", "wk-foo") is True


def test_title_names_branch_no_substring_false_match():
    # wk-foo must NOT match a bead about wk-foobar (would wrongly suppress wk-foo)
    assert smrh.title_names_branch("[AUTO-NOTIFY] Review PR: wk-foobar", "wk-foo") is False


def test_title_names_branch_empty_title():
    assert smrh.title_names_branch(None, "wk-foo") is False
    assert smrh.title_names_branch("", "wk-foo") is False


def test_title_names_branch_matches_hook_filed_title():
    # a hook-filed for-chuck bead also names the branch -> reconciler treats it as
    # already-handled (the two handoff sources are interchangeable)
    assert smrh.title_names_branch("[AUTO-NOTIFY] Review PR from optimus: wk-foo", "wk-foo") is True


# --- age_exceeds_threshold: strict > at the boundary ------------------------

@pytest.mark.parametrize("age_s,thresh_min,expected", [
    (601, 10, True),    # just over 10min -> eligible
    (600, 10, False),   # exactly 10min -> still settling (strict >)
    (599, 10, False),   # under -> too fresh
])
def test_age_exceeds_threshold(age_s, thresh_min, expected):
    assert smrh.age_exceeds_threshold(age_s, thresh_min) is expected


# --- parse_bead_id -----------------------------------------------------------

def test_parse_bead_id_embedded():
    assert smrh.parse_bead_id("wk-SABLE-jfg6.3") == "SABLE-jfg6.3"


def test_parse_bead_id_no_id_token():
    # a single slug with no '-' after stripping wk- yields no id-shaped token
    assert smrh.parse_bead_id("wk-x") is None


def test_parse_bead_id_worktree_prefix():
    assert smrh.parse_bead_id("worktree-bd2-0rk") == "bd2-0rk"


# --- status_from_show_json / first_status_from_search_json ------------------

def test_status_from_show_json_list():
    assert smrh.status_from_show_json(json.dumps([{"id": "X", "status": "closed"}])) == "closed"


def test_status_from_show_json_dict():
    assert smrh.status_from_show_json(json.dumps({"id": "X", "status": "in_progress"})) == "in_progress"


def test_status_from_show_json_bad():
    assert smrh.status_from_show_json("not json") is None
    assert smrh.status_from_show_json("[]") is None


def test_first_status_from_search_json():
    assert smrh.first_status_from_search_json(json.dumps([{"status": "closed"}, {"status": "open"}])) == "closed"
    assert smrh.first_status_from_search_json("[]") is None


# ===========================================================================
# find_work_bead_status — SABLE-i5739: structured `branch` metadata is the
# PRIMARY resolver; embedded-id `bd show` and prose `bd search` are fallbacks
# only, and metadata (once present) always wins over both — this is what
# closes modes (a) silent-failure, (b) wrong-success, and (c) drift.
# ===========================================================================

def _fake_bd_i5739(monkeypatch, *, metadata_status=None, metadata_rc=0,
                    show_status=None, search_status=None):
    """bd list --metadata-field branch=... -> metadata_status (miss when
    None); bd show -> show_status; bd search -> search_status. metadata_rc
    lets a test simulate the metadata QUERY ITSELF failing (nonzero exit),
    distinct from a clean empty-list miss."""
    def fake_bd(repo, *args, check=False):
        head = args[0] if args else ""
        if head == "list":
            if metadata_rc != 0:
                return _cp(args, metadata_rc, "boom")
            payload = [] if metadata_status is None else [{"id": "META", "status": metadata_status}]
            return _cp(args, 0, json.dumps(payload))
        if head == "show":
            if show_status is None:
                return _cp(args, 1, "not found")
            return _cp(args, 0, json.dumps([{"id": "X", "status": show_status}]))
        if head == "search":
            payload = [] if search_status is None else [{"id": "X", "status": search_status}]
            return _cp(args, 0, json.dumps(payload))
        return _cp(args, 0, "")
    monkeypatch.setattr(smrh, "_bd", fake_bd)


def test_i5739_a_structured_metadata_resolves(monkeypatch):
    # (a) a branch with a structured bead reference (metadata) resolves
    _fake_bd_i5739(monkeypatch, metadata_status="closed")
    assert smrh.find_work_bead_status("/repo", "wk-anything") == "closed"


def test_i5739_b_resolves_via_metadata_when_bead_text_never_mentions_it(monkeypatch):
    # (b) prose search would find NOTHING (search_status=None, mode a in the
    # OLD resolver) but the structured field still resolves it — this is
    # exactly the case that "currently returns None" before the fix.
    _fake_bd_i5739(monkeypatch, metadata_status="in_progress", search_status=None)
    assert smrh.find_work_bead_status("/repo", "wk-anything") == "in_progress"


def test_i5739_c_metadata_wins_over_unrelated_bead_prose_hit(monkeypatch):
    # (c) an UNRELATED bead's text happens to mention the branch (the OLD
    # resolver would latch onto it via bd search and return its status,
    # "open" here) — the structured field resolves the REAL bead ("closed")
    # first, so the unrelated hit is never even considered.
    _fake_bd_i5739(monkeypatch, metadata_status="closed", search_status="open")
    assert smrh.find_work_bead_status("/repo", "wk-anything") == "closed"


def test_i5739_d_resolution_stable_as_unrelated_beads_accumulate(monkeypatch):
    # (d) resolve once with no unrelated mentions on record, then again after
    # an unrelated bead mentioning the branch appears (search_status flips
    # from a miss to a hit) — the metadata-backed answer must not move.
    _fake_bd_i5739(monkeypatch, metadata_status="closed", search_status=None)
    first = smrh.find_work_bead_status("/repo", "wk-anything")
    _fake_bd_i5739(monkeypatch, metadata_status="closed", search_status="open")
    second = smrh.find_work_bead_status("/repo", "wk-anything")
    assert first == second == "closed"


def test_i5739_legacy_fallback_to_embedded_id_when_no_metadata(monkeypatch):
    # no bead carries the metadata field (a branch dispatched before this
    # field existed) -> falls back to the embedded-id `bd show` path, unchanged.
    _fake_bd_i5739(monkeypatch, metadata_status=None, show_status="closed")
    assert smrh.find_work_bead_status("/repo", "wk-SABLE-jfg6.3") == "closed"


def test_i5739_legacy_fallback_to_prose_search_when_nothing_else_resolves(monkeypatch):
    # no metadata, no embedded id -> legacy prose search, exactly the
    # pre-existing (prose-fragile) behavior, kept ONLY for already-pushed
    # pre-fix branches.
    _fake_bd_i5739(monkeypatch, metadata_status=None, search_status="closed")
    assert smrh.find_work_bead_status("/repo", "wk-anything") == "closed"


def test_i5739_metadata_query_failure_warns_and_falls_back(monkeypatch, capsys):
    # a failed metadata QUERY (nonzero bd exit) must be a LOUD, distinguishable
    # WARNING — never silently folded into "no bead" (dispatch note #8) — and
    # must still fall back to the legacy paths rather than give up.
    _fake_bd_i5739(monkeypatch, metadata_rc=1, search_status="closed")
    status = smrh.find_work_bead_status("/repo", "wk-anything")
    assert status == "closed"
    err = capsys.readouterr().err
    assert "WARNING" in err and "wk-anything" in err


def test_i5739_e_preview_kick_eligible_unaffected(monkeypatch):
    # (e) preview_kick_eligible takes no bd/bead argument at all — it is
    # STRUCTURALLY independent of find_work_bead_status and every change
    # above; pin its existing behavior unchanged as the explicit acceptance
    # check for AC5 (preview-kick must never couple to this predicate).
    assert smrh.preview_kick_eligible(1, 601.0, 10.0) == (True, "eligible-for-preview-kick")
    assert smrh.preview_kick_eligible(0, 601.0, 10.0) == (
        False, "merged-or-unresolvable(is-ancestor rc=0)")


# ===========================================================================
# classify_branch — the four-part predicate through injected _git / _bd
# ===========================================================================

def _fake_git(monkeypatch, *, ancestor_rc=1, tip_ct="1000"):
    """merge-base --is-ancestor -> ancestor_rc; log -1 --format=%ct -> tip_ct."""
    def fake_git(repo, *args, check=False):
        head = args[0] if args else ""
        if head == "merge-base":
            return _cp(args, ancestor_rc, "")
        if head == "log":
            return _cp(args, 0, tip_ct + "\n")
        return _cp(args, 0, "")
    monkeypatch.setattr(smrh, "_git", fake_git)


def _fake_bd(monkeypatch, *, show_status=None, search_status=None):
    """bd show --json -> show_status (miss when None); bd search --json -> search_status."""
    def fake_bd(repo, *args, check=False):
        head = args[0] if args else ""
        if head == "show":
            if show_status is None:
                return _cp(args, 1, "not found")
            return _cp(args, 0, json.dumps([{"id": "X", "status": show_status}]))
        if head == "search":
            payload = [] if search_status is None else [{"id": "X", "status": search_status}]
            return _cp(args, 0, json.dumps(payload))
        return _cp(args, 0, "")
    monkeypatch.setattr(smrh, "_bd", fake_bd)


NOW = 100000.0
# tip_ct that makes age just over / under the 10min=600s threshold at NOW
CT_OLD = str(int(NOW - 601))   # age 601s -> eligible
CT_AT = str(int(NOW - 600))    # age 600s -> at boundary
CT_FRESH = str(int(NOW - 100)) # age 100s -> too fresh


def _classify(branch="wk-x", open_beads=None):
    return smrh.classify_branch("/repo", "origin", branch, "trunk",
                                open_beads or [], 10.0, NOW)


def test_U5_true_positive_is_stranded(monkeypatch):
    # unmerged + work bead closed + no for-chuck + aged -> STRANDED
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="closed")
    is_stranded, reason, status = _classify()
    assert is_stranded is True, reason
    assert reason == "STRANDED"
    assert status == "closed"


def test_U5b_true_positive_in_progress_via_embedded_id(monkeypatch):
    # bead id embedded in branch -> resolved via bd show; in_progress also qualifies
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, show_status="in_progress")
    is_stranded, reason, status = _classify(branch="wk-SABLE-jfg6.3")
    assert is_stranded is True, reason
    assert status == "in_progress"


def test_U6_in_flight_open_work_bead_not_stranded(monkeypatch):
    # P2: an OPEN work bead (worker not done) is not merge-ready -> not stranded
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="open")
    is_stranded, reason, _ = _classify()
    assert is_stranded is False
    assert "no-qualifying-work-bead" in reason


def test_U6b_open_for_chuck_bead_suppresses(monkeypatch):
    # P3: a for-chuck bead already names the branch -> handoff on record, skip
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="closed")
    open_beads = [{"title": "[AUTO-NOTIFY] Review PR from optimus: wk-x", "labels": ["for-chuck"]}]
    is_stranded, reason, _ = _classify(open_beads=open_beads)
    assert is_stranded is False
    assert reason == "handoff-already-on-record"


def test_U7_already_merged_not_stranded(monkeypatch):
    # P1: branch tip IS an ancestor of the integration branch -> already merged
    _fake_git(monkeypatch, ancestor_rc=0, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="closed")
    is_stranded, reason, _ = _classify()
    assert is_stranded is False
    assert "merged-or-unresolvable" in reason


def test_U7b_git_error_not_stranded(monkeypatch):
    # a git error on ancestry (rc 128) is UNRESOLVED, never a strand -> no bead
    _fake_git(monkeypatch, ancestor_rc=128, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="closed")
    is_stranded, _, _ = _classify()
    assert is_stranded is False


def test_U8_age_at_boundary_not_stranded(monkeypatch):
    # P4 lower side: exactly at the settle threshold -> not yet eligible
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_AT)
    _fake_bd(monkeypatch, search_status="closed")
    is_stranded, reason, _ = _classify()
    assert is_stranded is False
    assert "too-fresh" in reason


def test_U8b_age_just_over_boundary_stranded(monkeypatch):
    # P4 upper side: one second past the threshold -> eligible
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="closed")
    is_stranded, _, _ = _classify()
    assert is_stranded is True


def test_U8c_fresh_push_not_stranded(monkeypatch):
    # a just-landed push (100s old) is inside the settle window -> not raced
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_FRESH)
    _fake_bd(monkeypatch, search_status="closed")
    is_stranded, reason, _ = _classify()
    assert is_stranded is False
    assert "too-fresh" in reason


def test_U9_idempotency_our_own_filed_bead_suppresses(monkeypatch):
    # a prior run's reconcile bead (our OWN title format) names the branch ->
    # this run finds it via P3 and does NOT re-file (idempotency key = branch)
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="closed")
    prior = [{"title": smrh.reconcile_bead_title("wk-x"), "labels": ["for-chuck", "coord"]}]
    is_stranded, reason, _ = _classify(open_beads=prior)
    assert is_stranded is False
    assert reason == "handoff-already-on-record"


# ===========================================================================
# SABLE-vif5e: open_for_chuck_beads' query-failure fallback must go the
# OPPOSITE direction from i5739's metadata-query fallback above. There, an
# empty result would wrongly leave a branch un-strandable, so the safe
# fallback is warn-and-continue. HERE, an empty corpus IS predicate 3's
# suppression signal (reconcile() files a for-chuck bead only when nothing
# already names the branch) — folding a bd failure into "empty" would file a
# DUPLICATE into chuck's queue, which is not self-correcting the way skipping
# one cadence is. Both directions covered, or the fix just moves the bug.
# ===========================================================================

def test_vif5e_nonzero_exit_returns_none_not_empty(monkeypatch, capsys):
    # (1) a failed query (nonzero rc, empty stdout) must be distinguishable
    # from a genuinely empty corpus — None, never [].
    monkeypatch.setattr(smrh, "_bd", lambda repo, *a, check=False: _cp(a, 1, ""))
    assert smrh.open_for_chuck_beads("/repo") is None
    err = capsys.readouterr().err
    assert "WARNING" in err and "1" in err, err


def test_vif5e_rc0_empty_list_is_genuinely_empty(monkeypatch):
    # (2) the genuine no-handoffs-yet case: rc 0, '[]' -> still resolves to an
    # empty list (not None), so a real stranded branch still gets filed.
    monkeypatch.setattr(smrh, "_bd", lambda repo, *a, check=False: _cp(a, 0, "[]"))
    assert smrh.open_for_chuck_beads("/repo") == []


def test_vif5e_malformed_json_treated_as_failure_not_empty(monkeypatch, capsys):
    # (3) rc 0 but unparseable JSON must ALSO be treated as failure, not
    # empty — the same "can't determine" case as a nonzero exit.
    monkeypatch.setattr(smrh, "_bd", lambda repo, *a, check=False: _cp(a, 0, "not json"))
    assert smrh.open_for_chuck_beads("/repo") is None
    err = capsys.readouterr().err
    assert "WARNING" in err, err


def test_vif5e_branch_named_by_open_for_chuck_none_means_assume_named():
    # predicate 3 fed the None sentinel: assume-named (conservative
    # suppression), never assume-absent (which would file a duplicate).
    assert smrh.branch_named_by_open_for_chuck(None, "wk-x") is True


def test_vif5e_classify_branch_suppresses_filing_on_query_failure(monkeypatch):
    # end-to-end at the classify_branch level: a branch that would otherwise
    # classify STRANDED must NOT when the for-chuck corpus is unknown (None) —
    # called directly (not via the `_classify` helper, whose `open_beads or
    # []` idiom would silently coerce the None failure sentinel into empty).
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="closed")
    is_stranded, reason, _ = smrh.classify_branch(
        "/repo", "origin", "wk-x", "trunk", None, 10.0, NOW)
    assert is_stranded is False
    assert reason == "handoff-already-on-record"


def test_vif5e_reconcile_files_zero_when_for_chuck_query_fails(monkeypatch):
    # the caller-level acceptance (4): reconcile() itself must issue NO bd
    # create when open_for_chuck_beads' underlying query failed, even for a
    # branch that is otherwise a true stranded positive.
    monkeypatch.setattr(smrh, "resolve_integration_branch", lambda repo: "trunk")
    monkeypatch.setattr(smrh, "list_origin_wk_branches", lambda repo, remote: ["wk-x"])
    monkeypatch.setattr(smrh, "open_for_chuck_beads", lambda repo: None)
    monkeypatch.setattr(smrh, "branch_ancestor_rc", lambda *a, **k: 1)
    monkeypatch.setattr(smrh, "find_work_bead_status", lambda repo, branch: "closed")
    monkeypatch.setattr(smrh, "branch_tip_age_seconds", lambda *a, **k: 9999.0)
    monkeypatch.setattr(smrh, "kick_preview", lambda *a, **k: 0)

    bd_calls = []

    def fake_git(repo, *args, check=False):
        return _cp(args, 0, "")

    def fake_bd(repo, *args, check=False):
        bd_calls.append(args)
        return _cp(args, 0, "")

    monkeypatch.setattr(smrh, "_git", fake_git)
    monkeypatch.setattr(smrh, "_bd", fake_bd)
    rc = smrh.reconcile("/repo", "origin", 10.0, dry_run=False)
    assert rc == 0
    assert not any(a and a[0] == "create" for a in bd_calls), \
        "a for-chuck query failure must suppress filing, not file a duplicate"


# ===========================================================================
# reconcile() — orchestration, --dry-run files zero
# ===========================================================================

def _spy_reconcile(monkeypatch, *, dry_run, stranded_branch="wk-x"):
    """Drive reconcile() with a single stranded branch; return the list of bd
    argv tuples issued so the test can assert whether a `create` was attempted."""
    monkeypatch.setattr(smrh, "resolve_integration_branch", lambda repo: "trunk")
    monkeypatch.setattr(smrh, "list_origin_wk_branches", lambda repo, remote: [stranded_branch])
    monkeypatch.setattr(smrh, "open_for_chuck_beads", lambda repo: [])
    monkeypatch.setattr(smrh, "branch_ancestor_rc", lambda *a, **k: 1)
    monkeypatch.setattr(smrh, "find_work_bead_status", lambda repo, branch: "closed")
    monkeypatch.setattr(smrh, "branch_tip_age_seconds", lambda *a, **k: 9999.0)
    # Preview-kick leg (SABLE-jd5fj.2) is orthogonal to the stranded-handoff
    # assertions this fixture drives — stub it so these pre-existing tests stay
    # isolated from it. Its own behavior is covered by test_reconcile_preview.py.
    monkeypatch.setattr(smrh, "kick_preview", lambda *a, **k: 0)

    bd_calls = []

    def fake_git(repo, *args, check=False):
        return _cp(args, 0, "")

    def fake_bd(repo, *args, check=False):
        bd_calls.append(args)
        return _cp(args, 0, "created bd-xyz")

    monkeypatch.setattr(smrh, "_git", fake_git)
    monkeypatch.setattr(smrh, "_bd", fake_bd)
    rc = smrh.reconcile("/repo", "origin", 10.0, dry_run)
    return rc, bd_calls


def test_U10_dry_run_files_zero(monkeypatch, capsys):
    rc, bd_calls = _spy_reconcile(monkeypatch, dry_run=True)
    assert rc == 0
    assert not any(a and a[0] == "create" for a in bd_calls), \
        "--dry-run must issue no bd create"
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "would file" in out


def test_non_dry_run_files_one(monkeypatch, capsys):
    # the counterpart: a real run DOES file exactly one for-chuck bead
    rc, bd_calls = _spy_reconcile(monkeypatch, dry_run=False)
    assert rc == 0
    creates = [a for a in bd_calls if a and a[0] == "create"]
    assert len(creates) == 1, creates
    # every bd write is --sandbox and carries the for-chuck,coord labels
    assert "--sandbox" in creates[0]
    assert "--labels=for-chuck,coord" in creates[0]
    assert "filed for-chuck bead" in capsys.readouterr().out


# ===========================================================================
# SABLE-6sdpx: reconcile()'s `git fetch --prune` refresh is check=False and
# was never inspected — a failed refresh silently classified against stale
# remote-tracking refs with no signal anywhere. Both directions: a genuine
# fetch failure must warn loudly (not crash — the sweep is still best-effort
# useful on stale refs), and a healthy fetch must stay silent.
# ===========================================================================

def test_reconcile_fetch_failure_warns_but_continues(monkeypatch, capsys):
    def fake_git(repo, *args, check=False):
        if args and args[0] == "fetch":
            return _cp(args, 128, "fatal: unable to access origin")
        return _cp(args, 0, "")

    monkeypatch.setattr(smrh, "resolve_integration_branch", lambda repo: "trunk")
    monkeypatch.setattr(smrh, "list_origin_wk_branches", lambda repo, remote: [])
    monkeypatch.setattr(smrh, "open_for_chuck_beads", lambda repo: [])
    monkeypatch.setattr(smrh, "_git", fake_git)
    monkeypatch.setattr(smrh, "_bd", lambda repo, *a, check=False: _cp(a, 0, ""))

    rc = smrh.reconcile("/repo", "origin", 10.0, dry_run=True)
    assert rc == 0, "a failed refresh must not crash the sweep"
    err = capsys.readouterr().err
    assert "WARNING" in err and "fetch" in err, err


def test_reconcile_fetch_success_no_warning(monkeypatch, capsys):
    monkeypatch.setattr(smrh, "resolve_integration_branch", lambda repo: "trunk")
    monkeypatch.setattr(smrh, "list_origin_wk_branches", lambda repo, remote: [])
    monkeypatch.setattr(smrh, "open_for_chuck_beads", lambda repo: [])
    monkeypatch.setattr(smrh, "_git", lambda repo, *a, check=False: _cp(a, 0, ""))
    monkeypatch.setattr(smrh, "_bd", lambda repo, *a, check=False: _cp(a, 0, ""))

    rc = smrh.reconcile("/repo", "origin", 10.0, dry_run=True)
    assert rc == 0
    assert capsys.readouterr().err == "", "a healthy fetch must not warn"


# ===========================================================================
# SABLE-2az2x: open_for_chuck_beads() -> None makes classify_branch
# assume-named (skip-filing) for every unmerged branch it sees, cadence after
# cadence, under a PERSISTENT corpus-query failure -- "skip this cadence"
# silently becomes "skip forever". Before this fix the SUMMARY line read
# identically to a genuinely healthy sweep ("0 stranded branch(es), 0
# for-chuck bead(s) filed") in both cases, so a floor that has silently
# stopped filing was indistinguishable from a fleet with nothing stranded.
# Both directions: the broken-corpus summary must say so, and the
# healthy-corpus summary must gain NO new noise.
# ===========================================================================

def _spy_reconcile_corpus(monkeypatch, *, open_beads, bd_stdout="",
                          stranded_branch="wk-x"):
    """Like _spy_reconcile, but drives open_for_chuck_beads directly (None to
    simulate an unreadable corpus, [] for a healthy-empty one) so the summary
    text can be inspected under both."""
    monkeypatch.setattr(smrh, "resolve_integration_branch", lambda repo: "trunk")
    monkeypatch.setattr(smrh, "list_origin_wk_branches", lambda repo, remote: [stranded_branch])
    monkeypatch.setattr(smrh, "open_for_chuck_beads", lambda repo: open_beads)
    monkeypatch.setattr(smrh, "branch_ancestor_rc", lambda *a, **k: 1)
    monkeypatch.setattr(smrh, "find_work_bead_status", lambda repo, branch: "closed")
    monkeypatch.setattr(smrh, "branch_tip_age_seconds", lambda *a, **k: 9999.0)
    monkeypatch.setattr(smrh, "kick_preview", lambda *a, **k: 0)
    monkeypatch.setattr(smrh, "_git", lambda repo, *a, check=False: _cp(a, 0, ""))
    monkeypatch.setattr(smrh, "_bd", lambda repo, *a, check=False: _cp(a, 0, bd_stdout))
    return smrh.reconcile("/repo", "origin", 10.0, dry_run=False)


def _last_summary_line(out: str) -> str:
    lines = [l for l in out.splitlines()
             if l.startswith("sable-reconcile-handoffs:")
             or l.startswith("sable-reconcile-handoffs [DRY-RUN]:")]
    assert lines, f"no summary line found in: {out!r}"
    return lines[-1]


def test_2az2x_summary_flags_corpus_unreadable(monkeypatch, capsys):
    rc = _spy_reconcile_corpus(monkeypatch, open_beads=None)
    assert rc == 0
    summary = _last_summary_line(capsys.readouterr().out)
    assert "0 stranded branch(es), 0 for-chuck bead(s) filed" in summary, summary
    assert "CORPUS UNREADABLE" in summary, summary
    assert "1 branch(es) unassessed" in summary, summary


def test_2az2x_summary_unchanged_when_corpus_healthy(monkeypatch, capsys):
    # counterpart: a genuinely empty (not failed) corpus must file the real
    # stranded branch exactly as before, and the summary must carry none of
    # the new corpus-unreadable noise.
    rc = _spy_reconcile_corpus(monkeypatch, open_beads=[], bd_stdout="created bd-xyz")
    assert rc == 0
    summary = _last_summary_line(capsys.readouterr().out)
    assert "CORPUS UNREADABLE" not in summary, summary
    assert "unassessed" not in summary, summary
    assert "1 stranded branch(es), 1 for-chuck bead(s) filed;" in summary, summary


def test_2az2x_dry_run_summary_flags_corpus_unreadable(monkeypatch, capsys):
    # the --dry-run summary line is a distinct f-string from the real-run one
    # -- cover it separately so a future edit can't silently miss one branch.
    monkeypatch.setattr(smrh, "resolve_integration_branch", lambda repo: "trunk")
    monkeypatch.setattr(smrh, "list_origin_wk_branches", lambda repo, remote: ["wk-x"])
    monkeypatch.setattr(smrh, "open_for_chuck_beads", lambda repo: None)
    monkeypatch.setattr(smrh, "branch_ancestor_rc", lambda *a, **k: 1)
    monkeypatch.setattr(smrh, "find_work_bead_status", lambda repo, branch: "closed")
    monkeypatch.setattr(smrh, "branch_tip_age_seconds", lambda *a, **k: 9999.0)
    monkeypatch.setattr(smrh, "kick_preview", lambda *a, **k: 0)
    monkeypatch.setattr(smrh, "_git", lambda repo, *a, check=False: _cp(a, 0, ""))
    monkeypatch.setattr(smrh, "_bd", lambda repo, *a, check=False: _cp(a, 0, ""))

    rc = smrh.reconcile("/repo", "origin", 10.0, dry_run=True)
    assert rc == 0
    summary = _last_summary_line(capsys.readouterr().out)
    assert "DRY-RUN" in summary
    assert "CORPUS UNREADABLE" in summary, summary
    assert "1 branch(es) unassessed" in summary, summary


# ===========================================================================
# SABLE-jejx3: HELD is a first-class THIRD outcome, not a suppression.
#
# The defect: a branch unmerged BY DELIBERATE DECISION satisfies all four
# stranded predicates identically to one unmerged BY ACCIDENT, so the floor
# filed a handoff whose content ("merge it") was the EXACT INVERSE of the
# standing instruction — and re-filed it every cadence once Chuck closed it.
#
# Every test below carries its NEGATIVE direction, because a hold check that
# passes vacuously (e.g. by never filing at all) would look identical to a
# working one. The hold marker must be what does the work.
# ===========================================================================

NOW_HOLD = 1_800_000_000.0  # fixed clock; all hold ages below are derived from it


def _bead(hold=None, by=None, since=None, until=None, status="in_progress",
          bead_id="SABLE-work"):
    """A work-bead record as bd emits it, optionally carrying a hold."""
    meta = {"branch": "wk-x"}
    if hold is not None:
        meta[smrh.HOLD_REASON_KEY] = hold
    if by is not None:
        meta[smrh.HOLD_BY_KEY] = by
    if since is not None:
        meta[smrh.HOLD_SINCE_KEY] = since
    if until is not None:
        meta[smrh.HOLD_UNTIL_KEY] = until
    return {"id": bead_id, "status": status, "metadata": meta}


def _iso_days_ago(days, now=NOW_HOLD):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(now - days * 86400.0,
                                  tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_days_ago_wallclock(days):
    """Relative to the REAL clock — reconcile() takes its own time.time(), so
    reconcile-level tests cannot use the fixed NOW_HOLD the pure-function tests
    use (a NOW_HOLD-relative date is simply in the future from the real clock's
    point of view, and every hold would read as age 0)."""
    import time as _time
    return _iso_days_ago(days, now=_time.time())


# --- hold_from_bead: presence keyed on a non-empty reason -------------------

def test_hold_from_bead_present():
    h = smrh.hold_from_bead(_bead(hold="security regression", by="tarzan",
                                  since="2026-07-21T00:00:00Z",
                                  until="tarzan green-lights a revised tip"))
    assert h["reason"] == "security regression"
    assert h["by"] == "tarzan"
    assert h["until"] == "tarzan green-lights a revised tip"
    assert h["bead"] == "SABLE-work"


@pytest.mark.parametrize("bead", [
    None,                                   # no bead at all
    {"id": "X", "status": "closed"},        # bead with no metadata
    {"id": "X", "metadata": {}},            # metadata with no hold key
    {"id": "X", "metadata": {"hold": ""}},  # empty reason is NOT a hold
    {"id": "X", "metadata": {"hold": "   "}},
])
def test_hold_from_bead_absent(bead):
    assert smrh.hold_from_bead(bead) is None


def test_hold_from_bead_reason_only_is_still_a_hold():
    # a sloppily-placed hold (reason only) must still PROTECT the branch; the
    # missing fields are review flags, never a reason to ignore the hold.
    h = smrh.hold_from_bead(_bead(hold="do not merge"))
    assert h is not None and h["by"] is None and h["until"] is None


# --- classify_branch: the headline pair (marker on / marker off) ------------

def test_held_branch_classifies_as_held_not_stranded(monkeypatch):
    """THE test named in the bead's spec, both directions in one place.

    POSITIVE: a branch whose work bead carries a hold marker classifies HELD
    and NOT stranded. NEGATIVE: the byte-for-byte IDENTICAL branch WITHOUT the
    marker still classifies STRANDED — so the marker is what does the work and
    this cannot pass vacuously (e.g. by the floor having stopped filing at all).
    """
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="closed")

    hold = smrh.hold_from_bead(_bead(hold="false-negative security regression",
                                     by="tarzan", since=_iso_days_ago(0.5),
                                     until="tarzan green-lights a revised tip"))
    is_stranded, reason, _ = smrh.classify_branch(
        "/repo", "origin", "wk-x", "trunk", [], 10.0, NOW_HOLD, hold)
    assert is_stranded is False, reason
    assert reason.startswith("HELD"), reason
    # the four fields chuck required: held, by whom, since when, until what
    assert "by=tarzan" in reason
    assert "since=" in reason and "until=tarzan green-lights a revised tip" in reason
    assert "false-negative security regression" in reason

    # NEGATIVE direction — identical branch, no marker
    is_stranded, reason, status = smrh.classify_branch(
        "/repo", "origin", "wk-x", "trunk", [], 10.0, NOW_HOLD, None)
    assert is_stranded is True, reason
    assert reason == "STRANDED"
    assert status == "closed"


def test_held_wins_over_every_other_stranded_predicate(monkeypatch):
    # the hold is checked immediately after ancestry, so a held branch is
    # reported HELD (not "no-qualifying-work-bead", not "too-fresh") whatever
    # the other predicates say — the operator must see the REASON it is not
    # merging, and that reason is the hold.
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_FRESH)
    _fake_bd(monkeypatch, search_status="open")
    hold = smrh.hold_from_bead(_bead(hold="held"))
    is_stranded, reason, _ = smrh.classify_branch(
        "/repo", "origin", "wk-x", "trunk", [], 10.0, NOW_HOLD, hold)
    assert is_stranded is False
    assert reason.startswith("HELD"), reason


def test_merged_branch_is_still_merged_even_if_held(monkeypatch):
    # P1 stays first: an already-merged branch is not interesting, held or not.
    _fake_git(monkeypatch, ancestor_rc=0, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="closed")
    hold = smrh.hold_from_bead(_bead(hold="held"))
    is_stranded, reason, _ = smrh.classify_branch(
        "/repo", "origin", "wk-x", "trunk", [], 10.0, NOW_HOLD, hold)
    assert is_stranded is False
    assert "merged-or-unresolvable" in reason


def test_hold_unknown_suppresses_filing_and_says_so(monkeypatch):
    # third outcome: the work bead could not be READ. Neither held nor
    # not-held can be established, so the floor must NOT file (an inverted
    # 'merge me' against a genuinely held branch is the unrecoverable
    # direction) — and must say which one it is, never a clean skip.
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="closed")
    is_stranded, reason, _ = smrh.classify_branch(
        "/repo", "origin", "wk-x", "trunk", [], 10.0, NOW_HOLD, smrh.HOLD_UNKNOWN)
    assert is_stranded is False
    assert "hold-state-unknown" in reason, reason
    assert "NOT filing" in reason, reason


# --- the hold must survive a BRANCH RENAME (the fourth destroyer) -----------

def _fake_bd_meta_record(monkeypatch, record_by_branch):
    """bd list --metadata-field branch=<b> resolves record_by_branch[b]."""
    def fake_bd(repo, *args, check=False):
        if args and args[0] == "list":
            field = next((a for a in args if a.startswith("branch=")), "")
            br = field.split("=", 1)[1] if "=" in field else ""
            rec = record_by_branch.get(br)
            return _cp(args, 0, json.dumps([rec] if rec else []))
        if args and args[0] == "show":
            return _cp(args, 1, "not found")
        if args and args[0] == "search":
            return _cp(args, 0, "[]")
        return _cp(args, 0, "")
    monkeypatch.setattr(smrh, "_bd", fake_bd)


def test_hold_survives_a_branch_rename(monkeypatch):
    # The gz3v2 bandage keyed the hold on the BRANCH NAME appearing in a bead
    # TITLE, so a rename / re-push under a new name silently dropped the
    # protection with nothing logged. The first-class hold is keyed on the WORK
    # BEAD: re-point its `branch` metadata and the hold travels with the work.
    bead = _bead(hold="regression", by="tarzan", since=_iso_days_ago(1),
                 until="revised tip")
    _fake_bd_meta_record(monkeypatch, {"wk-old-name": bead})
    assert smrh.find_work_bead_hold("/repo", "wk-old-name")["reason"] == "regression"

    # the branch is renamed; the SAME bead now points at the new name
    _fake_bd_meta_record(monkeypatch, {"wk-brand-new-name": bead})
    held = smrh.find_work_bead_hold("/repo", "wk-brand-new-name")
    assert held is not None and held["reason"] == "regression", \
        "a hold keyed on the work bead must survive a branch rename"
    # ...and the OLD name, which no longer has a work bead, is not held
    assert smrh.find_work_bead_hold("/repo", "wk-old-name") is None


def test_hold_found_even_when_another_bead_joins_the_branch_first(monkeypatch):
    """FOUND ON THE LIVE CORPUS while migrating the wk-dep-merge-guard hold:
    TWO beads carried `branch=wk-dep-merge-guard` — a scratch observation bead
    and the real work bead — and the scratch bead sorted FIRST. A resolver that
    reads only the first joined record silently misses the hold and resumes
    filing the inverted 'merge me' handoff, with nothing logged. Ordering must
    never decide whether a hold is honoured: if ANY joined bead is held, the
    branch is held."""
    def fake_bd(repo, *args, check=False):
        if args and args[0] == "list":
            return _cp(args, 0, json.dumps([
                {"id": "SCRATCH", "status": "closed",
                 "metadata": {"branch": "wk-x"}},                     # sorts first, unheld
                _bead(hold="rejected tip", by="tarzan",
                      since=_iso_days_ago(1), until="revised tip"),   # the real hold
            ]))
        return _cp(args, 0, "[]")
    monkeypatch.setattr(smrh, "_bd", fake_bd)

    hold = smrh.find_work_bead_hold("/repo", "wk-x")
    assert hold is not None and hold["reason"] == "rejected tip", \
        "a hold on a non-first joined bead must still hold the branch"
    # ...and predicate 2 keeps reading the FIRST record, exactly as before
    assert smrh.find_work_bead_status("/repo", "wk-x") == "closed"


def test_no_hold_when_no_joined_bead_is_held(monkeypatch):
    # counterpart to the above: scanning all joined beads must not manufacture
    # a hold out of unheld ones.
    def fake_bd(repo, *args, check=False):
        if args and args[0] == "list":
            return _cp(args, 0, json.dumps([
                {"id": "A", "status": "closed", "metadata": {"branch": "wk-x"}},
                {"id": "B", "status": "in_progress", "metadata": {"branch": "wk-x"}},
            ]))
        return _cp(args, 0, "[]")
    monkeypatch.setattr(smrh, "_bd", fake_bd)
    assert smrh.find_work_bead_hold("/repo", "wk-x") is None


def test_find_work_bead_hold_unreadable_when_query_fails(monkeypatch):
    # every resolver path fails -> HOLD_UNKNOWN, never None (not-held)
    monkeypatch.setattr(smrh, "_bd", lambda repo, *a, check=False: _cp(a, 1, "boom"))
    assert smrh.find_work_bead_hold("/repo", "wk-x") == smrh.HOLD_UNKNOWN


def test_find_work_bead_hold_none_when_bead_reads_clean_and_unheld(monkeypatch):
    # counterpart: a clean read of an UNHELD bead is None, not HOLD_UNKNOWN —
    # otherwise every ordinary branch would be treated as maybe-held and the
    # floor would stop filing entirely.
    _fake_bd_meta_record(monkeypatch, {"wk-x": _bead()})
    assert smrh.find_work_bead_hold("/repo", "wk-x") is None


def test_find_work_bead_status_still_resolves_through_the_shared_resolver(monkeypatch):
    # find_work_bead_status is now a thin accessor over find_work_bead; pin
    # that the refactor did not change what it returns.
    _fake_bd_meta_record(monkeypatch, {"wk-x": _bead(status="closed")})
    assert smrh.find_work_bead_status("/repo", "wk-x") == "closed"


# --- a hold must decay LOUDLY, never silently ------------------------------

def test_hold_review_flags_clean_hold_has_none():
    hold = smrh.hold_from_bead(_bead(hold="r", by="tarzan",
                                     since=_iso_days_ago(0.5), until="revised tip"))
    assert smrh.hold_review_flags(hold, NOW_HOLD, 3.0) == []


def test_hold_review_flags_stale():
    hold = smrh.hold_from_bead(_bead(hold="r", by="tarzan",
                                     since=_iso_days_ago(9), until="revised tip"))
    flags = smrh.hold_review_flags(hold, NOW_HOLD, 3.0)
    assert any(f.startswith("STALE(") for f in flags), flags


def test_hold_review_flags_unowned_and_no_release_condition():
    hold = smrh.hold_from_bead(_bead(hold="r", since=_iso_days_ago(0.1)))
    flags = smrh.hold_review_flags(hold, NOW_HOLD, 3.0)
    assert any("UNOWNED" in f for f in flags), flags
    assert any("NO-RELEASE-CONDITION" in f for f in flags), flags


def test_hold_review_flags_unparseable_since_is_age_unknown_not_fresh():
    # an unparseable date must never read as a fresh hold — that would let a
    # hold with a typo'd timestamp sit forever without ever going stale.
    hold = smrh.hold_from_bead(_bead(hold="r", by="t", since="last tuesday",
                                     until="x"))
    assert smrh.hold_age_days(hold, NOW_HOLD) is None
    assert any("AGE-UNKNOWN" in f for f in smrh.hold_review_flags(hold, NOW_HOLD, 3.0))


@pytest.mark.parametrize("since,expect_days", [
    ("2026-07-21T00:00:00Z", True),
    ("2026-07-21T00:00:00+00:00", True),
    ("2026-07-21T00:00:00", True),   # naive -> assumed UTC
    ("not-a-date", False),
    (None, False),
])
def test_parse_hold_since(since, expect_days):
    assert (smrh.parse_hold_since(since) is not None) is expect_days


def test_describe_hold_carries_all_four_fields_and_flags():
    hold = smrh.hold_from_bead(_bead(hold="regression", since=_iso_days_ago(9)))
    text = smrh.describe_hold(hold, NOW_HOLD, 3.0)
    assert text.startswith("HELD")
    assert "by=UNKNOWN" in text and "until=UNSTATED" in text
    assert "reason=regression" in text
    assert "NEEDS REVIEW" in text and "STALE(" in text


def test_hold_lift_hint_names_the_work_bead():
    hold = smrh.hold_from_bead(_bead(hold="r", bead_id="SABLE-vx4aj"))
    assert "SABLE-vx4aj" in smrh.hold_lift_hint(hold)
    assert "--unset-metadata hold" in smrh.hold_lift_hint(hold)


# --- reconcile(): held branches file nothing but are NAMED every cadence ----

def _spy_reconcile_hold(monkeypatch, *, hold, stranded_branch="wk-x", dry_run=False):
    """Drive reconcile() with one unmerged branch whose hold state is `hold`
    (a record, None, or HOLD_UNKNOWN). Returns (rc, bd argv list)."""
    monkeypatch.setattr(smrh, "resolve_integration_branch", lambda repo: "trunk")
    monkeypatch.setattr(smrh, "list_origin_wk_branches", lambda repo, remote: [stranded_branch])
    monkeypatch.setattr(smrh, "open_for_chuck_beads", lambda repo: [])
    monkeypatch.setattr(smrh, "branch_ancestor_rc", lambda *a, **k: 1)
    monkeypatch.setattr(smrh, "find_work_bead_status", lambda repo, branch: "closed")
    monkeypatch.setattr(smrh, "find_work_bead_hold", lambda repo, branch: hold)
    monkeypatch.setattr(smrh, "branch_tip_age_seconds", lambda *a, **k: 9999.0)
    monkeypatch.setattr(smrh, "kick_preview", lambda *a, **k: 0)

    bd_calls = []

    def fake_bd(repo, *args, check=False):
        bd_calls.append(args)
        return _cp(args, 0, "created bd-xyz")

    monkeypatch.setattr(smrh, "_git", lambda repo, *a, check=False: _cp(a, 0, ""))
    monkeypatch.setattr(smrh, "_bd", fake_bd)
    rc = smrh.reconcile("/repo", "origin", 10.0, dry_run)
    return rc, bd_calls


def test_reconcile_held_branch_files_nothing_but_is_named(monkeypatch, capsys):
    hold = smrh.hold_from_bead(_bead(hold="security regression", by="tarzan",
                                     since=_iso_days_ago_wallclock(0.2), until="revised tip"))
    rc, bd_calls = _spy_reconcile_hold(monkeypatch, hold=hold)
    assert rc == 0
    assert not any(a and a[0] == "create" for a in bd_calls), \
        "a HELD branch must never produce an inverted 'merge me' handoff"
    out = capsys.readouterr().out
    assert "HELD BRANCHES (1)" in out, out
    assert "HELD wk-x:" in out, out
    assert "to lift: bd update" in out, out
    summary = _last_summary_line(out)
    assert "1 held branch(es)" in summary, summary
    assert "wk-x" in summary, "the summary must NAME the held branch, never merely count it"


def test_reconcile_unheld_branch_still_files_positive_control(monkeypatch, capsys):
    # POSITIVE CONTROL for the test above: the same sweep, hold removed, DOES
    # file — proving the sweep was capable of filing in that configuration and
    # the hold marker is what stopped it.
    rc, bd_calls = _spy_reconcile_hold(monkeypatch, hold=None)
    assert rc == 0
    creates = [a for a in bd_calls if a and a[0] == "create"]
    assert len(creates) == 1, creates
    out = capsys.readouterr().out
    assert "HELD BRANCHES" not in out, out
    assert "0 held branch(es)" in _last_summary_line(out)


def test_reconcile_stale_hold_is_flagged_in_the_summary(monkeypatch, capsys):
    # a forgotten hold is self-silencing by construction — it suppresses the
    # report that would surface it — so age must escalate INTO the summary.
    hold = smrh.hold_from_bead(_bead(hold="r", by="tarzan",
                                     since=_iso_days_ago_wallclock(30), until="x"))
    rc, _ = _spy_reconcile_hold(monkeypatch, hold=hold)
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE(" in out, out
    assert "1 NEEDING REVIEW" in _last_summary_line(out)


def test_reconcile_fresh_hold_is_not_flagged(monkeypatch, capsys):
    # counterpart: a well-formed, fresh hold adds no review noise.
    hold = smrh.hold_from_bead(_bead(hold="r", by="tarzan",
                                     since=_iso_days_ago_wallclock(0.1), until="x"))
    _spy_reconcile_hold(monkeypatch, hold=hold)
    summary = _last_summary_line(capsys.readouterr().out)
    assert "NEEDING REVIEW" not in summary, summary
    assert "1 held branch(es)" in summary


def test_reconcile_hold_unreadable_files_nothing_and_says_so(monkeypatch, capsys):
    rc, bd_calls = _spy_reconcile_hold(monkeypatch, hold=smrh.HOLD_UNKNOWN)
    assert rc == 0
    assert not any(a and a[0] == "create" for a in bd_calls)
    out = capsys.readouterr().out
    assert "HOLD-STATE UNREADABLE wk-x" in out, out
    summary = _last_summary_line(out)
    assert "UNREADABLE hold state" in summary, summary
    assert "wk-x" in summary, summary


def test_reconcile_dry_run_summary_also_reports_holds(monkeypatch, capsys):
    # the --dry-run summary is a distinct f-string from the real-run one; cover
    # it separately so a future edit cannot silently miss one branch.
    hold = smrh.hold_from_bead(_bead(hold="r", by="tarzan",
                                     since=_iso_days_ago_wallclock(0.1), until="x"))
    rc, _ = _spy_reconcile_hold(monkeypatch, hold=hold, dry_run=True)
    assert rc == 0
    summary = _last_summary_line(capsys.readouterr().out)
    assert "DRY-RUN" in summary
    assert "1 held branch(es)" in summary and "wk-x" in summary, summary


def test_reconcile_held_branch_is_still_preview_kicked(monkeypatch, capsys):
    # the preview-kick leg is structurally independent of the stranded/hold
    # predicates (it reads P1/P4 only) — a hold must not silently disable CI
    # warm-up, which would be a second, unannounced behavior change.
    hold = smrh.hold_from_bead(_bead(hold="r", by="t", since=_iso_days_ago_wallclock(0.1), until="x"))
    _spy_reconcile_hold(monkeypatch, hold=hold)
    out = capsys.readouterr().out
    assert "preview-kick" in out
    assert "1 preview-kick candidate(s)" in _last_summary_line(out)


# ===========================================================================
# SABLE-z7gue: content-containment (patch-id). A rebased branch changes every
# SHA, so tip-containment (predicate 1, `git merge-base --is-ancestor`)
# cannot tell a REBASED-AND-LANDED branch from a genuinely stranded one — and
# neither can 'ask both lanes' (SABLE-xw32f's remedy for the ambiguous case),
# because every lane truthfully answers 'not mine' when the work is already
# on the spine. branch_content_contained reads `git cherry <upstream>
# <head>`: '-' means a patch-id-equivalent commit already exists at the
# spine, '+' means genuinely new.
# ===========================================================================

def _fake_git_cherry(monkeypatch, *, ancestor_rc=1, tip_ct="1000", cherry_lines=()):
    """merge-base --is-ancestor -> ancestor_rc; log -1 --format=%ct -> tip_ct;
    cherry <upstream> <head> -> cherry_lines, one per line."""
    def fake_git(repo, *args, check=False):
        head = args[0] if args else ""
        if head == "merge-base":
            return _cp(args, ancestor_rc, "")
        if head == "log":
            return _cp(args, 0, tip_ct + "\n")
        if head == "cherry":
            body = "\n".join(cherry_lines) + ("\n" if cherry_lines else "")
            return _cp(args, 0, body)
        return _cp(args, 0, "")
    monkeypatch.setattr(smrh, "_git", fake_git)


def test_branch_content_contained_true_when_every_commit_equivalent(monkeypatch):
    _fake_git_cherry(monkeypatch, cherry_lines=["- abc1234 rebase copy of the only commit"])
    contained, detail = smrh.branch_content_contained("/repo", "origin", "wk-x", "trunk")
    assert contained is True
    assert "content-contained" in detail


def test_branch_content_contained_false_when_any_commit_is_new(monkeypatch):
    # PARTIAL landing (some commits landed, some not) stays conservatively
    # NOT-contained — this floor's own stranded remedy is a write to the
    # spine, so an indeterminate reading must never assert containment it
    # cannot fully back.
    _fake_git_cherry(monkeypatch, cherry_lines=[
        "- abc1234 landed commit",
        "+ def5678 genuinely new, unlanded commit",
    ])
    contained, detail = smrh.branch_content_contained("/repo", "origin", "wk-x", "trunk")
    assert contained is False
    assert "patch-id-partial" in detail


def test_branch_content_contained_false_on_git_error(monkeypatch):
    def fake_git(repo, *args, check=False):
        if args and args[0] == "cherry":
            return _cp(args, 128, "fatal: bad revision")
        return _cp(args, 0, "")
    monkeypatch.setattr(smrh, "_git", fake_git)
    contained, detail = smrh.branch_content_contained("/repo", "origin", "wk-x", "trunk")
    assert contained is False
    assert "unresolvable" in detail


def test_branch_content_contained_false_on_empty_result(monkeypatch):
    # ancestry already said unmerged; an EMPTY cherry listing disagrees with
    # that, so this is left NOT-contained rather than inventing a verdict.
    _fake_git_cherry(monkeypatch, cherry_lines=[])
    contained, detail = smrh.branch_content_contained("/repo", "origin", "wk-x", "trunk")
    assert contained is False
    assert "empty" in detail


def test_rebased_and_landed_commit_is_not_stranded(monkeypatch):
    # THE test named in z7gue's spec: unmerged by tip (predicate 1 says
    # "not an ancestor"), but the commit is patch-id-equivalent at the spine
    # under a different sha -> NOT stranded.
    _fake_git_cherry(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD,
                     cherry_lines=["- abc1234 rebase copy"])
    _fake_bd(monkeypatch, search_status="closed")
    is_stranded, reason, _ = _classify()
    assert is_stranded is False, reason
    assert "landed-under-different-sha" in reason


def test_genuinely_unlanded_commit_is_still_stranded(monkeypatch):
    # Negative control in the SAME file (z7gue's spec, plant-and-fail
    # requirement): no patch-id equivalent exists at the spine -> the branch
    # MUST still classify STRANDED, so the fix above cannot pass by simply
    # disabling the detector.
    _fake_git_cherry(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD,
                     cherry_lines=["+ abc1234 genuinely new, unlanded commit"])
    _fake_bd(monkeypatch, search_status="closed")
    is_stranded, reason, status = _classify()
    assert is_stranded is True, reason
    assert reason == "STRANDED"
    assert status == "closed"


def test_no_bead_when_content_is_contained_under_a_different_sha(monkeypatch, capsys):
    # SABLE-4709h's negative-control matrix, full reconcile() sweep: a
    # rebased-and-landed branch must file NOTHING end-to-end, not merely
    # classify correctly in isolation.
    monkeypatch.setattr(smrh, "resolve_integration_branch", lambda repo: "trunk")
    monkeypatch.setattr(smrh, "list_origin_wk_branches", lambda repo, remote: ["wk-x"])
    monkeypatch.setattr(smrh, "open_for_chuck_beads", lambda repo: [])
    monkeypatch.setattr(smrh, "branch_ancestor_rc", lambda *a, **k: 1)
    monkeypatch.setattr(smrh, "find_work_bead_status", lambda repo, branch: "closed")
    monkeypatch.setattr(smrh, "branch_tip_age_seconds", lambda *a, **k: 9999.0)
    monkeypatch.setattr(smrh, "kick_preview", lambda *a, **k: 0)
    monkeypatch.setattr(smrh, "branch_content_contained",
                        lambda *a, **k: (True, "content-contained(1 commit(s))"))

    bd_calls = []

    def fake_bd(repo, *args, check=False):
        bd_calls.append(args)
        return _cp(args, 0, "created bd-xyz")

    monkeypatch.setattr(smrh, "_git", lambda repo, *a, check=False: _cp(a, 0, ""))
    monkeypatch.setattr(smrh, "_bd", fake_bd)

    rc = smrh.reconcile("/repo", "origin", 10.0, dry_run=False)
    assert rc == 0
    assert not any(a and a[0] == "create" for a in bd_calls), bd_calls
    assert "landed-under-different-sha" in capsys.readouterr().out


# ===========================================================================
# SABLE-4709h: no true positive had ever been demonstrated end-to-end for
# this detector across a full shift of sixteen firings. This is that missing
# known-positive control at the reconcile()-level (test_U5_true_positive_is_
# stranded already pins the pure classify_branch predicate; this pins the
# full sweep filing a bead off it), plus the negative-control matrix for the
# three other live states a real fleet actually produces.
# ===========================================================================

def test_detector_fires_on_a_constructed_true_positive(monkeypatch, capsys):
    rc, bd_calls = _spy_reconcile(monkeypatch, dry_run=False)
    assert rc == 0
    creates = [a for a in bd_calls if a and a[0] == "create"]
    assert len(creates) == 1, "the constructed true positive must file exactly one bead"
    assert "--labels=for-chuck,coord" in creates[0]
    out = capsys.readouterr().out
    assert "STRANDED" in out
    assert "filed for-chuck bead for stranded branch wk-x" in out


def test_no_bead_when_a_live_worker_pane_holds_the_branch(monkeypatch, capsys):
    # SABLE-4709h IN-ACTIVE-REVISE: "1 IN ACTIVE REVISE — a live worker pane,
    # bounced by its own manager (SABLE-660hy)" from the sixteen-firing
    # triage. A live pane tagged (via @sable_bead) with this branch's work
    # bead id means a manager already knows and a worker is on it — every
    # OTHER stranded predicate is satisfied here, and it must still file
    # nothing.
    monkeypatch.setattr(smrh, "resolve_integration_branch", lambda repo: "trunk")
    monkeypatch.setattr(smrh, "list_origin_wk_branches",
                        lambda repo, remote: ["wk-SABLE-live1"])
    monkeypatch.setattr(smrh, "open_for_chuck_beads", lambda repo: [])
    monkeypatch.setattr(smrh, "branch_ancestor_rc", lambda *a, **k: 1)
    monkeypatch.setattr(smrh, "find_work_bead_status", lambda repo, branch: "closed")
    monkeypatch.setattr(smrh, "branch_tip_age_seconds", lambda *a, **k: 9999.0)
    monkeypatch.setattr(smrh, "kick_preview", lambda *a, **k: 0)
    monkeypatch.setattr(smrh, "live_pane_bead_ids", lambda repo: {"SABLE-live1"})

    bd_calls = []

    def fake_bd(repo, *args, check=False):
        head = args[0] if args else ""
        if head == "create":
            bd_calls.append(args)
            return _cp(args, 0, "created bd-xyz")
        if head == "show":
            return _cp(args, 1, "not found")
        return _cp(args, 0, "[]")

    monkeypatch.setattr(smrh, "_git", lambda repo, *a, check=False: _cp(a, 0, ""))
    monkeypatch.setattr(smrh, "_bd", fake_bd)

    rc = smrh.reconcile("/repo", "origin", 10.0, dry_run=False)
    assert rc == 0
    assert not bd_calls, bd_calls
    assert "in-active-revise" in capsys.readouterr().out


def test_no_bead_when_the_work_bead_was_reopened(monkeypatch):
    # `bd reopen` sets status back to 'open' (bd's documented semantics:
    # "Reopen closed issues by setting status to 'open'..."). This is a PURE
    # negative-control pin, not a code change: work_bead_qualifies already
    # excludes 'open' (real merge work is only closed/in_progress), so a
    # bead reopened for more work already correctly suppresses filing. Pinned
    # explicitly under SABLE-4709h's negative-control matrix so a future
    # loosening of work_bead_qualifies cannot silently regress it.
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="open")
    is_stranded, reason, status = _classify()
    assert is_stranded is False
    assert "no-qualifying-work-bead" in reason
    assert status == "open"


def test_no_bead_when_a_seat_hold_reason_is_recorded(monkeypatch):
    # SABLE-4709h HELD-UNDER-PULL / HELD-ON-INFRA: the pre-existing
    # SABLE-jejx3 hold mechanism already covers this population (see
    # test_held_branch_classifies_as_held_not_stranded above); pinned again
    # under the literal name SABLE-4709h's test spec calls for, so the
    # bead's own four-state discriminator matrix has one traceable test per
    # row.
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="closed")
    hold = smrh.hold_from_bead(_bead(
        hold="seat-side hold: verified in hand, not yet promoted",
        by="chuck", since=_iso_days_ago_wallclock(0.1),
        until="chuck promotes on the next batch"))
    is_stranded, reason, _ = smrh.classify_branch(
        "/repo", "origin", "wk-x", "trunk", [], 10.0, NOW, hold)
    assert is_stranded is False
    assert reason.startswith("HELD"), reason


# ===========================================================================
# SABLE-xw32f: QUEUED AT THE SEAT. A branch whose ci-verify preview is
# actively running satisfies the ordinary stranded predicate identically to
# abandoned work — under burst load, six branches legitimately queued for CI
# (already in chuck's hands) would have been re-filed as for-chuck handoffs
# as fast as he closed them. branch_queued_at_seat reuses the SAME shared
# idempotency ref attempt_preview_kick's own no-op check already keys on
# (classify.preview_kick_ref), so 'is this branch queued' and 'would kicking
# it be a no-op' resolve from one source of truth.
# ===========================================================================

def _fake_queued(monkeypatch, *, ref_exists=True, inflight=True, ref_age=601.0,
                 resolve_ok=True):
    """resolve_commit/preview_kick_ref/remote_ref_commit/ref_has_inflight_run
    are top-level names bound to sable-merge-gate's own seams at import time
    (see the module's import block) — monkeypatched here the same way
    kick_preview already is. `ref_age` is the PREVIEW REF's own age (via
    preview_ref_age_seconds) — deliberately NOT the branch's push age, which
    every classify_branch-level test in this file backdates to far in the
    past (CT_OLD) so it always clears predicate 4; conflating the two clocks
    is the exact bug this fixture is shaped to avoid reintroducing."""
    if not resolve_ok:
        def boom(*a, **k):
            raise RuntimeError("cannot resolve ref")
        monkeypatch.setattr(smrh, "resolve_commit", boom)
        return
    monkeypatch.setattr(smrh, "resolve_commit", lambda repo, ref: "deadbeef")
    monkeypatch.setattr(smrh, "preview_kick_ref",
                        lambda branch, base_sha, branch_sha: "ci-verify/wk-x-1234567")
    monkeypatch.setattr(smrh, "remote_ref_commit",
                        lambda repo, remote, ref: ("cafefeed" if ref_exists else None))
    monkeypatch.setattr(smrh, "preview_ref_age_seconds", lambda repo, sha, now: ref_age)
    monkeypatch.setattr(smrh, "ref_has_inflight_run", lambda repo, ref: inflight)


def test_branch_queued_at_seat_true_when_ref_exists_and_inflight(monkeypatch):
    _fake_queued(monkeypatch, ref_exists=True, inflight=True)
    queued, detail = smrh.branch_queued_at_seat("/repo", "origin", "wk-x", "trunk", NOW)
    assert queued is True
    assert "QUEUED" in detail


def test_branch_queued_at_seat_false_when_ref_present_but_terminal(monkeypatch):
    _fake_queued(monkeypatch, ref_exists=True, inflight=False)
    queued, detail = smrh.branch_queued_at_seat("/repo", "origin", "wk-x", "trunk", NOW)
    assert queued is False
    assert "terminal" in detail


def test_branch_queued_at_seat_false_when_no_preview_ref(monkeypatch):
    _fake_queued(monkeypatch, ref_exists=False)
    queued, detail = smrh.branch_queued_at_seat("/repo", "origin", "wk-x", "trunk", NOW)
    assert queued is False
    assert "no-preview-ref" in detail


def test_branch_queued_at_seat_false_on_unresolvable_sha(monkeypatch):
    _fake_queued(monkeypatch, resolve_ok=False)
    queued, detail = smrh.branch_queued_at_seat("/repo", "origin", "wk-x", "trunk", NOW)
    assert queued is False
    assert "unresolvable" in detail


def test_branch_queued_at_seat_stale_escalates(monkeypatch):
    # a preview in flight far past the stale bound must escalate rather than
    # suppress indefinitely -- mirrors hold_since/hold_until on SABLE-jejx3.
    _fake_queued(monkeypatch, ref_exists=True, inflight=True, ref_age=999999.0)
    queued, detail = smrh.branch_queued_at_seat(
        "/repo", "origin", "wk-x", "trunk", NOW, queued_stale_min=1.0)
    assert queued is False
    assert "queued-check-stale" in detail


def test_queued_branch_classifies_as_queued_not_stranded(monkeypatch):
    # THE test named in xw32f's spec.
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="closed")
    _fake_queued(monkeypatch, ref_exists=True, inflight=True)
    is_stranded, reason, _ = _classify()
    assert is_stranded is False, reason
    assert "QUEUED" in reason


def test_queued_branch_with_terminal_preview_is_still_stranded(monkeypatch):
    # Negative direction, same file (xw32f's spec): a TERMINAL preview and
    # still unmerged must still classify STRANDED -- the queued state cannot
    # swallow a real strand.
    _fake_git(monkeypatch, ancestor_rc=1, tip_ct=CT_OLD)
    _fake_bd(monkeypatch, search_status="closed")
    _fake_queued(monkeypatch, ref_exists=True, inflight=False)
    is_stranded, reason, status = _classify()
    assert is_stranded is True, reason
    assert reason == "STRANDED"
    assert status == "closed"
