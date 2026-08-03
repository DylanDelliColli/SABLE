#!/usr/bin/env python3
"""Unit tests for per-file activation-shape classification (SABLE-tvhzw)."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import sable_activation_lib as activation  # noqa: E402
import sable_activation_debt_lib as debt  # noqa: E402
import sable_gate_promote_lib as promote  # noqa: E402


@pytest.fixture
def layout(tmp_path):
    repo = tmp_path / "repo"
    live_bin = repo / "bin"
    live_bin.mkdir(parents=True)
    hooks = repo / "hooks" / "multi-manager"
    hooks.mkdir(parents=True)
    installed_bin = tmp_path / "home" / ".local" / "bin"
    installed_hooks = tmp_path / "home" / ".claude" / "hooks" / "multi-manager"
    installed_bin.mkdir(parents=True)
    installed_hooks.mkdir(parents=True)
    roots = debt.Roots(
        claude=str(tmp_path / "home" / ".claude"),
        local_bin=str(installed_bin),
    )
    return repo, roots


def make_source(repo: Path, rel: str, text: str = "new\n") -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if rel.startswith("bin/sable-"):
        path.chmod(0o755)
    return path


def test_symlink_into_worktree_classifies_pull_hot_swap(layout):
    repo, roots = layout
    source = make_source(repo, "bin/sable-live")
    (Path(roots.local_bin) / source.name).symlink_to(source)
    assert activation.classify_activation(
        "bin/sable-live", repo_root=repo, roots=roots
    ) is activation.Shape.PULL_HOT_SWAP


def test_symlink_outside_bindir_classifies_repin(layout, tmp_path):
    repo, roots = layout
    make_source(repo, "bin/sable-pinned")
    snapshot = tmp_path / "snapshots" / "sable-deadbeef" / "sable-pinned"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("old\n")
    (Path(roots.local_bin) / "sable-pinned").symlink_to(snapshot)
    shape = activation.classify_activation(
        "bin/sable-pinned", repo_root=repo, roots=roots
    )
    assert shape is activation.Shape.REPIN
    assert shape is not activation.Shape.PULL_HOT_SWAP


def test_regular_file_classifies_install_refresh(layout):
    """The SABLE-52aym case: a landed hook leaves a stale copied consumer."""
    repo, roots = layout
    make_source(repo, "hooks/multi-manager/pre-dispatch-overlap.sh")
    installed = Path(roots.claude) / "hooks/multi-manager/pre-dispatch-overlap.sh"
    installed.write_text("old\n")
    assert activation.classify_activation(
        "hooks/multi-manager/pre-dispatch-overlap.sh", repo_root=repo, roots=roots
    ) is activation.Shape.INSTALL_REFRESH


def test_absent_installed_path_classifies_merge_is_activation(tmp_path):
    source = tmp_path / "repo" / "docs" / "method.md"
    source.parent.mkdir(parents=True)
    source.write_text("method\n")
    assert activation.classify_activation(
        "docs/method.md", repo_root=tmp_path / "repo",
        installed_path=tmp_path / "absent"
    ) is activation.Shape.MERGE_IS_ACTIVATION


def test_new_bin_with_no_installed_path_classifies_not_installed(layout):
    repo, roots = layout
    make_source(repo, "bin/sable-new")
    assert activation.classify_activation(
        "bin/sable-new", repo_root=repo, roots=roots
    ) is activation.Shape.NOT_INSTALLED


def test_peer_control_tool_that_is_installed_still_classifies_normally(layout):
    repo, roots = layout
    source = make_source(repo, "bin/sable-peer")
    (Path(roots.local_bin) / source.name).symlink_to(source)
    assert activation.classify_activation(
        "bin/sable-peer", repo_root=repo, roots=roots
    ) is activation.Shape.PULL_HOT_SWAP


def test_one_branch_two_shapes_reports_both_per_file(layout):
    repo, roots = layout
    tool = make_source(repo, "bin/sable-live")
    hook_rel = "hooks/multi-manager/pre-dispatch-overlap.sh"
    make_source(repo, hook_rel)
    (Path(roots.local_bin) / tool.name).symlink_to(tool)
    (Path(roots.claude) / hook_rel).write_text("old\n")

    obligations = activation.activation_obligations(
        ["bin/sable-live", hook_rel], repo_root=repo, roots=roots
    )
    assert obligations == {
        activation.Shape.PULL_HOT_SWAP: ["bin/sable-live"],
        activation.Shape.INSTALL_REFRESH: [hook_rel],
    }


def test_stale_installed_detects_content_difference_and_matching_control(layout):
    repo, roots = layout
    rel = "hooks/multi-manager/pre-dispatch-overlap.sh"
    source = make_source(repo, rel, "landed\n")
    installed = Path(roots.claude) / rel
    installed.write_text("old\n")

    assert activation.stale_installed(
        [rel], "WORKTREE", repo_root=repo, roots=roots
    ) == [rel]
    installed.write_bytes(source.read_bytes())
    assert activation.stale_installed(
        [rel], "WORKTREE", repo_root=repo, roots=roots
    ) == []


def test_stale_installed_does_not_report_clean_when_installed_side_absent(layout):
    repo, roots = layout
    make_source(repo, "bin/sable-new")
    assert activation.stale_installed(
        ["bin/sable-new"], "WORKTREE", repo_root=repo, roots=roots
    ) == ["bin/sable-new"]


def test_wiring_check_ignores_hooks_test_suites_with_positive_control(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"hooks": {}}\n')
    findings = activation.unwired_hooks(
        ["hooks/test/test-demo.sh", "hooks/multi-manager/demo.sh"],
        settings_paths=[settings],
    )
    assert findings == ["hooks/multi-manager/demo.sh"]


def test_pending_set_highest_bar_and_declared_bar_refusal(layout):
    repo, roots = layout
    tool = make_source(repo, "bin/sable-live")
    hook = "hooks/multi-manager/demo.sh"
    make_source(repo, hook)
    (Path(roots.local_bin) / tool.name).symlink_to(tool)
    (Path(roots.claude) / hook).write_text("old\n")
    pending = activation.activation_obligations(
        [hook, "bin/sable-live"], repo_root=repo, roots=roots
    )
    assert activation.highest_bar(pending) is activation.Shape.PULL_HOT_SWAP
    with pytest.raises(activation.ActivationBarRefused):
        activation.assert_declared_bar(pending, activation.Shape.INSTALL_REFRESH)


def test_promote_activation_report_prints_and_appends_but_probe_error_is_nonblocking(
        tmp_path, monkeypatch, capsys):
    appended = []
    monkeypatch.setattr(activation, "changed_paths", lambda *_: ["docs/method.md"])
    monkeypatch.setattr(
        activation, "activation_report",
        lambda *_, **__: '{"highest_bar": "merge-is-activation"}',
    )
    monkeypatch.setattr(promote, "_append_evidence",
                        lambda repo, bead, note: appended.append((bead, note)))
    promote._report_activation(str(tmp_path), "SABLE-demo", "base", "tip")
    assert appended == [(
        "SABLE-demo",
        'activation-obligations: {"highest_bar": "merge-is-activation"}',
    )]
    assert "activation-obligations" in capsys.readouterr().out

    monkeypatch.setattr(
        activation, "changed_paths", lambda *_: (_ for _ in ()).throw(OSError("probe")))
    promote._report_activation(str(tmp_path), "SABLE-demo", "base", "tip")
    assert "could not be assessed: probe" in capsys.readouterr().err
