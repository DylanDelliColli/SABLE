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
