#!/usr/bin/env python3
"""test_columbo_prefilter — unit tests for the prefilter scaffolding.

Tests the skeleton from rjv.5.1: argparse, output formatting, file
discovery, and heuristic-registry contract. Heuristics themselves land
in sibling beads (5.2-5.5); here we verify the registry plumbing using
in-test stub heuristics.

Run with:

  python3 bin/test_columbo_prefilter.py

Exits 0 if all pass, 1 if any fail. No pytest dependency.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

# Load the prefilter module by file path (the hyphen in its name keeps it
# from being importable via normal import statements). Mirrors the
# importlib pattern in test_tripwire_watcher.py.
SCRIPT_DIR = Path(__file__).resolve().parent
PREFILTER_PATH = SCRIPT_DIR / "columbo-prefilter.py"
spec = importlib.util.spec_from_file_location("columbo_prefilter", PREFILTER_PATH)
cp = importlib.util.module_from_spec(spec)
sys.modules["columbo_prefilter"] = cp
spec.loader.exec_module(cp)


PASS = 0
FAIL = 0
FAILED_NAMES: list[str] = []


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


def assert_in(name: str, needle, haystack):
    global PASS, FAIL
    if needle in haystack:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print(f"FAIL: {name}")
        print(f"  expected substring: {needle!r}")
        print(f"  in:                 {haystack!r}")


def assert_true(name: str, cond, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print(f"FAIL: {name}{(' — ' + detail) if detail else ''}")


# ---------------------------------------------------------------------------
# Helpers — register stub heuristics, restore after each test
# ---------------------------------------------------------------------------


def with_heuristics(heuristics):
    """Decorator-style helper: replace HEURISTICS for one test, restore after."""
    def wrap(fn):
        def run():
            saved = list(cp.HEURISTICS)
            cp.HEURISTICS.clear()
            cp.HEURISTICS.extend(heuristics)
            try:
                fn()
            finally:
                cp.HEURISTICS.clear()
                cp.HEURISTICS.extend(saved)
        return run
    return wrap


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_argparse_help():
    """--help exits cleanly and prints usage."""
    buf = io.StringIO()
    code = None
    try:
        with redirect_stdout(buf):
            cp.main(["--help"])
    except SystemExit as e:
        code = e.code
    assert_eq("argparse_help: exits with 0", code, 0)
    assert_in("argparse_help: prints usage", "usage:", buf.getvalue().lower())


def test_argparse_threshold():
    """--threshold N filters out files scoring below N."""
    @with_heuristics([
        {"name": "low", "score": 5, "fire": lambda t, s: t.name == "low.test.ts"},
        {"name": "high", "score": 8, "fire": lambda t, s: t.name == "high.test.ts"},
    ])
    def run():
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "low.test.ts").write_text("// stub\n")
            (tmp / "low.ts").write_text("// stub\n")
            (tmp / "high.test.ts").write_text("// stub\n")
            (tmp / "high.ts").write_text("// stub\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                cp.main([str(tmp), "--threshold", "7"])
            out = buf.getvalue()
            assert_in("threshold: high.test.ts surfaces", "high.test.ts", out)
            assert_true(
                "threshold: low.test.ts filtered out",
                "low.test.ts" not in out,
                f"unexpected low.test.ts in: {out}",
            )
    run()


def test_text_output_format():
    """Default text mode emits one line per file with score and signals."""
    results = [
        {"path": "src/foo.test.ts", "score": 8, "signals": [
            {"name": "happy-path-only", "score": 7},
            {"name": "mock-saturation", "score": 8},
        ]},
    ]
    out = cp.format_text(results)
    assert_in("text_format: path appears", "src/foo.test.ts", out)
    assert_in("text_format: score appears", "score=8", out)
    assert_in("text_format: signals comma-separated", "happy-path-only,mock-saturation", out)


def test_json_output_format():
    """--json output is well-formed JSON parseable back to Python."""
    results = [
        {"path": "src/foo.test.ts", "score": 7, "signals": [
            {"name": "happy-path-only", "score": 7},
        ]},
    ]
    out = cp.format_json(results)
    parsed = json.loads(out)
    assert_eq("json_format: list of one", len(parsed), 1)
    assert_eq("json_format: path round-trip", parsed[0]["path"], "src/foo.test.ts")
    assert_eq("json_format: score round-trip", parsed[0]["score"], 7)
    assert_eq("json_format: signals round-trip", parsed[0]["signals"][0]["name"], "happy-path-only")
    assert_eq("json_format: signal score round-trip", parsed[0]["signals"][0]["score"], 7)


def test_file_discovery_ts():
    """Test+source TS pair is discovered and paired."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "foo.test.ts").write_text("// stub\n")
        (tmp / "foo.ts").write_text("// stub\n")
        pairs = cp.discover(tmp)
        assert_eq("discovery_ts: one pair found", len(pairs), 1)
        test, source = pairs[0]
        assert_eq("discovery_ts: test path", test.name, "foo.test.ts")
        assert_eq("discovery_ts: source paired", source.name if source else None, "foo.ts")


def test_file_discovery_py():
    """Test+source Python pair is discovered and paired (foo_test.py → foo.py)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "foo_test.py").write_text("# stub\n")
        (tmp / "foo.py").write_text("# stub\n")
        pairs = cp.discover(tmp)
        assert_eq("discovery_py: one pair found", len(pairs), 1)
        test, source = pairs[0]
        assert_eq("discovery_py: test path", test.name, "foo_test.py")
        assert_eq("discovery_py: source paired", source.name if source else None, "foo.py")


def test_file_discovery_no_source():
    """Test file without a matching source still gets scored (source=None)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "orphan.test.ts").write_text("// stub\n")
        # Note: no orphan.ts
        pairs = cp.discover(tmp)
        assert_eq("discovery_no_source: one pair found", len(pairs), 1)
        test, source = pairs[0]
        assert_eq("discovery_no_source: test path", test.name, "orphan.test.ts")
        assert_eq("discovery_no_source: source is None", source, None)


def test_heuristic_registry():
    """A registered heuristic that fires contributes its score and name to output."""
    @with_heuristics([
        {"name": "stub-fires", "score": 6, "fire": lambda t, s: True},
    ])
    def run():
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "x.test.ts").write_text("// stub\n")
            (tmp / "x.ts").write_text("// stub\n")
            results = cp.score_files(cp.discover(tmp))
            assert_eq("registry: one file scored", len(results), 1)
            assert_eq("registry: score is heuristic score", results[0]["score"], 6)
            signal_names = [s["name"] for s in results[0]["signals"]]
            assert_in("registry: signal name appears", "stub-fires", signal_names)
    run()


def test_max_score_not_sum():
    """Two heuristics firing — final score is max(scores), not sum."""
    @with_heuristics([
        {"name": "five", "score": 5, "fire": lambda t, s: True},
        {"name": "eight", "score": 8, "fire": lambda t, s: True},
    ])
    def run():
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "x.test.ts").write_text("// stub\n")
            (tmp / "x.ts").write_text("// stub\n")
            results = cp.score_files(cp.discover(tmp))
            assert_eq("max_not_sum: final score is 8 (max), not 13 (sum)", results[0]["score"], 8)
            signal_names = [s["name"] for s in results[0]["signals"]]
            assert_eq("max_not_sum: both signals listed", sorted(signal_names), ["eight", "five"])
    run()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


TESTS = [
    test_argparse_help,
    test_argparse_threshold,
    test_text_output_format,
    test_json_output_format,
    test_file_discovery_ts,
    test_file_discovery_py,
    test_file_discovery_no_source,
    test_heuristic_registry,
    test_max_score_not_sum,
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
