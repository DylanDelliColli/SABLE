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
