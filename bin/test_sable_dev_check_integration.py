#!/usr/bin/env python3
"""End-to-end proof that a derived budget reaches a VERDICT (SABLE-y4nom.4).

Real composition throughout: a real git repo on the real filesystem, the real
`bin/sable-dev-check` entry point in a real subprocess, running real pytest and
real bash suites. Nothing here is mocked — the defect being fixed only ever
appeared in composition, so a mocked run could not have caught it.

THE MOTIVATING MEASUREMENT (SABLE-1urtf): a three-file change to
bin/sable-orchestration-install mapped to a plan of Python files plus two shell
suites, emitted 69 passing dots, and then died at
`sable-dev-check: command exceeded remaining 90.0s budget`, exit 124 — before
the shell suites ran at all. Nothing was verified. Not a failure, a NON-ANSWER.

ON LOAD SENSITIVITY (SABLE-af7u4 measured a wall-clock cap that FAILED at
10.04s and PASSED at 10.16s on identical code — a threshold that close is a
load detector, not a discriminator). So no assertion here is a bare wall-clock
threshold. Every margin is arranged so that machine load can only push the
measurement FURTHER in the direction already asserted:

  * the timeout controls run work that SLEEPS past the bound — load makes a
    sleep longer, never shorter, so a control that times out keeps timing out;
  * the passing cases declare measured costs ~20x their real runtime, so the
    derived budgets are enormous relative to the work and no plausible load
    brings them near the bound.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_dev_check_lib as devcheck  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_CHECK = REPO_ROOT / "bin" / "sable-dev-check"
PRE_PUSH_HOOK = REPO_ROOT / "hooks" / "multi-manager" / "pre-push-rebase-test.sh"

# Each selected Python file sleeps this long. Five files plus interpreter and
# pytest startup put the real run comfortably past the 1s control bound.
SLEEP_SECONDS = 0.5
PYTHON_SUITES = 5

# What the cost reports CLAIM each suite costs. Deliberately ~20x the real
# runtime: the derived budgets under test then sit far above anything load can
# produce, so a green result here means the derivation worked, not that the
# machine happened to be idle.
DECLARED_COST = 30.0

CHANGED = (
    "bin/sable-orchestration-install",
    "bin/orchestration_lib.py",
    "bin/orchestration_extra.py",
)


def _write(repo: Path, relative: str, content: str, *, executable: bool = False) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content))
    if executable:
        path.chmod(0o755)


@pytest.fixture(scope="module")
def install_repo(tmp_path_factory) -> Path:
    """SABLE-1urtf's shape: a three-file change mapping to Python + 2 suites."""
    repo = tmp_path_factory.mktemp("orchestration-install")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    # The changed extensionless tool, plus the two libraries beside it.
    _write(repo, "bin/sable-orchestration-install", "#!/usr/bin/env python3\n")
    _write(repo, "bin/orchestration_lib.py", "def install(): return 'ok'\n")
    _write(repo, "bin/orchestration_extra.py", "def extra(): return 'ok'\n")

    # Python suites that reach the changed paths: one names the extensionless
    # tool by literal (the loader-reference edge), the rest import the libs.
    _write(
        repo,
        "bin/test_install_tool.py",
        f"""
        import time
        from pathlib import Path
        TOOL = Path(__file__).parent / "sable-orchestration-install"
        def test_tool_present():
            time.sleep({SLEEP_SECONDS})
            assert TOOL.name == "sable-orchestration-install"
        """,
    )
    for index in range(PYTHON_SUITES - 1):
        module = "orchestration_lib" if index % 2 == 0 else "orchestration_extra"
        symbol = "install" if index % 2 == 0 else "extra"
        _write(
            repo,
            f"bin/test_install_{index}.py",
            f"""
            import time
            from {module} import {symbol}
            def test_{symbol}_{index}():
                time.sleep({SLEEP_SECONDS})
                assert {symbol}() == 'ok'
            """,
        )

    # The two shell suites, and a manifest that maps the change to them.
    for name in ("test-install-a.sh", "test-install-b.sh"):
        _write(repo, f"hooks/test/{name}", "#!/usr/bin/env bash\nexit 0\n", executable=True)
    _write(
        repo,
        ".github/ci/impact-manifest.sh",
        """
        #!/usr/bin/env bash
        echo test-install-a.sh
        echo test-install-b.sh
        echo "::notice::impact-manifest: SCOPED -- 2 suites from 3 mapped paths" >&2
        """,
        executable=True,
    )

    # The tier SSOT. full_snapshot is the absolute bound; pre_push survives
    # only as a per-command minimum grant.
    _write(
        repo,
        ".github/ci/test-tiers.sh",
        """
        #!/usr/bin/env bash
        [ "$1" = --budget ] && [ "$2" = full_snapshot ] && echo 600
        [ "$1" = --budget ] && [ "$2" = pre_push ] && echo 60
        exit 0
        """,
        executable=True,
    )

    # The measurements, in the exact formats the two EXISTING reporters emit:
    # conftest.py --sable-test-cost-report and shell-run-set.sh --profile.
    modules = ["bin/test_install_tool.py"] + [
        f"bin/test_install_{index}.py" for index in range(PYTHON_SUITES - 1)
    ]
    # Written at the implementation's OWN default paths, read from the module
    # rather than retyped — a default that moves must move this fixture with
    # it, not silently stop being found.
    _write(
        repo,
        devcheck.PYTHON_COST_REPORT,
        json.dumps({
            "modules": [
                {"module": module, "seconds": DECLARED_COST, "declared_heavy": False}
                for module in modules
            ],
            "tests": [],
            "violations": [],
        }),
    )
    _write(
        repo,
        devcheck.SHELL_COST_PROFILE,
        "suite\tstatus\tseconds\n"
        f"test-install-a.sh\tpass\t{DECLARED_COST:.6f}\n"
        f"test-install-b.sh\tpass\t{DECLARED_COST:.6f}\n",
    )
    return repo


def _dev_check(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DEV_CHECK), *[arg for path in CHANGED
                                           for arg in ("--path", path)], *extra],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=300,
    )


# --- the fix: a mapped change reaches a verdict ---------------------------


def test_the_sable_1urtf_shape_reaches_a_verdict_instead_of_exit_124(install_repo):
    """The whole bead in one assertion: this run ANSWERS.

    Exit 124 is not a failing verdict, it is the absence of one. Pass or fail
    are both acceptable outcomes here; "no answer" is not.
    """
    result = _dev_check(install_repo)

    assert result.returncode != 124, (
        f"the derived budget still produced a non-answer\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert result.returncode in (0, 1)
    assert "Budget: derived" in result.stdout


def test_a_fitting_mapped_plan_leaves_nothing_unverified(install_repo):
    """A green, correctly-mapped selection runs in FULL — no silent narrowing."""
    result = _dev_check(install_repo)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "UNVERIFIED" not in result.stdout
    assert "UNVERIFIED" not in result.stderr
    # Every selected suite is named in the executed plan.
    for suite in ("test-install-a.sh", "test-install-b.sh"):
        assert suite in result.stdout


def test_the_derived_budget_is_not_the_scoped_tier_value(install_repo):
    """60s is this fixture's pre_push tier value; it must not be the ceiling."""
    result = _dev_check(install_repo, "--dry-run")

    assert result.returncode == 0
    assert "Budget: derived" in result.stdout
    # Derived from 7 suites x 30s declared x 2.0 headroom = 420s, which only a
    # derivation can produce — neither tier value is 420.
    assert "420" in result.stdout


def test_the_report_separates_the_total_bound_from_the_summed_grants(install_repo):
    """SABLE-8jln9: the two numbers were conflated, so both are now printed.

    600s is the fixture's full_snapshot tier — the absolute total this run may
    spend. 420s is the sum of the per-command grants. A reader who cannot tell
    them apart cannot tell whether the gate is bounded at all, which is how the
    summed value was handed to the executor unnoticed.
    """
    result = _dev_check(install_repo, "--dry-run")

    assert "Budget: derived — 600s" in result.stdout
    assert "Total bound: 600s absolute" in result.stdout
    assert "headroom sums to 420s" in result.stdout


# --- NEGATIVE CONTROL: the same input still dies without the derivation ----


def test_negative_control_same_input_exits_124_without_a_derived_budget(install_repo):
    """Proves these tests exercise the NEW path.

    Same repo, same changed paths, same suites, same flat bound — only the
    derivation is switched off. Without it the run is cut off mid-flight and
    returns SABLE-1urtf's exit 124 with no verdict.

    Load-safe by construction: the work sleeps ~2.5s against a 1s bound, and
    load can only make a sleep longer.

    BOTH POLARITIES AT THE SAME BOUND. The control and the treatment below
    differ in exactly one flag — not in the budget, the repo, the changed
    paths, or the suites — so the discriminator is provably the derivation
    itself and not a more generous number.
    """
    control = _dev_check(install_repo, "--no-derived-budget", "--budget", "1")
    treatment = _dev_check(install_repo, "--budget", "1")

    assert control.returncode == 124, (
        f"expected the pre-fix non-answer, got {control.returncode}\n"
        f"stdout:\n{control.stdout}\nstderr:\n{control.stderr}"
    )
    assert "Budget: derived" not in control.stdout

    assert treatment.returncode != 124, (
        f"the derivation must answer at the same bound the control died at\n"
        f"stdout:\n{treatment.stdout}\nstderr:\n{treatment.stderr}"
    )
    assert "Budget: derived" in treatment.stdout
    # It answers by TRIMMING, and it names what it gave up to do so.
    assert "UNVERIFIED" in treatment.stdout


def test_under_the_same_tight_bound_derivation_trims_and_still_answers(install_repo):
    """The contract when a plan genuinely cannot fit: TRIM, REPORT, ANSWER.

    Identical bound to the control above's regime, derivation ON. The plan no
    longer overruns; it drops what will not fit, names every dropped suite, and
    returns a real verdict.
    """
    result = _dev_check(install_repo, "--budget", "100")

    assert result.returncode != 124
    assert result.returncode in (0, 1)
    assert "UNVERIFIED" in result.stdout
    # Dropped suites are named, not merely counted.
    combined = result.stdout + result.stderr
    assert any(
        name in combined
        for name in ("bin/test_install_tool.py", "test-install-a.sh")
    )


def test_a_trim_is_reported_at_execution_time_not_only_while_planning(install_repo):
    result = _dev_check(install_repo, "--budget", "100")

    assert "UNVERIFIED" in result.stderr


# --- the total bound, in real composition (SABLE-8jln9) --------------------


@pytest.fixture(scope="module")
def partial_measurement_repo(tmp_path_factory) -> Path:
    """The install shape with the shell profile DELETED.

    This is the state this very checkout was in when the defect was found: a
    Python cost report exists, no shell profile does. Every shell suite is
    therefore unmeasured, costed provisionally, and — correctly — exempt from
    trimming, because a trim decided by invented numbers is the silent
    narrowing this whole change forbids. What must NOT follow is an unbounded
    run: without a total, each command keeps its own generous grant and the
    plan stops only when the outer wrapper kills the gate outright.
    """
    repo = tmp_path_factory.mktemp("partial-measurement")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    _write(repo, "bin/sable-orchestration-install", "#!/usr/bin/env python3\n")
    _write(repo, "bin/orchestration_lib.py", "def install(): return 'ok'\n")
    _write(repo, "bin/orchestration_extra.py", "def extra(): return 'ok'\n")
    _write(
        repo,
        "bin/test_install_tool.py",
        f"""
        import time
        from pathlib import Path
        TOOL = Path(__file__).parent / "sable-orchestration-install"
        def test_tool_present():
            time.sleep({SLEEP_SECONDS})
            assert TOOL.name == "sable-orchestration-install"
        """,
    )
    for index in range(PYTHON_SUITES - 1):
        module = "orchestration_lib" if index % 2 == 0 else "orchestration_extra"
        symbol = "install" if index % 2 == 0 else "extra"
        _write(
            repo,
            f"bin/test_install_{index}.py",
            f"""
            import time
            from {module} import {symbol}
            def test_{symbol}_{index}():
                time.sleep({SLEEP_SECONDS})
                assert {symbol}() == 'ok'
            """,
        )
    for name in ("test-install-a.sh", "test-install-b.sh"):
        _write(repo, f"hooks/test/{name}", "#!/usr/bin/env bash\nexit 0\n",
               executable=True)
    _write(
        repo,
        ".github/ci/impact-manifest.sh",
        """
        #!/usr/bin/env bash
        echo test-install-a.sh
        echo test-install-b.sh
        echo "::notice::impact-manifest: SCOPED -- 2 suites from 3 mapped paths" >&2
        """,
        executable=True,
    )
    _write(
        repo,
        ".github/ci/test-tiers.sh",
        """
        #!/usr/bin/env bash
        [ "$1" = --budget ] && [ "$2" = full_snapshot ] && echo 600
        [ "$1" = --budget ] && [ "$2" = pre_push ] && echo 60
        exit 0
        """,
        executable=True,
    )
    # Python measurements only. No SHELL_COST_PROFILE is written — that absence
    # IS the condition under test.
    modules = ["bin/test_install_tool.py"] + [
        f"bin/test_install_{index}.py" for index in range(PYTHON_SUITES - 1)
    ]
    _write(
        repo,
        devcheck.PYTHON_COST_REPORT,
        json.dumps({
            "modules": [
                {"module": module, "seconds": DECLARED_COST, "declared_heavy": False}
                for module in modules
            ],
            "tests": [],
            "violations": [],
        }),
    )
    assert not (repo / devcheck.SHELL_COST_PROFILE).exists()
    return repo


def test_provisional_grants_do_not_survive_the_total_bound(partial_measurement_repo):
    """Every command is granted 60s; the total is 2s; the run must stop at 2s.

    LOAD-SAFE BY CONSTRUCTION, and this is the whole point of the shape. The
    selected Python files sleep 2.5s in aggregate against a 2s total, so the
    total is exceeded on an idle machine and exceeded by MORE under load — the
    measurement can only move further in the direction asserted. No per-command
    grant is anywhere near binding (60s each), so a 124 here can only come from
    the total.

    WITHOUT THE FIX this run returns 0: each command runs under its own 60s
    grant, nothing tracks the total, and roughly 5s of work sails past a 2s
    bound that was never enforced.
    """
    result = _dev_check(partial_measurement_repo, "--budget", "2")

    assert result.returncode == 124, (
        f"the total bound did not stop a run that overran it\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # It answers with a REPORT, not merely a non-zero exit: what did not get
    # verified is named, which is the half of the contract exit 124 alone fails.
    assert "UNVERIFIED:" in result.stderr
    assert "Do NOT narrow" in result.stderr
    assert any(
        suite in result.stderr.split("UNVERIFIED:", 1)[1]
        for suite in ("test-install-a.sh", "test-install-b.sh",
                      "bin/test_install_tool.py")
    )


def test_negative_control_the_same_plan_completes_when_the_total_is_ample(
    partial_measurement_repo,
):
    """One number differs from the test above: the total. Nothing else.

    Same repo, same missing shell profile, same provisional costs, same
    per-command grants. With room in the total the run reaches a verdict and
    leaves nothing unverified — so the 124 above is provably the total binding
    and not the fixture being broken, and the total is provably not capping
    runs that fit.
    """
    result = _dev_check(partial_measurement_repo, "--budget", "600")

    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "UNVERIFIED" not in result.stderr
    for suite in ("test-install-a.sh", "test-install-b.sh"):
        assert suite in result.stdout


# --- recorded-command leg -------------------------------------------------


def _run_pre_push(repo: Path, env_pairs: dict[str, str]) -> str:
    """Drive the real pre-push gate exactly as Claude Code's hook layer does."""
    payload = json.dumps({
        "tool_input": {"command": "git push"},
        "cwd": str(repo),
    })
    env = {"PATH": __import__("os").environ["PATH"], **env_pairs}
    result = subprocess.run(
        ["bash", str(PRE_PUSH_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return result.stdout


@pytest.fixture(scope="module")
def gate_repo(tmp_path_factory) -> Path:
    """A minimal repo whose pre-push gate reaches phase 4 with a real command."""
    root = tmp_path_factory.mktemp("gate")
    bare = root / "origin.git"
    repo = root / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    (repo / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "push", "-q", str(bare), "HEAD:refs/heads/main"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(bare)], check=True,
    )
    subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)
    (repo / ".sable").write_text("testCommand=true\n")
    return repo


def _gate_env(repo: Path, **extra: str) -> dict[str, str]:
    return {
        "CLAUDE_AGENT_NAME": "optimus",
        "CLAUDE_AGENT_ROLE": "manager",
        "SABLE_BASE_BRANCH": "origin/main",
        "SABLE_PRE_PUSH_TYPECHECK_COMMAND": "true",
        **extra,
    }


def test_the_gate_records_the_command_it_actually_executed(gate_repo):
    """A report of what was enforced is not evidence of what was enforced."""
    out = _run_pre_push(gate_repo, _gate_env(gate_repo))

    assert "enforced test command (executed): `true`" in out


def test_the_record_names_the_executed_command_over_a_disagreeing_intent(gate_repo):
    """SABLE-b99hy, both directions: intent never certifies anything.

    A worker rewrote sable.testCommand to a two-file subset, pushed green, and
    restored it — so the configured claim and the enforced claim diverged with
    nothing recording which one actually ran.
    """
    out = _run_pre_push(
        gate_repo,
        _gate_env(gate_repo, SABLE_TEST_COMMAND_INTENT="pytest bin/ -q"),
    )

    assert "enforced test command (executed): `true`" in out
    assert "DIVERGENCE: prior intent was `pytest bin/ -q`" in out
    # The intent must never be presented as what was enforced.
    assert "enforced test command (executed): `pytest bin/ -q`" not in out


def test_an_agreeing_intent_does_not_manufacture_a_divergence(gate_repo):
    """Negative control for the divergence leg."""
    out = _run_pre_push(gate_repo, _gate_env(gate_repo, SABLE_TEST_COMMAND_INTENT="true"))

    assert "enforced test command (executed): `true`" in out
    assert "DIVERGENCE" not in out


def test_the_gate_never_advises_narrowing_the_test_command():
    """The remediation that produced SABLE-b99hy must not come back."""
    gate = PRE_PUSH_HOOK.read_text()

    assert "scope the test command to a faster subset" not in gate
    assert "Do NOT narrow" in gate
