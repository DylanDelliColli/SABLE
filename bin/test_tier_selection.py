#!/usr/bin/env python3
"""Unit tests for bin/tier_selection.py (SABLE-cmar4.3).

Pure logic + an injected `collector` seam standing in for the real
pytest-testmon / pytest-impact collect-only subprocess calls (those are
exercised for real, with no mocking, in test_tier_selection_integration.py).
"""
import signal
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tier_selection as ts  # noqa: E402


# --- parse_collect_only_nodeids ----------------------------------------------

def test_parse_collect_only_nodeids_extracts_ids():
    output = (
        "bin/test_x.py::test_a\n"
        "bin/test_x.py::test_b\n"
        "bin/sub/test_y.py::TestC::test_d\n"
    )
    assert ts.parse_collect_only_nodeids(output) == [
        "bin/test_x.py::test_a",
        "bin/test_x.py::test_b",
        "bin/sub/test_y.py::TestC::test_d",
    ]


def test_parse_collect_only_nodeids_ignores_summary_and_blank_lines():
    output = (
        "\n"
        "bin/test_x.py::test_a\n"
        "\n"
        "3 tests collected in 0.02s\n"
    )
    assert ts.parse_collect_only_nodeids(output) == ["bin/test_x.py::test_a"]


def test_parse_collect_only_nodeids_empty_output():
    assert ts.parse_collect_only_nodeids("") == []
    assert ts.parse_collect_only_nodeids("no tests ran in 0.00s\n") == []


# --- build_impact_tier_plan --------------------------------------------------

def test_cache_miss_falls_back_to_full_run(tmp_path):
    # No .testmondata present at all -- the definitive cache-miss case.
    assert not ts.testmondata_path(tmp_path).exists()

    def collector_should_not_be_called(repo_root, extra_args):
        raise AssertionError("collector must not run on a cache miss")

    plan = ts.build_impact_tier_plan(tmp_path, collector=collector_should_not_be_called)

    assert plan.mode == "full"
    assert plan.argv == ["bin/", "-q", "-p", "no:cacheprovider"]
    assert "cache miss" in plan.reason


def test_cache_hit_unions_testmon_and_impact_changed_file_selections(tmp_path):
    ts.testmondata_path(tmp_path).write_text("{}")

    def fake_collector(repo_root, extra_args):
        if "--testmon" in extra_args:
            return ts.CollectResult(ids=["bin/test_mod.py::test_touches_changed_file"], returncode=0)
        assert any(a.startswith("--impact") for a in extra_args)
        return ts.CollectResult(ids=["bin/test_conftest_dependent.py::test_uses_fixture"], returncode=0)

    plan = ts.build_impact_tier_plan(tmp_path, collector=fake_collector)

    assert plan.mode == "selected"
    assert plan.argv == [
        "bin/test_conftest_dependent.py::test_uses_fixture",
        "bin/test_mod.py::test_touches_changed_file",
        "-q",
    ]
    assert "testmon=1" in plan.reason
    assert "impact=1" in plan.reason


def test_cache_hit_overlapping_selections_deduplicate(tmp_path):
    ts.testmondata_path(tmp_path).write_text("{}")

    def fake_collector(repo_root, extra_args):
        return ts.CollectResult(ids=["bin/test_mod.py::test_shared"], returncode=0)

    plan = ts.build_impact_tier_plan(tmp_path, collector=fake_collector)

    assert plan.mode == "selected"
    assert plan.argv == ["bin/test_mod.py::test_shared", "-q"]


def test_cache_hit_both_selectors_empty_returns_none_mode(tmp_path):
    ts.testmondata_path(tmp_path).write_text("{}")

    def empty_collector(repo_root, extra_args):
        return ts.CollectResult(ids=[], returncode=0)

    plan = ts.build_impact_tier_plan(tmp_path, collector=empty_collector)

    assert plan.mode == "none"
    assert plan.argv == []


# --- collector failure must fall back to FULL, never "none" (SABLE-cmar4.3 revise) --

def test_collector_usage_error_falls_back_to_full_run(tmp_path):
    # A missing/incompatible plugin looks like a pytest usage error (exit 4):
    # empty stdout, same as a legitimately-empty selection. Must NOT be read
    # as "nothing impacted" -- that would run zero tests and report success.
    ts.testmondata_path(tmp_path).write_text("{}")

    def failing_collector(repo_root, extra_args):
        return ts.CollectResult(ids=[], returncode=4)

    plan = ts.build_impact_tier_plan(tmp_path, collector=failing_collector)

    assert plan.mode == "full"
    assert plan.argv == ["bin/", "-q", "-p", "no:cacheprovider"]
    assert "exit 4" in plan.reason


def test_collector_exit5_still_means_none(tmp_path):
    # Regression guard against overcorrecting: pytest's own "no tests
    # collected" code (5) is a LEGITIMATE empty selection, not a failure.
    ts.testmondata_path(tmp_path).write_text("{}")

    def empty_collector(repo_root, extra_args):
        return ts.CollectResult(ids=[], returncode=5)

    plan = ts.build_impact_tier_plan(tmp_path, collector=empty_collector)

    assert plan.mode == "none"
    assert plan.argv == []


# --- run_impact_tier ----------------------------------------------------------

def test_run_impact_tier_none_mode_skips_subprocess(tmp_path, monkeypatch):
    ts.testmondata_path(tmp_path).write_text("{}")
    monkeypatch.setattr(ts, "build_impact_tier_plan", lambda *a, **k: ts.ImpactTierPlan("none", [], "nothing"))

    calls = []
    monkeypatch.setattr(ts.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    rc = ts.run_impact_tier(tmp_path)

    assert rc == 0
    assert calls == []


def test_run_impact_tier_full_mode_invokes_pytest_with_full_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ts, "build_impact_tier_plan",
        lambda *a, **k: ts.ImpactTierPlan("full", ["bin/", "-q"], "cache miss"),
    )

    captured = {}

    class FakeCompletedProcess:
        returncode = 3

    def fake_run(argv, cwd):
        captured["argv"] = argv
        captured["cwd"] = cwd
        return FakeCompletedProcess()

    monkeypatch.setattr(ts.subprocess, "run", fake_run)

    rc = ts.run_impact_tier(tmp_path)

    assert rc == 3
    assert captured["argv"] == [sys.executable, "-m", "pytest", "bin/", "-q"]
    assert captured["cwd"] == tmp_path


# --- classify_cache_warm_outcome (SABLE-cmar4.3 second revise) ---------------

_TESTMON_CRASH_OUTPUT = (
    "231 passed, 19 skipped in 20.41s\n"
    "INTERNALERROR> Traceback (most recent call last):\n"
    "INTERNALERROR>   File \".../testmon/testmon_core.py\", line 93, in get_file\n"
    "INTERNALERROR>     ext=filename.rsplit(\".\", 1)[1],\n"
    "INTERNALERROR>         ~~~~~~~~~~~~~~~~~~~~~~~^^^\n"
    "INTERNALERROR> IndexError: list index out of range\n"
)


def test_classify_cache_warm_outcome_real_success_is_success():
    assert ts.classify_cache_warm_outcome(0, "231 passed in 5.00s\n") is True


def test_classify_cache_warm_outcome_tolerates_known_testmon_crash():
    assert ts.classify_cache_warm_outcome(3, _TESTMON_CRASH_OUTPUT) is True


def test_classify_cache_warm_outcome_rejects_crash_with_a_real_failure():
    output = _TESTMON_CRASH_OUTPUT.replace("231 passed, 19 skipped", "1 failed, 230 passed, 19 skipped")
    assert ts.classify_cache_warm_outcome(3, output) is False


def test_classify_cache_warm_outcome_rejects_crash_with_a_real_error():
    output = _TESTMON_CRASH_OUTPUT.replace("231 passed, 19 skipped", "1 error, 230 passed, 19 skipped")
    assert ts.classify_cache_warm_outcome(3, output) is False


def test_classify_cache_warm_outcome_rejects_unrelated_nonzero_exit():
    # A genuinely different crash (no testmon rsplit signature at all) must
    # never be masked -- only the exact known defect is tolerated.
    assert ts.classify_cache_warm_outcome(2, "collected 0 items / usage error\n") is False


def test_classify_cache_warm_outcome_rejects_partial_signature_match():
    # Missing the IndexError line specifically -- must not pattern-match on
    # testmon_core.py alone (too broad; could mask an unrelated testmon bug).
    output = "231 passed in 5.00s\nINTERNALERROR> some other testmon_core.py failure\n"
    assert ts.classify_cache_warm_outcome(1, output) is False


# --- run_cache_warm signal diagnostics (SABLE-rum46) -------------------------

def test_run_cache_warm_names_the_signal_when_the_child_is_killed(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        ts.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=-signal.SIGTERM,
            stdout="",
            stderr="",
        ),
    )

    rc = ts.run_cache_warm(tmp_path)
    captured = capsys.readouterr()

    assert rc == 128 + signal.SIGTERM
    assert "killed by signal SIGTERM (15)" in captured.err
    assert "test failure" not in captured.err.lower()


def test_run_cache_warm_does_not_relabel_an_ordinary_failure_as_a_signal(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        ts.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=3,
            stdout="ordinary pytest failure\n",
            stderr="",
        ),
    )

    rc = ts.run_cache_warm(tmp_path)
    captured = capsys.readouterr()

    assert rc == 3
    assert "signal" not in captured.err.lower()
    assert "ordinary pytest failure" in captured.out


# --- build_diff_cover_scope_plan (SABLE-hauwa) --------------------------------
# PURE decision layer on top of build_impact_tier_plan -- every test here
# injects an ImpactTierPlan directly, no subprocess, no filesystem beyond a
# tmp_path used only for the "file still exists" guarantee-2 pre-check.

def _selected_plan(*node_ids):
    return ts.ImpactTierPlan("selected", [*node_ids, "-q"], f"{len(node_ids)} impacted test(s)")


_FULL_PLAN = ts.ImpactTierPlan("full", ["bin/", "-q", "-p", "no:cacheprovider"], "testmon cache miss")
_NONE_PLAN = ts.ImpactTierPlan("none", [], "no impacted tests")


def test_scopes_when_selection_covers_every_diff_touched_test_file(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "test_foo.py").write_text("def test_x(): pass\n")

    plan = ts.build_diff_cover_scope_plan(
        _selected_plan("bin/test_foo.py::test_x"),
        diff_touched_files=["bin/test_foo.py"],
        repo_root=tmp_path,
    )

    assert plan.mode == "scoped"
    assert plan.test_paths == ["bin/test_foo.py"]


def test_scopes_and_unions_selection_with_diff_touched_files_not_individually_selected(tmp_path):
    # A test file the diff touches is always folded in even if the
    # impact-tier plan's own node ids happen to name a DIFFERENT file (e.g.
    # a fixture/conftest-mediated dependent) -- both are included.
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "test_foo.py").write_text("def test_x(): pass\n")

    plan = ts.build_diff_cover_scope_plan(
        _selected_plan("bin/test_foo.py::test_x", "bin/test_dependent.py::test_y"),
        diff_touched_files=["bin/test_foo.py"],
        repo_root=tmp_path,
    )

    assert plan.mode == "scoped"
    assert plan.test_paths == ["bin/test_dependent.py", "bin/test_foo.py"]


def test_full_when_impact_tier_plan_is_already_full(tmp_path):
    plan = ts.build_diff_cover_scope_plan(_FULL_PLAN, diff_touched_files=[], repo_root=tmp_path)

    assert plan.mode == "full"
    assert plan.test_paths == []
    assert "testmon cache miss" in plan.reason


def test_full_when_diff_touched_files_is_none_git_diff_failed(tmp_path):
    plan = ts.build_diff_cover_scope_plan(
        _selected_plan("bin/test_foo.py::test_x"), diff_touched_files=None, repo_root=tmp_path)

    assert plan.mode == "full"
    assert "could not determine diff-touched files" in plan.reason


def test_full_when_a_diff_touched_test_file_no_longer_exists(tmp_path):
    # Deleted (or renamed away) -- cannot be re-collected, so the guarantee
    # is unsatisfiable regardless of what the collector says. This must be
    # checked BEFORE consulting tier_plan at all.
    (tmp_path / "bin").mkdir()

    plan = ts.build_diff_cover_scope_plan(
        _selected_plan("bin/test_foo.py::test_x"),
        diff_touched_files=["bin/test_foo.py"],
        repo_root=tmp_path,
    )

    assert plan.mode == "full"
    assert "no longer exists at HEAD" in plan.reason


def test_negative_control_fails_closed_when_selection_omits_a_diff_touched_test_file(tmp_path):
    # THE NEGATIVE CONTROL (SABLE-hauwa, non-negotiable): the diff touches
    # bin/test_bar.py, but the impact-tier plan's selection only names a
    # DIFFERENT file. Coverage on test_bar.py's changed lines would come
    # from a suite this selection never runs -- a false miss waiting to
    # happen. Must fail closed to full, not silently proceed scoped.
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "test_bar.py").write_text("def test_y(): pass\n")

    plan = ts.build_diff_cover_scope_plan(
        _selected_plan("bin/test_foo.py::test_x"),
        diff_touched_files=["bin/test_bar.py"],
        repo_root=tmp_path,
    )

    assert plan.mode == "full"
    assert plan.test_paths == []
    assert "omits diff-touched test file(s)" in plan.reason
    assert "bin/test_bar.py" in plan.reason


def test_full_when_impact_tier_plan_mode_is_none_and_diff_touches_a_test_file(tmp_path):
    # "none" means the impact-tier plan itself selected nothing; a diff
    # that touches a test file can never be proven covered by an empty
    # selection.
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "test_foo.py").write_text("def test_x(): pass\n")

    plan = ts.build_diff_cover_scope_plan(
        _NONE_PLAN, diff_touched_files=["bin/test_foo.py"], repo_root=tmp_path)

    assert plan.mode == "full"
    assert "omits diff-touched test file(s)" in plan.reason


def test_full_when_impact_tier_plan_mode_is_none_and_diff_touches_no_test_file():
    # Defensive: this function is never called outside the pruning-diff
    # context in production (a pruning diff always touches >=1 test file),
    # but must still fail closed rather than silently treat "nothing
    # selected, nothing to check" as vacuously safe.
    plan = ts.build_diff_cover_scope_plan(_NONE_PLAN, diff_touched_files=[], repo_root=Path("/nonexistent"))

    assert plan.mode == "full"
    assert "selected no tests" in plan.reason


def test_line_1400_shape_added_inline_skip_call_keeps_its_file_in_scope(tmp_path):
    # be4lo.7/21rug.4 shape: a `pytest.skip(...)` call ADDED inside an
    # existing test body. The line is only "covered" by the test file being
    # collected and run far enough to hit it -- since the file containing
    # it is directly touched by the diff, guarantee 2 must keep it in scope
    # regardless of what the impact-tier plan's own selection says.
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "test_rehearsal.py").write_text(
        "import pytest\n"
        "def test_slow_rehearsal():\n"
        "    if True:\n"
        "        pytest.skip('opt-in only')\n"
    )

    plan = ts.build_diff_cover_scope_plan(
        _selected_plan("bin/test_rehearsal.py::test_slow_rehearsal"),
        diff_touched_files=["bin/test_rehearsal.py"],
        repo_root=tmp_path,
    )

    assert plan.mode == "scoped"
    assert "bin/test_rehearsal.py" in plan.test_paths


# --- _git_diff_touched_files (SABLE-hauwa) ------------------------------------

def test_git_diff_touched_files_returns_none_on_git_failure(tmp_path):
    # Not a git repo at all -- git itself fails, must be None not [].
    assert ts._git_diff_touched_files(tmp_path, "HEAD") is None


# --- print_diff_cover_scope CLI contract (SABLE-hauwa) ------------------------

def test_print_diff_cover_scope_never_raises_and_prints_full_on_crash(tmp_path, monkeypatch, capsys):
    def boom(repo_root, compare_ref, collector=None):
        raise RuntimeError("synthetic crash")

    monkeypatch.setattr(ts, "run_diff_cover_scope", boom)

    rc = ts.print_diff_cover_scope(tmp_path, "HEAD")

    assert rc == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "full"


def test_print_diff_cover_scope_prints_scoped_mode_and_paths(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        ts, "run_diff_cover_scope",
        lambda repo_root, compare_ref, collector=None: ts.DiffCoverScopePlan(
            "scoped", ["bin/test_a.py", "bin/test_b.py"], "2 impact-scoped test file(s)"),
    )

    rc = ts.print_diff_cover_scope(tmp_path, "HEAD")

    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["scoped", "bin/test_a.py", "bin/test_b.py"]


# --- invariant: --testmon only ever reaches a --collect-only run -------------
# (SABLE-jd5fj.19)
#
# THE INVARIANT: every pytest invocation this module builds that carries a
# pytest-testmon flag (--testmon / --testmon-noselect) also carries
# --collect-only -- with exactly ONE deliberate exception, run_cache_warm,
# whose whole job is to take an executing --testmon-noselect run in order to
# rebuild the coverage map.
#
# It is NOT "--testmon is never passed". build_impact_tier_plan does pass
# --testmon; it passes it to a --collect-only collector. Stating the invariant
# the blanket way is the exact error this bead exists to correct (it was the
# claim in sable_gate_promote_lib.py's warm-testmon comment), so the checks
# below carry a control in BOTH polarities: a violating call must be caught,
# and a legitimate --collect-only --testmon call must NOT be.
#
# WHY IT IS LOAD-BEARING: pytest-testmon 2.2.0 crashes on extensionless files
# (testmon_core.py:93 rsplit(".", 1)[1] -> IndexError) from
# pytest_runtest_logreport -- a per-test-EXECUTION hook. Under --collect-only
# no test executes, so the crash site is unreachable. This repo's bin/ carries
# dozens of extensionless python executables, so a maintainer who "restores"
# --testmon to a genuine executing call reintroduces the crash into the merge
# seat's local impact tier. Two independent checks below:
#   A. STATIC (_pytest_argv_literals): reads tier_selection.py's own source, so
#      it also covers argv literals no test happens to execute.
#   B. RUNTIME (_captured_pytest_argvs): every argv the real code path actually
#      hands to subprocess.run, including ones assembled dynamically.

import ast  # noqa: E402
import subprocess  # noqa: E402

_TIER_SELECTION_SOURCE = Path(__file__).resolve().parent / "tier_selection.py"

# The ONE function allowed to hand a testmon flag to an executing pytest run.
# Asserted as an exact set below: growing it silently is itself a failure.
_EXECUTING_TESTMON_ALLOWLIST = {"run_cache_warm"}


def _is_testmon_flag(arg):
    return arg.split("=", 1)[0].startswith("--testmon")


def _hands_testmon_to_an_executing_run(argv):
    """The invariant, as a predicate: True when `argv` would give pytest-testmon
    a run that actually EXECUTES tests (and therefore reaches the crash site).
    False both for a legitimate --collect-only --testmon call and for a call
    that never mentions testmon at all."""
    return any(_is_testmon_flag(a) for a in argv) and "--collect-only" not in argv


def _pytest_argv_literals(source):
    """[(enclosing function name, [string literals in the argv list]), ...] for
    every `subprocess.run([...])` in `source` whose argv literal invokes pytest.

    Non-literal elements (*extra_args, f-strings) are simply absent from the
    returned list -- this check only ever reasons about flags it can SEE, which
    is why the runtime check below exists alongside it."""
    tree = ast.parse(source)
    found = []
    stack = []

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_Call(self, node):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "run"
                    and isinstance(func.value, ast.Name) and func.value.id == "subprocess"
                    and node.args and isinstance(node.args[0], ast.List)):
                literals = [e.value for e in node.args[0].elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if "pytest" in literals:
                    found.append((stack[-1] if stack else "<module>", literals))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return found


def test_static_no_executing_pytest_call_passes_a_testmon_flag():
    invocations = _pytest_argv_literals(_TIER_SELECTION_SOURCE.read_text())

    # Vacuity guard: if the scanner stops seeing tier_selection's pytest calls
    # (renamed import, argv built some other way), it would pass by finding
    # nothing at all.
    assert len(invocations) >= 2, f"scanner found too few pytest invocations: {invocations}"

    offenders = {fn for fn, argv in invocations if _hands_testmon_to_an_executing_run(argv)}
    assert offenders == _EXECUTING_TESTMON_ALLOWLIST, (
        f"functions handing a testmon flag to an EXECUTING pytest run: {offenders}; "
        f"the only sanctioned one is {_EXECUTING_TESTMON_ALLOWLIST} (the deliberate "
        f"cache-warm, guarded by classify_cache_warm_outcome). See SABLE-jd5fj.19."
    )


def test_static_collector_is_the_thing_that_makes_testmon_safe():
    # The positive half of the same fact: the collector every --testmon call
    # goes through really does carry --collect-only. If this ever stops being
    # true the check above flips to RED, so pin it explicitly here rather than
    # leaving the reason implicit.
    by_fn = dict(_pytest_argv_literals(_TIER_SELECTION_SOURCE.read_text()))
    assert "--collect-only" in by_fn["_pytest_collect_only"]


def test_negative_control_static_check_catches_a_planted_executing_testmon_call():
    # Exactly the change this bead exists to prevent: a maintainer who believes
    # "the tier never passes --testmon" adds a genuine, non-collect-only one.
    planted = (
        "import subprocess\n"
        "def run_impact_tier(repo_root, plan):\n"
        "    subprocess.run(['python', '-m', 'pytest', 'bin/', '-q', '--testmon'])\n"
    )
    offenders = {fn for fn, argv in _pytest_argv_literals(planted)
                 if _hands_testmon_to_an_executing_run(argv)}
    assert offenders == {"run_impact_tier"}


def test_negative_control_predicate_discriminates_rather_than_banning_testmon():
    # BOTH polarities. A check that merely banned --testmon would pass the two
    # asserts above while restating the bead's own error in a new place.
    assert _hands_testmon_to_an_executing_run(["bin/", "-q", "--testmon"])
    assert _hands_testmon_to_an_executing_run(["bin/", "-q", "--testmon-noselect"])
    # ...and must NOT flag the legitimate call the tier actually makes:
    assert not _hands_testmon_to_an_executing_run(
        ["bin/", "--collect-only", "-q", "--testmon"])
    assert not _hands_testmon_to_an_executing_run(["bin/", "-q", "-p", "no:cacheprovider"])


def _captured_pytest_argvs(monkeypatch, collect_only_stdout):
    """Record every argv tier_selection hands to subprocess.run, standing in
    for the real pytest process. Returns the (growing) list of argvs."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        stdout = collect_only_stdout if "--collect-only" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(ts.subprocess, "run", fake_run)
    return calls


def test_runtime_selected_mode_never_executes_a_testmon_run(tmp_path, monkeypatch):
    ts.testmondata_path(tmp_path).write_text("{}")
    calls = _captured_pytest_argvs(monkeypatch, "bin/test_x.py::test_a\n")

    rc = ts.run_impact_tier(tmp_path)

    assert rc == 0
    # Vacuity guard: the tier must STILL be passing --testmon somewhere, or
    # this test proves nothing about an invariant governing --testmon.
    assert any(any(_is_testmon_flag(a) for a in argv) for argv in calls), \
        f"the tier no longer passes --testmon at all: {calls}"
    # And the executing run must have happened, or "no violation" is trivial.
    assert any("--collect-only" not in argv for argv in calls), \
        f"no executing pytest run was made: {calls}"
    assert [argv for argv in calls if _hands_testmon_to_an_executing_run(argv)] == []


def test_runtime_full_mode_never_executes_a_testmon_run(tmp_path, monkeypatch):
    # Cache-miss path: no .testmondata, so the tier skips the collector
    # entirely and goes straight to the executing full run.
    calls = _captured_pytest_argvs(monkeypatch, "")

    rc = ts.run_impact_tier(tmp_path)

    assert rc == 0
    assert calls and all(not _hands_testmon_to_an_executing_run(argv) for argv in calls)


def test_runtime_diff_cover_scope_never_executes_a_testmon_run(tmp_path, monkeypatch):
    ts.testmondata_path(tmp_path).write_text("{}")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "test_x.py").write_text("def test_a(): pass\n")
    monkeypatch.setattr(ts, "_git_diff_touched_files",
                        lambda repo_root, compare_ref: ["bin/test_x.py"])
    calls = _captured_pytest_argvs(monkeypatch, "bin/test_x.py::test_a\n")

    plan = ts.run_diff_cover_scope(tmp_path, "HEAD")

    assert plan.mode == "scoped"
    assert any(any(_is_testmon_flag(a) for a in argv) for argv in calls), \
        f"the diff-cover path no longer passes --testmon at all: {calls}"
    assert [argv for argv in calls if _hands_testmon_to_an_executing_run(argv)] == []


def test_cli_help_is_metadata_only_and_returns_immediately():
    result = subprocess.run(
        [sys.executable, str(_TIER_SELECTION_SOURCE), "--help"],
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert "tier_selection: full" not in result.stderr


def test_negative_control_runtime_capture_catches_a_collector_missing_collect_only(
        tmp_path, monkeypatch):
    # Plant the same maintainer mistake at RUNTIME rather than in source text:
    # a collector that drops --collect-only. The capture harness must report a
    # violation, otherwise the three runtime asserts above are green-by-
    # construction and could never have failed.
    ts.testmondata_path(tmp_path).write_text("{}")
    calls = _captured_pytest_argvs(monkeypatch, "bin/test_x.py::test_a\n")

    def collector_without_collect_only(repo_root, extra_args):
        result = ts.subprocess.run(
            [sys.executable, "-m", "pytest", "bin/", "-q", *extra_args],
            cwd=repo_root, capture_output=True, text=True, check=False)
        return ts.CollectResult(ids=ts.parse_collect_only_nodeids(result.stdout),
                                returncode=result.returncode)

    ts.build_impact_tier_plan(tmp_path, collector=collector_without_collect_only)

    violations = [argv for argv in calls if _hands_testmon_to_an_executing_run(argv)]
    assert len(violations) == 1
    assert "--testmon" in violations[0] and "--collect-only" not in violations[0]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
