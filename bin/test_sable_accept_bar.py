#!/usr/bin/env python3
"""test_sable_accept_bar — unit tests for sable_accept_bar (SABLE-5lli.6).

Run with:

  python3 bin/test_sable_accept_bar.py

Exits 0 if all pass, 1 if any fail. No pytest dependency.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sable_accept_bar as bar  # noqa: E402

PASS = 0
FAIL = 0
FAILED_NAMES: list[str] = []


def assert_close(name: str, got: float, expected: float, tol: float = 0.001):
    global PASS, FAIL
    if abs(got - expected) <= tol:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print(f"FAIL: {name}")
        print(f"  expected: {expected!r} +/- {tol}")
        print(f"  got:      {got!r}")


def assert_eq(name: str, got, expected):
    global PASS, FAIL
    if got == expected:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print(f"FAIL: {name}")
        print(f"  expected: {expected!r}")
        print(f"  got:      {got!r}")


def test_qby7_false_green_at_n100_p1in300():
    # SABLE-qby7's documented worst case: 100 iterations at a 1-in-300 base
    # rate looked rigorous and was ~72% likely to manufacture a false green.
    assert_close(
        "false_green_probability(100, 1/300) reproduces qby7's 0.716",
        bar.false_green_probability(100, 1 / 300),
        0.716,
        tol=0.001,
    )


def test_qby7_bar_at_p1in300():
    assert_eq(
        "bar_for_confidence(1/300) reproduces qby7's ~897",
        bar.bar_for_confidence(1 / 300),
        897,
    )


def test_bar_at_p1in100():
    # 4nmi's companion figure at the tighter base rate.
    assert_eq(
        "bar_for_confidence(1/100) reproduces 4nmi's ~298",
        bar.bar_for_confidence(1 / 100),
        298,
    )


def test_false_green_probability_zero_trials_is_one():
    assert_eq(
        "false_green_probability(0, p) is 1.0 regardless of p (zero evidence)",
        bar.false_green_probability(0, 1 / 300),
        1.0,
    )


def test_false_green_probability_rejects_out_of_range_p():
    global PASS, FAIL
    try:
        bar.false_green_probability(10, 1.5)
        FAIL += 1
        FAILED_NAMES.append("false_green_probability rejects p > 1")
        print("FAIL: false_green_probability rejects p > 1")
    except ValueError:
        PASS += 1
        print("PASS: false_green_probability rejects p > 1")


def test_bar_for_confidence_rejects_p_zero():
    global PASS, FAIL
    try:
        bar.bar_for_confidence(0)
        FAIL += 1
        FAILED_NAMES.append("bar_for_confidence rejects p=0 (no defect, no bar)")
        print("FAIL: bar_for_confidence rejects p=0 (no defect, no bar)")
    except ValueError:
        PASS += 1
        print("PASS: bar_for_confidence rejects p=0 (no defect, no bar)")


TESTS = [
    test_qby7_false_green_at_n100_p1in300,
    test_qby7_bar_at_p1in300,
    test_bar_at_p1in100,
    test_false_green_probability_zero_trials_is_one,
    test_false_green_probability_rejects_out_of_range_p,
    test_bar_for_confidence_rejects_p_zero,
]


def main() -> int:
    for t in TESTS:
        try:
            t()
        except Exception as e:
            global FAIL
            FAIL += 1
            FAILED_NAMES.append(f"{t.__name__} (raised {type(e).__name__})")
            print(f"FAIL: {t.__name__} — raised {type(e).__name__}: {e}")
    print()
    print("==========================================")
    print(f"Tests: {PASS + FAIL} | Passed: {PASS} | Failed: {FAIL}")
    print("==========================================")
    if FAIL:
        print("Failed:")
        for n in FAILED_NAMES:
            print(f"  {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
