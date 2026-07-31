#!/usr/bin/env python3
"""Tests for deriving execution budgets from measured suite cost (SABLE-y4nom.4).

The contract under test: a budget is DERIVED from measurements, and a plan
that cannot fit its ceiling SPLITS or TRIMS and names what it left unverified.
It never returns a plan that cannot complete, and it never silently narrows.

`bin/test_tier_budget.py` covers this module's other half — the tier-total
breach check and its idempotent bead filing. This file covers derivation only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_gate_budget_lib as gate_budget  # noqa: E402


def _costs(python=None, shell=None) -> gate_budget.SuiteCosts:
    return gate_budget.SuiteCosts(python=python or {}, shell=shell or {})


# --- derivation is from data, not from a constant -------------------------


def test_budget_tracks_the_measurement_so_it_cannot_be_a_literal():
    """The discriminator: change only the measurement, and the budget moves.

    A hardcoded ceiling produces the same number for both inputs, so this is
    the assertion a literal cannot pass.
    """
    cheap = gate_budget.derive_plan(
        ("bin/test_a.py",), (), _costs(python={"bin/test_a.py": 30.0}), 1800,
    )
    dear = gate_budget.derive_plan(
        ("bin/test_a.py",), (), _costs(python={"bin/test_a.py": 45.0}), 1800,
    )

    assert cheap.budget_seconds == pytest.approx(60.0)
    assert dear.budget_seconds == pytest.approx(90.0)
    assert dear.budget_seconds > cheap.budget_seconds


def test_scoped_selection_is_not_capped_at_the_ninety_second_literal():
    """slip0.7's measured shape: 12 Python files, 165.47s, all green.

    Under the fixed 90s scoped ceiling this selection could not finish and
    returned exit 124 with no verdict. Derived, it is granted a budget built
    from its own measured cost and nothing is left unverified.
    """
    suites = tuple(f"bin/test_{index}.py" for index in range(12))
    measured = {suite: 165.47 / 12 for suite in suites}

    plan = gate_budget.derive_plan(suites, (), _costs(python=measured), 1800)

    assert plan.measured_seconds == pytest.approx(165.47)
    assert plan.budget_seconds == pytest.approx(330.94)
    assert plan.budget_seconds > 90.0
    assert plan.omitted == ()


def test_the_scoped_tier_value_is_a_floor_and_never_lowers_a_budget():
    """min_shard_seconds may only raise a small budget, never cap a large one."""
    tiny = gate_budget.derive_plan(
        ("bin/test_a.py",), (), _costs(python={"bin/test_a.py": 0.3}), 1800,
        min_shard_seconds=90.0,
    )
    large = gate_budget.derive_plan(
        ("bin/test_a.py",), (), _costs(python={"bin/test_a.py": 400.0}), 1800,
        min_shard_seconds=90.0,
    )

    assert tiny.budget_seconds == pytest.approx(90.0)  # raised off 0.6s
    assert large.budget_seconds == pytest.approx(800.0)  # not capped at 90


# --- trim: it must fit, and it must say what it dropped --------------------


def test_oversized_total_trims_and_names_what_it_left_unverified():
    measured = {"bin/test_a.py": 60.0, "bin/test_b.py": 50.0}
    plan = gate_budget.derive_plan(
        ("bin/test_a.py", "bin/test_b.py"), ("test-x.sh",),
        _costs(python=measured, shell={"test-x.sh": 30.0}), 100,
    )

    # 140s measured against a 100s ceiling: the most expensive suite goes,
    # and it goes BY NAME.
    assert plan.omitted == ("bin/test_a.py",)
    assert plan.measured_seconds == pytest.approx(80.0)  # 50 + 30, fits 100
    assert plan.budget_seconds == pytest.approx(160.0)  # per-command headroom
    executed = {suite for shard in plan.shards for suite in shard.suites}
    assert executed == {"bin/test_b.py", "test-x.sh"}


def test_headroom_is_not_double_counted_into_a_false_trim():
    """This repo's own shape: ~1569s measured against an 1800s bound.

    It fits, and must not be trimmed merely because a 2x per-command headroom
    would notionally total 3138s. Padding the fit decision would turn the
    mechanism that PREVENTS false timeouts into a cause of silent narrowing.
    """
    suites = tuple(f"bin/test_{index}.py" for index in range(100))
    plan = gate_budget.derive_plan(
        suites, (), _costs(python={suite: 15.69 for suite in suites}), 1800,
    )

    assert plan.omitted == ()
    assert plan.measured_seconds == pytest.approx(1569.0)
    assert plan.budget_seconds > 1800  # headroom exceeds the bound, and that is fine


def test_a_fitting_selection_is_never_trimmed():
    """Negative control: without it, a function that always trims would pass."""
    plan = gate_budget.derive_plan(
        ("bin/test_a.py", "bin/test_b.py"), ("test-x.sh",),
        _costs(
            python={"bin/test_a.py": 20.0, "bin/test_b.py": 10.0},
            shell={"test-x.sh": 5.0},
        ),
        200,
    )

    assert plan.omitted == ()
    assert plan.budget_seconds == pytest.approx(70.0)
    executed = {suite for shard in plan.shards for suite in shard.suites}
    assert executed == {"bin/test_a.py", "bin/test_b.py", "test-x.sh"}


def test_trim_keeps_the_largest_number_of_suites_verified():
    """Most-expensive-first: one 90s drop beats three 30s drops."""
    plan = gate_budget.derive_plan(
        ("bin/test_big.py", "bin/test_1.py", "bin/test_2.py", "bin/test_3.py"),
        (),
        _costs(python={
            "bin/test_big.py": 90.0,
            "bin/test_1.py": 10.0,
            "bin/test_2.py": 10.0,
            "bin/test_3.py": 10.0,
        }),
        100,
    )

    assert plan.omitted == ("bin/test_big.py",)
    executed = {suite for shard in plan.shards for suite in shard.suites}
    assert executed == {"bin/test_1.py", "bin/test_2.py", "bin/test_3.py"}


# --- split: no single command may be unable to finish ----------------------


def test_an_oversized_python_command_splits_into_bounded_shards():
    suites = tuple(f"bin/test_{index}.py" for index in range(6))
    plan = gate_budget.derive_plan(
        suites, (), _costs(python={suite: 30.0 for suite in suites}), 1800,
        shard_max_seconds=120,
    )

    assert plan.omitted == ()
    assert len(plan.shards) > 1
    assert all(shard.budget_seconds <= 120 for shard in plan.shards)
    packed = [suite for shard in plan.shards for suite in shard.suites]
    assert sorted(packed) == sorted(suites)  # split, not dropped


def test_shell_suites_stay_one_command_each():
    plan = gate_budget.derive_plan(
        (), ("test-a.sh", "test-b.sh"),
        _costs(shell={"test-a.sh": 5.0, "test-b.sh": 7.0}), 1800,
    )

    assert [shard.suites for shard in plan.shards] == [("test-a.sh",), ("test-b.sh",)]
    assert all(shard.kind == "shell" for shard in plan.shards)


# --- unmeasured suites are costed, not quietly dropped ---------------------


def test_an_unmeasured_suite_is_costed_conservatively_and_still_runs():
    """A brand-new test file has no measurement.

    Dropping it as "unmeasured" would mean the gate never runs the very test
    the change added — the silent narrowing this module exists to prevent.
    """
    plan = gate_budget.derive_plan(
        ("bin/test_known.py", "bin/test_brand_new.py"), (),
        _costs(python={"bin/test_known.py": 12.0}), 1800,
    )

    executed = {suite for shard in plan.shards for suite in shard.suites}
    assert "bin/test_brand_new.py" in executed
    assert plan.omitted == ()
    # Costed at the most expensive thing ever measured, so it is granted
    # enough budget to reach a verdict.
    assert plan.measured_seconds == pytest.approx(24.0)


def test_provisional_cost_is_the_largest_measurement_not_an_invented_number():
    costs = _costs(python={"a": 3.0, "b": 11.0}, shell={"c": 7.0})
    assert gate_budget.provisional_seconds(costs) == pytest.approx(11.0)


def test_provisional_cost_uses_the_suites_own_kind():
    """Measured on this repo: Python integration suites reach 200s while shell
    suites are far cheaper. Charging an unmeasured shell suite the Python
    maximum would manufacture trims the real costs never justified."""
    costs = _costs(python={"bin/test_slow.py": 200.0}, shell={"test-a.sh": 4.0})

    assert gate_budget.provisional_seconds(costs, "shell") == pytest.approx(4.0)
    assert gate_budget.provisional_seconds(costs, "python") == pytest.approx(200.0)

    plan = gate_budget.derive_plan(
        (), ("test-a.sh", "test-brand-new.sh"), costs, 1800,
    )
    assert plan.omitted == ()
    assert plan.measured_seconds == pytest.approx(8.0)  # 4 + 4, not 4 + 200


def test_a_trim_never_fires_on_a_provisional_cost():
    """Dropping real coverage to satisfy an invented number is still narrowing.

    Measured on this repo: a Python cost report with no shell profile made all
    95 shell suites inherit the 200s Python maximum, and 88 were "trimmed" on
    the strength of a number nobody measured. The measured suite here would
    blow the ceiling on its own, yet the presence of ONE unmeasured suite must
    suppress the trim entirely.
    """
    plan = gate_budget.derive_plan(
        ("bin/test_measured.py", "bin/test_unmeasured.py"), (),
        _costs(python={"bin/test_measured.py": 500.0}), 100,
    )

    assert plan.omitted == ()
    executed = {suite for shard in plan.shards for suite in shard.suites}
    assert executed == {"bin/test_measured.py", "bin/test_unmeasured.py"}


def test_a_fully_measured_plan_still_trims(  # negative control for the rule above
):
    """Without this, "never trim on provisional" could hide a dead trim path."""
    plan = gate_budget.derive_plan(
        ("bin/test_a.py", "bin/test_b.py"), (),
        _costs(python={"bin/test_a.py": 500.0, "bin/test_b.py": 20.0}), 100,
    )

    assert plan.omitted == ("bin/test_a.py",)


def test_provisional_falls_back_across_kinds_when_its_own_kind_is_unmeasured():
    costs = _costs(python={"bin/test_a.py": 12.0})
    assert gate_budget.provisional_seconds(costs, "shell") == pytest.approx(12.0)


def test_nothing_measured_means_nothing_derivable():
    """With no data there is no derivation — say so rather than invent one."""
    empty = _costs()
    assert gate_budget.has_measurements(empty) is False
    with pytest.raises(ValueError, match="no measurements"):
        gate_budget.provisional_seconds(empty)


def test_a_ceiling_must_be_positive():
    with pytest.raises(ValueError, match="ceiling must be positive"):
        gate_budget.derive_plan(("bin/test_a.py",), (), _costs(python={"bin/test_a.py": 1.0}), 0)


# --- the two existing reporters' real formats ------------------------------


def test_python_cost_report_reads_conftests_own_output(tmp_path):
    report = tmp_path / "cost.json"
    report.write_text(json.dumps({
        "thresholds": {"ordinary_test_seconds": 1.0},
        "tests": [{"nodeid": "bin/test_a.py::test_x", "seconds": 2.5}],
        "modules": [
            {"module": "bin/test_a.py", "seconds": 2.5, "declared_heavy": False},
            {"module": "bin/test_b.py", "seconds": 9.0, "declared_heavy": True},
        ],
        "violations": [],
    }))

    assert gate_budget.load_python_cost_report(report) == {
        "bin/test_a.py": 2.5,
        "bin/test_b.py": 9.0,
    }


def test_shell_cost_profile_reads_shell_run_sets_own_output(tmp_path):
    profile = tmp_path / "cost.tsv"
    profile.write_text(
        "suite\tstatus\tseconds\n"
        "test-a.sh\tpass\t4.500000\n"
        "test-b.sh\tmissing\t0.000000\n"
        "test-c.sh\tfail\t1.250000\n"
    )

    # Only a passing measurement is a usable cost: a suite that died early
    # measures how fast it failed, not what it costs to run.
    assert gate_budget.load_shell_cost_profile(profile) == {"test-a.sh": 4.5}


def test_a_corrupt_report_is_rejected_rather_than_read_as_zero(tmp_path):
    report = tmp_path / "cost.json"
    report.write_text(json.dumps({"modules": [{"module": "bin/test_a.py"}]}))
    with pytest.raises(ValueError, match="non-numeric"):
        gate_budget.load_python_cost_report(report)

    profile = tmp_path / "cost.tsv"
    profile.write_text("not\ta\theader\n")
    with pytest.raises(ValueError, match="invalid shell cost profile header"):
        gate_budget.load_shell_cost_profile(profile)
