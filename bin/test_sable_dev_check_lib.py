#!/usr/bin/env python3
"""Tests for the proportional local feedback planner."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from textwrap import dedent

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_dev_check_lib as devcheck  # noqa: E402

DEV_CHECK = Path(__file__).resolve().parent / "sable-dev-check"
REPO_ROOT = Path(__file__).resolve().parents[1]
_CLI_LOADER = SourceFileLoader("sable_dev_check_cli", str(DEV_CHECK))
_CLI_SPEC = importlib.util.spec_from_loader("sable_dev_check_cli", _CLI_LOADER)
sable_dev_check_cli = importlib.util.module_from_spec(_CLI_SPEC)
_CLI_LOADER.exec_module(sable_dev_check_cli)


def _write(repo: Path, relative: str, content: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content))


def _python_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for relative, content in files.items():
        _write(repo, relative, content)
    return repo


def test_direct_import_selects_only_dependent_test(tmp_path):
    repo = _python_repo(
        tmp_path,
        {
            "bin/widget.py": "def value(): return 1\n",
            "bin/other.py": "def value(): return 2\n",
            "bin/test_widget.py": """
                from widget import value
                def test_value(): assert value() == 1
            """,
            "bin/test_other.py": """
                from other import value
                def test_value(): assert value() == 2
            """,
        },
    )

    selection = devcheck.select_python_tests(repo, ["bin/widget.py"])

    assert selection.mode == "selected"
    assert selection.tests == ("bin/test_widget.py",)


def test_transitive_import_selects_test_at_end_of_dependency_chain(tmp_path):
    repo = _python_repo(
        tmp_path,
        {
            "bin/core.py": "VALUE = 1\n",
            "bin/facade.py": "from core import VALUE\n",
            "bin/test_facade.py": """
                from facade import VALUE
                def test_value(): assert VALUE == 1
            """,
        },
    )

    selection = devcheck.select_python_tests(repo, ["bin/core.py"])

    assert selection.tests == ("bin/test_facade.py",)


def test_changed_test_selects_itself(tmp_path):
    repo = _python_repo(
        tmp_path,
        {"bin/test_widget.py": "def test_value(): assert True\n"},
    )

    selection = devcheck.select_python_tests(repo, ["bin/test_widget.py"])

    assert selection.tests == ("bin/test_widget.py",)


def test_conftest_change_selects_every_test_below_it(tmp_path):
    repo = _python_repo(
        tmp_path,
        {
            "bin/conftest.py": "VALUE = 1\n",
            "bin/test_a.py": "def test_a(): assert True\n",
            "bin/nested/test_b.py": "def test_b(): assert True\n",
        },
    )

    selection = devcheck.select_python_tests(repo, ["bin/conftest.py"])

    assert selection.mode == "full"
    assert selection.tests == ("bin/nested/test_b.py", "bin/test_a.py")


def test_literal_loader_reference_selects_test_for_extensionless_tool(tmp_path):
    repo = _python_repo(
        tmp_path,
        {
            "bin/sable-widget": "#!/usr/bin/env python3\nVALUE = 1\n",
            "bin/test_widget.py": """
                from pathlib import Path
                TOOL = Path(__file__).parent / "sable-widget"
                def test_tool_path(): assert TOOL.name == "sable-widget"
            """,
        },
    )

    selection = devcheck.select_python_tests(repo, ["bin/sable-widget"])

    assert selection.tests == ("bin/test_widget.py",)


def test_unmapped_bin_change_falls_back_to_all_python_tests(tmp_path):
    repo = _python_repo(
        tmp_path,
        {
            "bin/orphan.py": "VALUE = 1\n",
            "bin/test_a.py": "def test_a(): assert True\n",
            "bin/test_b.py": "def test_b(): assert True\n",
        },
    )

    selection = devcheck.select_python_tests(repo, ["bin/orphan.py"])

    assert selection.mode == "full"
    assert selection.tests == ("bin/test_a.py", "bin/test_b.py")
    assert "unmapped" in selection.reason


def test_implicit_full_fallback_budget_comes_from_full_snapshot_ssot(tmp_path):
    """The motivating shape is selected Python plus a FULL shell fallback.

    The long duration must be read from test-tiers.sh, never copied into the
    Python implementation as another copy of today's production duration.
    """
    _write(tmp_path, ".github/ci/test-tiers.sh", "# fake tier source\n")
    plan = devcheck.DeveloperPlan(
        changed_paths=("hooks/new-unmapped.sh",),
        python=devcheck.PythonSelection(
            "selected", ("bin/test_a.py",), "1 of 2 Python test files reached",
        ),
        shell_suites=("test-a.sh", "test-b.sh"),
        shell_mode="full",
        shell_reason="unmapped path(s): hooks/new-unmapped.sh",
    )
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="347\n", stderr="")

    decision = devcheck.effective_budget(
        tmp_path, plan, explicit_seconds=None, runner=fake_runner,
    )

    assert decision.seconds == 347
    assert decision.mode == "full-fallback"
    assert "Shell FULL" in decision.reason
    assert len(calls) == 1
    assert calls[0][0][-2:] == ["--budget", "full_snapshot"]


def test_implicit_scoped_budget_stays_fast_without_reading_full_snapshot(tmp_path):
    plan = devcheck.DeveloperPlan(
        changed_paths=("bin/widget.py",),
        python=devcheck.PythonSelection(
            "selected", ("bin/test_widget.py",), "1 of 112 Python test files reached",
        ),
        shell_suites=("test-widget.sh",),
        shell_mode="scoped",
        shell_reason="1 suite from 1 mapped path",
    )

    def forbidden_runner(*_args, **_kwargs):
        pytest.fail("a scoped plan must not query the full_snapshot budget")

    decision = devcheck.effective_budget(
        tmp_path, plan, explicit_seconds=None, runner=forbidden_runner,
    )

    assert decision.seconds == 90
    assert decision.mode == "scoped"
    assert "90" not in decision.reason  # duration and explanation stay separate


def test_python_full_alone_uses_the_full_snapshot_budget(tmp_path):
    _write(tmp_path, ".github/ci/test-tiers.sh", "# fake tier source\n")
    plan = devcheck.DeveloperPlan(
        changed_paths=("bin/unmapped_tool",),
        python=devcheck.PythonSelection(
            "full", ("bin/test_a.py", "bin/test_b.py"), "unmapped Python tool",
        ),
        shell_suites=("test-a.sh",),
        shell_mode="scoped",
        shell_reason="1 suite from 1 mapped path",
    )
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="411\n", stderr="")

    decision = devcheck.effective_budget(
        tmp_path, plan, explicit_seconds=None, runner=fake_runner,
    )

    assert decision.seconds == 411
    assert decision.mode == "full-fallback"
    assert "Python FULL" in decision.reason
    assert "Shell FULL" not in decision.reason
    assert len(calls) == 1


def test_explicit_budget_wins_even_for_a_full_fallback(tmp_path):
    plan = devcheck.DeveloperPlan(
        changed_paths=("hooks/new-unmapped.sh",),
        python=devcheck.PythonSelection("full", ("bin/test_a.py",), "unmapped"),
        shell_suites=("test-a.sh",),
        shell_mode="full",
        shell_reason="unmapped",
    )

    def forbidden_runner(*_args, **_kwargs):
        pytest.fail("an explicit --budget must not consult an implicit tier budget")

    decision = devcheck.effective_budget(
        tmp_path, plan, explicit_seconds=37, runner=forbidden_runner,
    )

    assert decision.seconds == 37
    assert decision.mode == "explicit"
    assert "--budget" in decision.reason


def test_cli_dry_run_reports_selection_modes_reasons_and_effective_budget(
    tmp_path, monkeypatch, capsys,
):
    """Exercise selector -> budget -> rendering without launching a test."""
    _write(
        tmp_path,
        ".github/ci/impact-manifest.sh",
        """
        #!/usr/bin/env bash
        echo test-a.sh
        echo "::notice::impact-manifest: FULL -- unmapped path(s): $2" >&2
        """,
    )
    _write(
        tmp_path,
        ".github/ci/test-tiers.sh",
        """
        #!/usr/bin/env bash
        [ "$1" = --budget ] && [ "$2" = full_snapshot ] && echo 347
        """,
    )
    _write(tmp_path, "bin/test_a.py", "def test_a(): assert True\n")
    monkeypatch.setattr(sable_dev_check_cli, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        sable_dev_check_cli.devcheck,
        "run_plan",
        lambda *_args, **_kwargs: pytest.fail("--dry-run must not execute tests"),
    )

    rc = sable_dev_check_cli.main(
        ["--dry-run", "--path", "hooks/new-unmapped.sh"],
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "Python: none — no Python dependency reached" in out
    assert "Shell: full — unmapped path(s): hooks/new-unmapped.sh" in out
    assert "hooks/test/test-a.sh" in out
    assert "Budget: full-fallback — 347s" in out


def test_cli_execution_hands_the_effective_full_budget_to_run_plan(
    tmp_path, monkeypatch, capsys,
):
    _write(
        tmp_path,
        ".github/ci/test-tiers.sh",
        """
        #!/usr/bin/env bash
        [ "$1" = --budget ] && [ "$2" = full_snapshot ] && echo 347
        """,
    )
    plan = devcheck.DeveloperPlan(
        changed_paths=("hooks/new-unmapped.sh",),
        python=devcheck.PythonSelection(
            "selected", ("bin/test_a.py",), "1 of 112 Python test files reached",
        ),
        shell_suites=("test-a.sh",),
        shell_mode="full",
        shell_reason="unmapped path(s): hooks/new-unmapped.sh",
    )
    observed = {}
    monkeypatch.setattr(sable_dev_check_cli, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        sable_dev_check_cli.devcheck, "build_plan", lambda _repo, _changed: plan,
    )

    def fake_run_plan(repo, actual_plan, *, budget_seconds):
        observed.update(
            repo=repo,
            plan=actual_plan,
            budget_seconds=budget_seconds,
        )
        return 17

    monkeypatch.setattr(
        sable_dev_check_cli.devcheck, "run_plan", fake_run_plan,
    )

    rc = sable_dev_check_cli.main(["--path", "hooks/new-unmapped.sh"])

    assert rc == 17
    assert observed == {
        "repo": tmp_path,
        "plan": plan,
        "budget_seconds": 347,
    }
    assert "Budget: full-fallback — 347s" in capsys.readouterr().out


def test_repo_hook_timeouts_cover_authoritative_full_fallback_budget():
    """Each outer clock covers the inner clock plus its own named headroom.

    The phase-four margin covers planning and teardown outside run_plan's
    internal timer. The lifecycle margin covers fetch/rebase and the other
    hook phases outside phase four.
    """
    tier_budget = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / ".github/ci/test-tiers.sh"),
            "--budget",
            "full_snapshot",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    full_snapshot_seconds = int(tier_budget.stdout.strip())

    sable = {}
    for raw_line in (REPO_ROOT / ".sable").read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"invalid .sable line: {raw_line!r}"
        sable[key] = value

    phase_four_planning_headroom_seconds = 30
    assert "--budget" not in sable["testCommand"]
    phase_four_timeout_seconds = int(sable["testTimeout"])
    assert phase_four_timeout_seconds == (
        full_snapshot_seconds + phase_four_planning_headroom_seconds
    )

    settings = json.loads(
        (REPO_ROOT / "templates/multi-manager/settings-snippet.json").read_text()
    )
    pre_push_hooks = [
        hook
        for group in settings["hooks"]["PreToolUse"]
        for hook in group["hooks"]
        if hook["command"].endswith("/pre-push-rebase-test.sh")
    ]
    assert len(pre_push_hooks) == 1
    lifecycle_non_test_headroom_seconds = 30
    assert pre_push_hooks[0]["timeout"] >= 1000 * (
        phase_four_timeout_seconds + lifecycle_non_test_headroom_seconds
    )


def test_unrelated_documentation_change_selects_no_python_tests(tmp_path):
    repo = _python_repo(
        tmp_path,
        {
            "README.md": "before\n",
            "bin/test_a.py": "def test_a(): assert True\n",
        },
    )

    selection = devcheck.select_python_tests(repo, ["README.md"])

    assert selection.mode == "none"
    assert selection.tests == ()


def test_hidden_path_normalisation_preserves_leading_dot():
    assert devcheck._normalise_path(".github/ci/impact-manifest.sh") == (
        ".github/ci/impact-manifest.sh"
    )


def test_unparseable_python_graph_falls_back_to_all_tests(tmp_path):
    repo = _python_repo(
        tmp_path,
        {
            "bin/broken.py": "this is not valid python !!!\n",
            "bin/test_a.py": "def test_a(): assert True\n",
        },
    )

    selection = devcheck.select_python_tests(repo, ["bin/broken.py"])

    assert selection.mode == "full"
    assert "cannot parse" in selection.reason


def test_collect_changed_paths_unions_commits_worktree_and_untracked(tmp_path):
    repo = _python_repo(tmp_path, {"tracked.txt": "base\n"})
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "base"], check=True)
    base = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()

    _write(repo, "committed.txt", "committed\n")
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "change"], check=True)
    _write(repo, "tracked.txt", "worktree\n")
    _write(repo, "untracked.txt", "new\n")

    assert devcheck.collect_changed_paths(repo, base) == (
        "committed.txt",
        "tracked.txt",
        "untracked.txt",
    )


def test_shell_selection_delegates_to_manifest_and_preserves_reason(tmp_path, capsys):
    repo = _python_repo(tmp_path, {".github/ci/impact-manifest.sh": "exit 0\n"})
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="test-z.sh\ntest-a.sh\n",
            stderr=(
                "::notice::impact-manifest: SCOPED -- "
                "2 suites from 1 mapped path\n"
            ),
        )

    selection = devcheck.select_shell_selection(
        repo, ["bin/widget.py"], runner=fake_runner,
    )

    assert selection.suites == ("test-a.sh", "test-z.sh")
    assert selection.mode == "scoped"
    assert selection.reason == "2 suites from 1 mapped path"
    assert calls[0][0][-2:] == ["--select", "bin/widget.py"]
    assert "SCOPED" in capsys.readouterr().err


def test_shell_selection_without_a_mode_line_fails_closed_to_full(tmp_path):
    repo = _python_repo(
        tmp_path,
        {
            "bin/widget.py": "VALUE = 1\n",
            "bin/test_widget.py": "import widget\n",
        },
    )
    _write(repo, ".github/ci/impact-manifest.sh", "# fixture\n")

    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="test-widget.sh\n",
            stderr="selector completed without a classification line\n",
        )

    selection = devcheck.select_shell_selection(
        repo, ["bin/widget.py"], runner=fake_runner,
    )

    assert selection.suites == ("test-widget.sh",)
    assert selection.mode == "full"
    assert "omitted" in selection.reason


def test_shell_selector_failure_is_reported_as_environment_error(tmp_path):
    repo = _python_repo(tmp_path, {".github/ci/impact-manifest.sh": "exit 1\n"})

    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="bad manifest")

    with pytest.raises(devcheck.DeveloperCheckError, match="bad manifest"):
        devcheck.select_shell_suites(repo, ["README.md"], runner=fake_runner)


def test_preflight_reports_missing_selected_suite_tool_before_execution(
    tmp_path, monkeypatch,
):
    repo = _python_repo(
        tmp_path,
        {"hooks/test/test-needs-coverage.sh": "diff-cover coverage.xml\n"},
    )
    plan = devcheck.DeveloperPlan(
        changed_paths=("coverage.py",),
        python=devcheck.PythonSelection("none", (), "none"),
        shell_suites=("test-needs-coverage.sh",),
    )
    real_which = devcheck.shutil.which
    monkeypatch.setattr(
        devcheck.shutil,
        "which",
        lambda command: None if command == "diff-cover" else real_which(command),
    )

    with pytest.raises(devcheck.DeveloperCheckError, match="diff-cover"):
        devcheck.run_plan(repo, plan)


def test_run_plan_uses_one_pytest_process_then_selected_shell_suites(
    tmp_path, monkeypatch,
):
    repo = _python_repo(
        tmp_path,
        {
            "bin/test_a.py": "def test_a(): assert True\n",
            "bin/test_b.py": "def test_b(): assert True\n",
            "hooks/test/test-shell.sh": "exit 0\n",
        },
    )
    plan = devcheck.DeveloperPlan(
        changed_paths=("bin/a.py",),
        python=devcheck.PythonSelection(
            "selected", ("bin/test_a.py", "bin/test_b.py"), "two",
        ),
        shell_suites=("test-shell.sh",),
    )
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    rc = devcheck.run_plan(repo, plan, runner=fake_runner)

    assert rc == 0
    assert len(calls) == 2
    assert calls[0][0][:3] == [sys.executable, "-m", "pytest"]
    assert calls[0][0].count("-m") == 1
    assert calls[1][0][-1].endswith("hooks/test/test-shell.sh")


def test_run_plan_continues_independent_suites_after_failure(tmp_path):
    repo = _python_repo(
        tmp_path,
        {
            "hooks/test/test-first.sh": "exit 1\n",
            "hooks/test/test-second.sh": "exit 0\n",
        },
    )
    plan = devcheck.DeveloperPlan(
        changed_paths=("hooks/tool.sh",),
        python=devcheck.PythonSelection("none", (), "none"),
        shell_suites=("test-first.sh", "test-second.sh"),
    )
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if len(calls) == 1 else 0)

    assert devcheck.run_plan(repo, plan, runner=fake_runner) == 1
    assert [Path(argv[-1]).name for argv in calls] == [
        "test-first.sh",
        "test-second.sh",
    ]


def test_execute_plan_rendering_summarises_large_fallback():
    suites = tuple(f"test-{index}.sh" for index in range(21))
    plan = devcheck.DeveloperPlan(
        changed_paths=("unknown",),
        python=devcheck.PythonSelection("none", (), "none"),
        shell_suites=suites,
    )

    assert "list omitted" in devcheck.render_plan(plan)
    assert "test-20.sh" in devcheck.render_plan(plan, max_entries=None)


def test_cli_help_does_not_inspect_repository_or_run_tests():
    result = subprocess.run(
        [sys.executable, str(DEV_CHECK), "--help"],
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert result.returncode == 0
    assert "sealed-candidate" in result.stdout
