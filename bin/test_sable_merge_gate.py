#!/usr/bin/env python3
"""Unit tests for bin/sable-merge-gate (SABLE-o9aa).

The merge-preview ci-verify gate: build a merge-preview commit, gate it on an
Actions run keyed to the preview SHA, and PROMOTE the byte-identical object on
green (never re-merge). These are the UNIT tests — pure logic + subprocess seams
injected via the SABLE_MG_* env vars / monkeypatched _git. Real composition
against a scratch remote (the three o9aa rehearsals) lives in the integration
variant.
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


# --- preview_ref_name --------------------------------------------------------

def test_preview_ref_name_basic():
    assert smg.preview_ref_name("market-brief-package-nev0", "abcdef1234") == "ci-verify/market-brief-package-nev0-abcdef1"


def test_preview_ref_name_sanitizes_slash():
    # a stray slash in a bead id must not create a nested ref the flat sweep misses
    assert "/" not in smg.preview_ref_name("weird/bead", "abcdef1234").split("ci-verify/", 1)[1]


def test_preview_ref_name_rejects_short_sha():
    with pytest.raises(ValueError):
        smg.preview_ref_name("bead", "abc")


def test_preview_ref_name_rejects_empty_bead():
    with pytest.raises(ValueError):
        smg.preview_ref_name("///", "abcdef1234")


# --- is_orphan ---------------------------------------------------------------

@pytest.mark.parametrize("age_s,hours,expected", [
    (3600 * 5, 6, False),      # 5h < 6h -> keep
    (3600 * 7, 6, True),       # 7h > 6h -> sweep
    (3600 * 6, 6, False),      # exactly at threshold -> keep (strict >)
])
def test_is_orphan(age_s, hours, expected):
    assert smg.is_orphan(age_s, hours) is expected


# --- qualify_remote_ref: caution-1 regression guard --------------------------
# A stray local branch named `origin` makes bare `origin/<branch>` ambiguous and
# broke a real rev-parse. The gate must ONLY resolve fully-qualified refs.

def test_qualify_remote_ref_basic():
    assert smg.qualify_remote_ref("origin", "tmux-only") == "refs/remotes/origin/tmux-only"


def test_qualify_remote_ref_refuses_already_qualified_branch():
    with pytest.raises(ValueError):
        smg.qualify_remote_ref("origin", "origin/tmux-only")


def test_qualify_remote_ref_refuses_refs_prefix():
    with pytest.raises(ValueError):
        smg.qualify_remote_ref("origin", "refs/heads/tmux-only")


def test_qualify_remote_ref_refuses_slashed_remote():
    with pytest.raises(ValueError):
        smg.qualify_remote_ref("origin/x", "tmux-only")


# --- build_preview: conflict -> exit 22 --------------------------------------

def _fake_git_factory(monkeypatch, *, merge_tree_rc, merge_tree_out="TREEOID\n", commit_out="PREVIEWSHA\n"):
    def fake_git(repo, *args, check=True):
        if args and args[0] == "merge-tree":
            return subprocess.CompletedProcess(args, merge_tree_rc, stdout=merge_tree_out, stderr="")
        if args and args[0] == "commit-tree":
            return subprocess.CompletedProcess(args, 0, stdout=commit_out, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(smg, "_git", fake_git)


def test_build_preview_conflict_raises_22(monkeypatch):
    _fake_git_factory(monkeypatch, merge_tree_rc=1)
    with pytest.raises(smg.GateError) as ei:
        smg.build_preview("/repo", "BASESHA", "BRANCHSHA", "msg")
    assert ei.value.code == 22


def test_build_preview_clean_returns_commit(monkeypatch):
    _fake_git_factory(monkeypatch, merge_tree_rc=0)
    assert smg.build_preview("/repo", "BASESHA", "BRANCHSHA", "msg") == "PREVIEWSHA"


# --- wait_for_ci: actions-down when no run ever appears ----------------------

def test_wait_for_ci_actions_down(monkeypatch):
    # gh returns an empty run list; with grace=0 the first poll reports actions_down
    def fake_run(argv, cwd=None, check=True):
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
    monkeypatch.setattr(smg, "_run", fake_run)
    monkeypatch.setenv("SABLE_MG_GRACE", "0")
    monkeypatch.setenv("SABLE_MG_POLL", "0")
    monkeypatch.setenv("SABLE_MG_TIMEOUT", "0")
    conclusion, url = smg.wait_for_ci("/repo", "ci-verify/bead-abcdef1", "PREVIEWSHA")
    assert conclusion == "actions_down"


def test_wait_for_ci_success(monkeypatch):
    import json
    payload = json.dumps([{"headSha": "PREVIEWSHA", "status": "completed",
                           "conclusion": "success", "url": "http://run/1"}])

    def fake_run(argv, cwd=None, check=True):
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")
    monkeypatch.setattr(smg, "_run", fake_run)
    monkeypatch.setenv("SABLE_MG_POLL", "0")
    conclusion, url = smg.wait_for_ci("/repo", "ci-verify/bead-abcdef1", "PREVIEWSHA")
    assert conclusion == "success"
    assert url == "http://run/1"


# --- F1: tip moved during gate -> non-ff promote -> retryable exit 23 ---------
# post-flip this is a COMMON case (serial merges advance the tip during the CI
# wait), so the non-ff promote push must map to a clean retryable exit, never an
# uncaught CalledProcessError traceback, and cleanup must still run.

def test_promote_tip_moved_non_ff_is_retryable_23(monkeypatch):
    seen = []

    def fake_git(repo, *args, check=True):
        seen.append(args)
        head = args[0] if args else ""
        if head == "merge-tree":
            return subprocess.CompletedProcess(args, 0, stdout="TREEOID\n", stderr="")
        if head == "commit-tree":
            return subprocess.CompletedProcess(args, 0, stdout="PREVIEWSHA\n", stderr="")
        if head == "rev-parse":
            return subprocess.CompletedProcess(args, 0, stdout="SOMESHA\n", stderr="")
        if head == "push" and len(args) >= 3 and args[2].endswith(":refs/heads/trunk"):
            # the promote push: simulate the base having advanced (non-ff reject)
            return subprocess.CompletedProcess(args, 1, stdout="! [rejected] (non-fast-forward)", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(smg, "_git", fake_git)
    monkeypatch.setattr(smg, "wait_for_ci", lambda *a, **k: ("success", "http://run/1"))
    monkeypatch.setattr(smg, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(smg, "_append_evidence", lambda *a, **k: None)

    with pytest.raises(smg.GateError) as ei:
        smg.promote("BEAD", "wk-x", "trunk", "/repo", "origin", "mgr", None)
    assert ei.value.code == 23
    # cleanup still ran despite the non-ff exit: a push --delete of the ci-verify ref
    assert any("--delete" in a for a in seen), "ci-verify ref not cleaned up on tip-moved exit"


# --- SABLE-dn7r: post-merge worktree/branch cleanup (GREEN path only) ----------
# At fleet pace worker worktrees + branches re-accumulate (58 in one day) unless
# a green promote reaps them. These drive cleanup_after_merge as a unit via a
# recording fake _git; the destructive-call ORDER is load-bearing (worktree must
# come off before the branch, or git refuses to delete a checked-out branch).

def _cleanup_fake_git(monkeypatch, *, has_worktree=True, dirty=False, branch_exists=True,
                      worktree_remove_rc=0, branch_d_rc=0, cherry_out="", cherry_rc=0,
                      branch_big_d_rc=0, push_delete_rc=0):
    calls = []

    def fake_git(repo, *args, check=True):
        calls.append(args)
        head = args[0] if args else ""
        if head == "worktree" and len(args) >= 2 and args[1] == "list":
            block = ("worktree /main\nHEAD aaa\nbranch refs/heads/other\n\n")
            if has_worktree:
                block += "worktree /wt/wk-x\nHEAD bbb\nbranch refs/heads/wk-x\n"
            return subprocess.CompletedProcess(args, 0, stdout=block, stderr="")
        if head == "status":
            return subprocess.CompletedProcess(args, 0, stdout=("M f.py\n" if dirty else ""), stderr="")
        if head == "worktree" and len(args) >= 2 and args[1] == "remove":
            return subprocess.CompletedProcess(args, worktree_remove_rc, stdout="", stderr="")
        if head == "show-ref":
            return subprocess.CompletedProcess(args, 0 if branch_exists else 1, stdout="", stderr="")
        if head == "branch" and len(args) >= 2 and args[1] == "-d":
            return subprocess.CompletedProcess(args, branch_d_rc,
                                               stdout=("not fully merged" if branch_d_rc else ""), stderr="")
        if head == "cherry":
            return subprocess.CompletedProcess(args, cherry_rc, stdout=cherry_out, stderr="")
        if head == "branch" and len(args) >= 2 and args[1] == "-D":
            return subprocess.CompletedProcess(args, branch_big_d_rc, stdout="", stderr="")
        if head == "push":
            return subprocess.CompletedProcess(args, push_delete_rc, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(smg, "_git", fake_git)
    return calls


def _destructive(calls):
    """The gate-mutating verbs, in call order, ignoring read-only probes."""
    out = []
    for a in calls:
        if a[:2] == ("worktree", "remove"):
            out.append("worktree-remove")
        elif a[:2] == ("branch", "-d"):
            out.append("branch-d")
        elif a[:2] == ("branch", "-D"):
            out.append("branch-D")
        elif a and a[0] == "push" and "--delete" in a:
            out.append("push-delete")
    return out


def test_cleanup_removes_worktree_and_branches(monkeypatch):
    # happy path: registered clean worktree, branch -d succeeds -> worktree
    # removed, local branch deleted, remote branch deleted, in that order.
    calls = _cleanup_fake_git(monkeypatch)
    smg.cleanup_after_merge("/repo", "origin", "refs/remotes/origin/trunk", "wk-x")
    assert _destructive(calls) == ["worktree-remove", "branch-d", "push-delete"]


def test_cleanup_refuses_dirty_worktree(monkeypatch, capsys):
    # a dirty worktree aborts the WHOLE cleanup: zero destructive calls, warning,
    # and (in promote) exit stays 0 — uncommitted work is never destroyed.
    calls = _cleanup_fake_git(monkeypatch, dirty=True)
    smg.cleanup_after_merge("/repo", "origin", "refs/remotes/origin/trunk", "wk-x")
    assert _destructive(calls) == []
    assert "DIRTY" in capsys.readouterr().err


def test_cleanup_refuses_unmerged_branch(monkeypatch):
    # branch -d fails AND git cherry shows a genuinely absent commit ('+') -> no
    # -D escalation, and neither the local nor the remote branch is deleted.
    calls = _cleanup_fake_git(monkeypatch, branch_d_rc=1, cherry_out="+ deadbeef\n")
    smg.cleanup_after_merge("/repo", "origin", "refs/remotes/origin/trunk", "wk-x")
    assert _destructive(calls) == ["worktree-remove", "branch-d"]


def test_cleanup_patch_equivalent_branch_deleted(monkeypatch):
    # branch -d fails ancestry but every unique commit is patch-equivalent ('-')
    # -> guarded -D proceeds, then the remote is deleted (wk-git-autopush-hunt).
    calls = _cleanup_fake_git(monkeypatch, branch_d_rc=1, cherry_out="- cca59c2\n")
    smg.cleanup_after_merge("/repo", "origin", "refs/remotes/origin/trunk", "wk-x")
    # the refused -d is a real attempt before the guarded -D escalation
    assert _destructive(calls) == ["worktree-remove", "branch-d", "branch-D", "push-delete"]


def test_cleanup_missing_worktree_is_noop(monkeypatch):
    # no registered worktree -> skip (a) entirely, still delete both branches.
    calls = _cleanup_fake_git(monkeypatch, has_worktree=False)
    smg.cleanup_after_merge("/repo", "origin", "refs/remotes/origin/trunk", "wk-x")
    assert _destructive(calls) == ["branch-d", "push-delete"]


# --- SABLE-dtp1: resolve_integration_branch / resolve_base --------------------
# The pre-push hook resolves a repo's integration branch via git config >
# .sable file > env > "main" (hooks/multi-manager/lib-identity.sh's
# sable_resolve_integration_branch). promote() must agree, instead of
# defaulting to the literal 'llm-integration'.

def _fake_git_config(monkeypatch, *, config_val=None, config_rc=1):
    """Fake _git that answers `config --get sable.integrationBranch` and is a
    no-op for anything else (resolve_integration_branch only calls config)."""
    def fake_git(repo, *args, check=True):
        if args[:2] == ("config", "--get"):
            rc = 0 if config_val is not None else config_rc
            return subprocess.CompletedProcess(args, rc, stdout=(config_val or "") + ("\n" if config_val else ""), stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(smg, "_git", fake_git)


def test_resolve_integration_branch_git_config_wins(monkeypatch, tmp_path):
    _fake_git_config(monkeypatch, config_val="release-line")
    (tmp_path / ".sable").write_text("integrationBranch=sable-file-branch\n")
    assert smg.resolve_integration_branch(str(tmp_path)) == "release-line"


def test_resolve_integration_branch_sable_file_when_no_git_config(monkeypatch, tmp_path):
    _fake_git_config(monkeypatch, config_val=None)
    (tmp_path / ".sable").write_text("integrationBranch=sable-file-branch\n")
    assert smg.resolve_integration_branch(str(tmp_path)) == "sable-file-branch"


def test_resolve_integration_branch_env_when_no_config_or_file(monkeypatch, tmp_path):
    _fake_git_config(monkeypatch, config_val=None)
    monkeypatch.setenv("SABLE_INTEGRATION_BRANCH", "env-branch")
    assert smg.resolve_integration_branch(str(tmp_path)) == "env-branch"


def test_resolve_integration_branch_strips_origin_prefix_from_base_branch_env(monkeypatch, tmp_path):
    _fake_git_config(monkeypatch, config_val=None)
    monkeypatch.delenv("SABLE_INTEGRATION_BRANCH", raising=False)
    monkeypatch.setenv("SABLE_BASE_BRANCH", "origin/legacy-main")
    assert smg.resolve_integration_branch(str(tmp_path)) == "legacy-main"


def test_resolve_integration_branch_defaults_to_main(monkeypatch, tmp_path):
    _fake_git_config(monkeypatch, config_val=None)
    monkeypatch.delenv("SABLE_INTEGRATION_BRANCH", raising=False)
    monkeypatch.delenv("SABLE_BASE_BRANCH", raising=False)
    assert smg.resolve_integration_branch(str(tmp_path)) == "main"


def test_resolve_base_explicit_flag_wins_over_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("SABLE_MG_BASE", "env-base")
    monkeypatch.setattr(smg, "resolve_integration_branch", lambda repo: "resolved-base")
    assert smg.resolve_base("flag-base", str(tmp_path)) == "flag-base"


def test_resolve_base_env_wins_when_flag_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("SABLE_MG_BASE", "env-base")
    monkeypatch.setattr(smg, "resolve_integration_branch", lambda repo: "resolved-base")
    assert smg.resolve_base(None, str(tmp_path)) == "env-base"


def test_resolve_base_falls_back_to_resolved_integration_branch(monkeypatch, tmp_path):
    monkeypatch.delenv("SABLE_MG_BASE", raising=False)
    monkeypatch.setattr(smg, "resolve_integration_branch", lambda repo: "resolved-base")
    assert smg.resolve_base(None, str(tmp_path)) == "resolved-base"

