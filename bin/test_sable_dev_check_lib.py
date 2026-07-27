#!/usr/bin/env python3
"""Tests for the proportional local feedback planner."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_dev_check_lib as devcheck  # noqa: E402

DEV_CHECK = Path(__file__).resolve().parent / "sable-dev-check"


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
            stderr="::notice::impact-manifest: SCOPED\n",
        )

    suites = devcheck.select_shell_suites(
        repo, ["bin/widget.py"], runner=fake_runner,
    )

    assert suites == ("test-a.sh", "test-z.sh")
    assert calls[0][0][-2:] == ["--select", "bin/widget.py"]
    assert "SCOPED" in capsys.readouterr().err


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
