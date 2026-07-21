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
