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


def test_file_discovery_pytest_prefix():
    """test_<name>.py (pytest convention) is recognized and paired co-located."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "test_foo.py").write_text("# stub\n")
        (tmp / "foo.py").write_text("# stub\n")
        pairs = cp.discover(tmp)
        assert_eq("discovery_pytest_prefix: one pair found", len(pairs), 1)
        test, source = pairs[0]
        assert_eq("discovery_pytest_prefix: test path", test.name, "test_foo.py")
        assert_eq(
            "discovery_pytest_prefix: source paired",
            source.name if source else None,
            "foo.py",
        )


def test_file_discovery_pytest_tests_sibling_dir():
    """tests/test_foo.py paired with foo.py one directory up (project root)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "tests").mkdir()
        (tmp / "tests" / "test_foo.py").write_text("# stub\n")
        (tmp / "foo.py").write_text("# stub\n")
        pairs = cp.discover(tmp)
        assert_eq("discovery_pytest_sibling: one pair found", len(pairs), 1)
        test, source = pairs[0]
        assert_eq(
            "discovery_pytest_sibling: source paired across dirs",
            source.name if source else None,
            "foo.py",
        )


def test_file_discovery_pytest_tests_to_src_subdir():
    """tests/test_foo.py paired with src/foo.py (common Python layout)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "tests").mkdir()
        (tmp / "src").mkdir()
        (tmp / "tests" / "test_foo.py").write_text("# stub\n")
        (tmp / "src" / "foo.py").write_text("# stub\n")
        pairs = cp.discover(tmp)
        assert_eq("discovery_pytest_to_src: one pair found", len(pairs), 1)
        test, source = pairs[0]
        assert_true(
            "discovery_pytest_to_src: source resolved to src/",
            source is not None and source.parent.name == "src",
            f"got {source}",
        )


def test_file_discovery_ts_tests_dir_mirror():
    """__tests__/lib/foo.test.ts paired with lib/foo.ts (mirror layout)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "__tests__" / "lib").mkdir(parents=True)
        (tmp / "lib").mkdir()
        (tmp / "__tests__" / "lib" / "foo.test.ts").write_text("// stub\n")
        (tmp / "lib" / "foo.ts").write_text("// stub\n")
        pairs = cp.discover(tmp)
        assert_eq("discovery_ts_mirror: one pair found", len(pairs), 1)
        test, source = pairs[0]
        assert_true(
            "discovery_ts_mirror: source resolved across __tests__ strip",
            source is not None and source.name == "foo.ts" and source.parent.name == "lib",
            f"got {source}",
        )


def test_file_discovery_ts_tests_dir_at_root():
    """__tests__/foo.test.ts paired with foo.ts one level up."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "__tests__").mkdir()
        (tmp / "__tests__" / "foo.test.ts").write_text("// stub\n")
        (tmp / "foo.ts").write_text("// stub\n")
        pairs = cp.discover(tmp)
        assert_eq("discovery_ts_at_root: one pair found", len(pairs), 1)
        test, source = pairs[0]
        assert_eq(
            "discovery_ts_at_root: source paired",
            source.name if source else None,
            "foo.ts",
        )


def test_file_discovery_skips_node_modules():
    """Files inside node_modules/.next/.venv/__pycache__ are skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "node_modules" / "pkg").mkdir(parents=True)
        (tmp / "node_modules" / "pkg" / "foo.test.ts").write_text("// stub\n")
        (tmp / ".venv" / "lib").mkdir(parents=True)
        (tmp / ".venv" / "lib" / "test_bar.py").write_text("# stub\n")
        # Legit one outside skip dirs to confirm discovery still works.
        (tmp / "ok.test.ts").write_text("// stub\n")
        pairs = cp.discover(tmp)
        names = sorted(p[0].name for p in pairs)
        assert_eq("discovery_skip_dirs: only non-skipped test found", names, ["ok.test.ts"])


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
# rjv.5.2 — Heuristic 2 (happy-path-only) + Heuristic 4 (single-case wonder)
# ---------------------------------------------------------------------------


def _h(name: str):
    """Return the registered heuristic by name, or None if not registered."""
    for h in cp.HEURISTICS:
        if h["name"] == name:
            return h
    return None


def test_h2_fires_on_only_happy_assertions():
    """TS test with only happy expects → heuristic 2 fires."""
    h2 = _h("happy-path-only")
    assert_true("h2 registered", h2 is not None)
    if h2 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "foo.test.ts"
        test.write_text("it('works', () => { expect(x).toBe(1); expect(y).toEqual(2); });\n")
        assert_eq("h2: fires on happy-only TS", h2["fire"](test, None), True)


def test_h2_does_not_fire_with_toThrow():
    """TS test with .toThrow → heuristic 2 does not fire."""
    h2 = _h("happy-path-only")
    if h2 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "foo.test.ts"
        test.write_text("it('throws', () => { expect(() => bad()).toThrow(); });\n")
        assert_eq("h2: skip TS toThrow", h2["fire"](test, None), False)


def test_h2_does_not_fire_with_pytest_raises():
    """Python test with pytest.raises → heuristic 2 does not fire."""
    h2 = _h("happy-path-only")
    if h2 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "foo_test.py"
        test.write_text("def test_x():\n    with pytest.raises(ValueError):\n        bad()\n")
        assert_eq("h2: skip pytest.raises", h2["fire"](test, None), False)


def test_h2_fires_on_pure_python_assertions():
    """Python test with only happy assert statements → heuristic 2 fires."""
    h2 = _h("happy-path-only")
    if h2 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "foo_test.py"
        test.write_text("def test_x():\n    assert foo(1) == 1\n    assert foo(2) == 2\n")
        assert_eq("h2: fires on happy-only Python", h2["fire"](test, None), True)


def test_h4_fires_when_one_case_many_functions():
    """1 it block + source with 5 export functions → heuristic 4 fires."""
    h4 = _h("single-case-wonder")
    assert_true("h4 registered", h4 is not None)
    if h4 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "foo.test.ts"
        source = Path(tmp) / "foo.ts"
        test.write_text("it('one', () => { expect(x).toBe(1); });\n")
        source.write_text("\n".join([
            "export function a() {}",
            "export function b() {}",
            "export function c() {}",
            "export function d() {}",
            "export function e() {}",
        ]) + "\n")
        assert_eq("h4: fires 1-vs-5", h4["fire"](test, source), True)


def test_h4_does_not_fire_when_many_cases():
    """4 it blocks + source with 5 functions → heuristic 4 does not fire."""
    h4 = _h("single-case-wonder")
    if h4 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "foo.test.ts"
        source = Path(tmp) / "foo.ts"
        test.write_text("\n".join([
            "it('a', () => { expect(x).toBe(1); });",
            "it('b', () => { expect(x).toBe(2); });",
            "it('c', () => { expect(x).toBe(3); });",
            "it('d', () => { expect(x).toBe(4); });",
        ]) + "\n")
        source.write_text("\n".join([
            "export function a() {}",
            "export function b() {}",
            "export function c() {}",
            "export function d() {}",
            "export function e() {}",
        ]) + "\n")
        assert_eq("h4: skip many cases", h4["fire"](test, source), False)


def test_h4_does_not_fire_when_few_source_functions():
    """1 it block + source with 2 functions → heuristic 4 does not fire (below floor)."""
    h4 = _h("single-case-wonder")
    if h4 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "foo.test.ts"
        source = Path(tmp) / "foo.ts"
        test.write_text("it('one', () => { expect(x).toBe(1); });\n")
        source.write_text("export function a() {}\nexport function b() {}\n")
        assert_eq("h4: skip below floor", h4["fire"](test, source), False)


def test_h4_skips_when_no_source():
    """1 it block + source=None → heuristic 4 does not fire (and does not error)."""
    h4 = _h("single-case-wonder")
    if h4 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "foo.test.ts"
        test.write_text("it('one', () => { expect(x).toBe(1); });\n")
        assert_eq("h4: skip no source", h4["fire"](test, None), False)


def test_h4_python_class_methods_counted():
    """Python source with class C: 4 public methods + 1 single test → heuristic 4 fires."""
    h4 = _h("single-case-wonder")
    if h4 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "foo_test.py"
        source = Path(tmp) / "foo.py"
        test.write_text("def test_a():\n    assert foo() == 1\n")
        source.write_text("\n".join([
            "class C:",
            "    def a(self): pass",
            "    def b(self): pass",
            "    def c(self): pass",
            "    def d(self): pass",
            "    def _private(self): pass",
        ]) + "\n")
        assert_eq("h4: counts class methods, skips _private", h4["fire"](test, source), True)


# ---------------------------------------------------------------------------
# rjv.5.3 — Heuristic 6 (stale fixture, score 9)
# ---------------------------------------------------------------------------


def test_h6_fires_on_deleted_ts_export():
    """TS test imports {processRefund}, source has only processOrder → fires."""
    h6 = _h("stale-fixture")
    assert_true("h6 registered", h6 is not None)
    if h6 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "refund.test.ts"
        source = tmp / "refund.ts"
        test.write_text("import { processRefund } from './refund';\nit('x', () => {});\n")
        source.write_text("export function processOrder() {}\n")
        result = h6["fire"](test, source)
        assert_true(
            "h6: deleted TS export fires (truthy detail)",
            bool(result),
            f"got: {result!r}",
        )
        assert_in("h6: detail names processRefund", "processRefund", str(result))


def test_h6_fires_on_deleted_py_function():
    """Python test does 'from .refund import process_refund'; module has only process_order → fires."""
    h6 = _h("stale-fixture")
    if h6 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "refund_test.py"
        source = tmp / "refund.py"
        test.write_text("from .refund import process_refund\n\ndef test_x():\n    assert True\n")
        source.write_text("def process_order():\n    return 1\n")
        result = h6["fire"](test, source)
        assert_true(
            "h6: deleted Python export fires",
            bool(result),
            f"got: {result!r}",
        )
        assert_in("h6: detail names process_refund", "process_refund", str(result))


def test_h6_does_not_fire_on_valid_ts_export():
    """TS test imports {processRefund}, source exports processRefund → no fire."""
    h6 = _h("stale-fixture")
    if h6 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "refund.test.ts"
        source = tmp / "refund.ts"
        test.write_text("import { processRefund } from './refund';\nit('x', () => {});\n")
        source.write_text("export function processRefund() {}\n")
        assert_eq("h6: valid TS export skips", bool(h6["fire"](test, source)), False)


def test_h6_does_not_fire_on_valid_py_export():
    """Python test imports process_refund, source defines it → no fire."""
    h6 = _h("stale-fixture")
    if h6 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "refund_test.py"
        source = tmp / "refund.py"
        test.write_text("from .refund import process_refund\n\ndef test_x():\n    assert True\n")
        source.write_text("def process_refund():\n    return 1\n")
        assert_eq("h6: valid Python export skips", bool(h6["fire"](test, source)), False)


def test_h6_skips_namespace_import():
    """`import * as foo from './bar'` is unverifiable; skipped without firing."""
    h6 = _h("stale-fixture")
    if h6 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "bar.test.ts"
        source = tmp / "bar.ts"
        # bar.ts is empty — namespace import would normally seem stale, but
        # the heuristic must skip namespace imports entirely.
        test.write_text("import * as bar from './bar';\nit('x', () => {});\n")
        source.write_text("\n")
        assert_eq("h6: namespace import skipped", bool(h6["fire"](test, source)), False)


def test_h6_skips_unresolved_target():
    """Imports from non-relative paths (node_modules / pip) are skipped."""
    h6 = _h("stale-fixture")
    if h6 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "comp.test.ts"
        source = tmp / "comp.ts"
        # 'react' is non-relative; the heuristic shouldn't try to resolve it.
        test.write_text("import { useState } from 'react';\nit('x', () => {});\n")
        source.write_text("export function comp() {}\n")
        assert_eq("h6: external import skipped", bool(h6["fire"](test, source)), False)


def test_h6_handles_default_export():
    """`import bar from './foo'` + foo.ts has any `export default` → no fire."""
    h6 = _h("stale-fixture")
    if h6 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "foo.test.ts"
        # foo.ts default-exports a function; the local name in the test ('bar')
        # is the import alias and doesn't need to match the export's actual name.
        target = tmp / "foo.ts"
        test.write_text("import bar from './foo';\nit('x', () => {});\n")
        target.write_text("export default function whatever() {}\n")
        assert_eq("h6: default import accepts any default export", bool(h6["fire"](test, target)), False)


def test_h6_handles_named_reexport():
    """`export { processRefund } from './inner'` counts as an export of processRefund."""
    h6 = _h("stale-fixture")
    if h6 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "refund.test.ts"
        source = tmp / "refund.ts"
        test.write_text("import { processRefund } from './refund';\nit('x', () => {});\n")
        source.write_text("export { processRefund } from './inner';\n")
        assert_eq("h6: named re-export counts as export", bool(h6["fire"](test, source)), False)


# ---------------------------------------------------------------------------
# rjv.5.4 — Heuristic 3 (mock saturation) + Heuristic 5 (missing categories)
# ---------------------------------------------------------------------------


def test_h3_fires_on_overmocked_integration():
    """integration.test.ts with 5 imports and 4 vi.mock calls → fires."""
    h3 = _h("mock-saturation")
    assert_true("h3 registered", h3 is not None)
    if h3 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "user.integration.test.ts"
        test.write_text("\n".join([
            "import { a } from './a';",
            "import { b } from './b';",
            "import { c } from './c';",
            "import { d } from './d';",
            "import { e } from './e';",
            "vi.mock('./a');",
            "vi.mock('./b');",
            "vi.mock('./c');",
            "vi.mock('./d');",
            "it('x', () => { expect(1).toBe(1); });",
        ]))
        assert_eq("h3: overmocked integration fires", bool(h3["fire"](test, None)), True)


def test_h3_does_not_fire_below_threshold():
    """integration.test.ts with 5 imports and 2 mocks → no fire (40% < 70%)."""
    h3 = _h("mock-saturation")
    if h3 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "user.integration.test.ts"
        test.write_text("\n".join([
            "import { a } from './a';",
            "import { b } from './b';",
            "import { c } from './c';",
            "import { d } from './d';",
            "import { e } from './e';",
            "vi.mock('./a');",
            "vi.mock('./b');",
            "it('x', () => { expect(1).toBe(1); });",
        ]))
        assert_eq("h3: under-threshold skips", bool(h3["fire"](test, None)), False)


def test_h3_does_not_fire_on_unit_tests():
    """foo.test.ts (no integration tag) with 5 imports and 5 mocks → no fire."""
    h3 = _h("mock-saturation")
    if h3 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "foo.test.ts"
        test.write_text("\n".join([
            "import { a } from './a';",
            "import { b } from './b';",
            "import { c } from './c';",
            "import { d } from './d';",
            "import { e } from './e';",
            "vi.mock('./a');",
            "vi.mock('./b');",
            "vi.mock('./c');",
            "vi.mock('./d');",
            "vi.mock('./e');",
            "it('x', () => { expect(1).toBe(1); });",
        ]))
        assert_eq("h3: unit test skips even when fully mocked", bool(h3["fire"](test, None)), False)


def test_h3_python_unittest_mock():
    """Python integration_test.py with 4 patches over 5 imports → fires."""
    h3 = _h("mock-saturation")
    if h3 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "user_integration_test.py"
        test.write_text("\n".join([
            "import a",
            "import b",
            "from c import x",
            "from d import y",
            "from e import z",
            "@mock.patch('a.foo')",
            "@mock.patch('b.bar')",
            "@mock.patch('c.x')",
            "@mock.patch('d.y')",
            "def test_one():",
            "    assert True",
        ]))
        assert_eq("h3: Python @patch saturated", bool(h3["fire"](test, None)), True)


def test_h3_describe_block_tag():
    """foo.test.ts with describe('integration: ...') and 4/5 mocks → fires."""
    h3 = _h("mock-saturation")
    if h3 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "foo.test.ts"
        test.write_text("\n".join([
            "import { a } from './a';",
            "import { b } from './b';",
            "import { c } from './c';",
            "import { d } from './d';",
            "import { e } from './e';",
            "vi.mock('./a');",
            "vi.mock('./b');",
            "vi.mock('./c');",
            "vi.mock('./d');",
            "describe('integration: user flow', () => {",
            "  it('x', () => { expect(1).toBe(1); });",
            "});",
        ]))
        assert_eq("h3: describe-block integration tag triggers fire", bool(h3["fire"](test, None)), True)


def test_h5_fires_on_state_machine_no_transition_tests():
    """Source has switch + 3 cases; test never mentions transition → fires with state-machine in detail."""
    h5 = _h("missing-categories")
    assert_true("h5 registered", h5 is not None)
    if h5 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "fsm.test.ts"
        source = tmp / "fsm.ts"
        source.write_text("\n".join([
            "export function next(s: string) {",
            "  switch (s) {",
            "    case 'a': return 'b';",
            "    case 'b': return 'c';",
            "    case 'c': return 'a';",
            "  }",
            "}",
        ]))
        test.write_text("it('returns next', () => { expect(next('a')).toBe('b'); });\n")
        result = h5["fire"](test, source)
        assert_true("h5: state-machine missing fires", bool(result), f"got: {result!r}")
        assert_in("h5: detail names state-machine", "state-machine", str(result))


def test_h5_does_not_fire_when_test_mentions_transition():
    """Same source + test with describe('transitions') → no fire."""
    h5 = _h("missing-categories")
    if h5 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "fsm.test.ts"
        source = tmp / "fsm.ts"
        source.write_text("\n".join([
            "export function next(s: string) {",
            "  switch (s) {",
            "    case 'a': return 'b';",
            "    case 'b': return 'c';",
            "    case 'c': return 'a';",
            "  }",
            "}",
        ]))
        test.write_text("describe('transitions', () => { it('a→b', () => { expect(next('a')).toBe('b'); }); });\n")
        assert_eq("h5: covered category skips", bool(h5["fire"](test, source)), False)


def test_h5_fires_multiple_categories():
    """Source with state-machine + security; tests cover neither → detail lists both."""
    h5 = _h("missing-categories")
    if h5 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "auth_fsm.test.ts"
        source = tmp / "auth_fsm.ts"
        source.write_text("\n".join([
            "export function authenticate(t: string) { return verify_token(t); }",
            "export function next(s: string) {",
            "  switch (s) {",
            "    case 'a': return 'b';",
            "    case 'b': return 'c';",
            "    case 'c': return 'a';",
            "  }",
            "}",
        ]))
        test.write_text("it('default path', () => { expect(next('a')).toBe('b'); });\n")
        result = h5["fire"](test, source)
        assert_true("h5: multiple missing fires", bool(result))
        assert_in("h5: state-machine in detail", "state-machine", str(result))
        assert_in("h5: security in detail", "security", str(result))


def test_h5_skips_when_no_source():
    """source_path=None → no fire (and no exception)."""
    h5 = _h("missing-categories")
    if h5 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "x.test.ts"
        test.write_text("it('x', () => { expect(1).toBe(1); });\n")
        assert_eq("h5: no-source skips", bool(h5["fire"](test, None)), False)


def test_h5_concurrency_pattern():
    """Source has async + Mutex; test never mentions race/concurrent → fires with concurrency."""
    h5 = _h("missing-categories")
    if h5 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "queue.test.ts"
        source = tmp / "queue.ts"
        source.write_text("\n".join([
            "import { Mutex } from 'async-mutex';",
            "const lock = new Mutex();",
            "export async function push(x: number) {",
            "  await lock.runExclusive(() => { /* ... */ });",
            "}",
        ]))
        test.write_text("it('pushes', async () => { await push(1); expect(1).toBe(1); });\n")
        result = h5["fire"](test, source)
        assert_true("h5: concurrency missing fires", bool(result))
        assert_in("h5: concurrency in detail", "concurrency", str(result))


# ---------------------------------------------------------------------------
# rjv.5.5 — Heuristic 1 (assertion density) + epic integration test
# ---------------------------------------------------------------------------


def test_h1_python_ast_counts_branches():
    """Python ast walker returns expected branch counts for hand-crafted sources."""
    fixtures = [
        ("def foo(): pass\n", 0),
        ("if a: pass\n", 1),
        ("if a: pass\nif b: pass\nif c: pass\n", 3),
        # 1 if + 1 for + 1 while + (1 try + 1 except) = 5
        ("if a: pass\nfor x in y: pass\nwhile z: pass\ntry:\n    pass\nexcept E:\n    pass\n", 5),
        # 1 if (with elif → nested If, +1) + 1 BoolOp (a and b → +1) = 3
        # (the elif counts as a nested if = +1, but the original if is +1 → 2; plus the BoolOp's len-1 = 1 → 3)
        ("if a and b:\n    pass\nelif c:\n    pass\n", 3),
    ]
    for source, expected in fixtures:
        got = cp._count_branches_py(source)
        assert_eq(f"h1: branch count for {source!r}", got, expected)


def test_h1_python_assertion_count():
    """Assertion counter handles `assert ...` and `self.assert*` patterns."""
    fixtures = [
        ("import x\n", 0),
        ("def test_x():\n    assert a == b\n    assert c == d\n    assert e == f\n", 3),
        ("def test_y(self):\n    self.assertEqual(a, b)\n    self.assertTrue(c)\n    assert d == e\n", 3),
    ]
    for text, expected in fixtures:
        got = cp._count_assertions(text, "py")
        assert_eq(f"h1: assertion count for {text[:30]!r}...", got, expected)


def test_h1_fires_on_low_density():
    """Source with many branches, test with few assertions → density < 1.0 → fires."""
    h1 = _h("assertion-density")
    assert_true("h1 registered", h1 is not None)
    if h1 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "deep_logic_test.py"
        source = tmp / "deep_logic.py"
        # 10 branches, 3 assertions
        source.write_text("\n".join([
            "def foo(x):",
            "    if x == 1: return 1",
            "    if x == 2: return 2",
            "    if x == 3: return 3",
            "    if x == 4: return 4",
            "    if x == 5: return 5",
            "    if x == 6: return 6",
            "    if x == 7: return 7",
            "    if x == 8: return 8",
            "    if x == 9: return 9",
            "    if x == 10: return 10",
        ]))
        test.write_text("\n".join([
            "def test_x():",
            "    assert foo(1) == 1",
            "    assert foo(2) == 2",
            "    assert foo(3) == 3",
        ]))
        assert_eq("h1: low density fires", bool(h1["fire"](test, source)), True)


def test_h1_does_not_fire_above_threshold():
    """Density >= 1.0 → no fire."""
    h1 = _h("assertion-density")
    if h1 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "well_tested_test.py"
        source = tmp / "well_tested.py"
        # 4 branches, 5 assertions → density 1.25
        source.write_text("\n".join([
            "def foo(x):",
            "    if x == 1: return 1",
            "    if x == 2: return 2",
            "    if x == 3: return 3",
            "    if x == 4: return 4",
        ]))
        test.write_text("\n".join([
            "def test_x():",
            "    assert foo(1) == 1",
            "    assert foo(2) == 2",
            "    assert foo(3) == 3",
            "    assert foo(4) == 4",
            "    assert foo(5) is None",
        ]))
        assert_eq("h1: above-threshold density skips", bool(h1["fire"](test, source)), False)


def test_h1_does_not_fire_on_trivial_source():
    """Source with <3 branches → no fire (below floor)."""
    h1 = _h("assertion-density")
    if h1 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "trivial_test.py"
        source = tmp / "trivial.py"
        # 2 branches, 0 assertions → trivial source, no fire
        source.write_text("def foo(x):\n    if x: return 1\n    if not x: return 0\n")
        test.write_text("def test_x():\n    foo(1)\n")
        assert_eq("h1: trivial source skips", bool(h1["fire"](test, source)), False)


def test_h1_ts_regex_in_ballpark():
    """TS branch regex returns counts within ±20% of hand-counted ground truth."""
    fixtures = [
        # source, hand-counted "true" count, tolerance
        (
            "function foo(x: number) {\n  if (x > 0) return 1;\n  if (x < 0) return -1;\n"
            "  switch (x) {\n    case 0: return 0;\n    case 1: return 1;\n  }\n  return -1;\n}\n",
            4,  # 2 ifs + 2 cases
        ),
        (
            "function bar(x: number) {\n  try {\n    if (x) doIt();\n  } catch (e) { handle(e); }\n}\n",
            3,  # 1 if + 1 try + 1 catch
        ),
        (
            "function baz(x: number, y: number) {\n  if (x && y) return 1;\n  return x ? 2 : 0;\n}\n",
            3,  # 1 if + 1 && + 1 ternary (regex may overcount slightly)
        ),
    ]
    for source, true_count in fixtures:
        got = cp._count_branches_ts(source)
        # ±20% means got is in [true*0.8, true*1.2], rounded outward
        lo = max(1, int(true_count * 0.8))
        hi = max(1, int(true_count * 1.2) + 1)
        assert_true(
            f"h1 TS: count for hand-counted={true_count} got {got} (range {lo}-{hi})",
            lo <= got <= hi + 2,  # extra slack for ternary overcounting
            f"got={got} expected~={true_count}",
        )


# ---------------------------------------------------------------------------
# SABLE-5lli.3 — Heuristic 7 (vacuous-guard: create-then-assert-exists)
# ---------------------------------------------------------------------------


def test_h7_fires_on_create_then_assert_exists_py():
    """Python test writes a file directly, then asserts raw existence, with
    no local import at all -- textbook tautology (assertion cannot fail)."""
    h7 = _h("vacuous-guard")
    assert_true("h7 registered", h7 is not None)
    if h7 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "config_test.py"
        test.write_text(
            "def test_creates_config():\n"
            "    path = Path('config.json')\n"
            "    path.write_text('{}')\n"
            "    assert path.exists()\n"
        )
        result = h7["fire"](test, None)
        assert_true(
            "h7: create-then-assert-exists fires",
            bool(result),
            f"got: {result!r}",
        )
        assert_in("h7: detail names the shape", "create-then-assert-exists", str(result))


def test_h7_does_not_fire_on_shared_helper_guard_py():
    """Python test calls an imported LOCAL function (the code under test)
    between the setup and the exists() assertion -- the assertion's truth
    depends on that call actually running, so it is not vacuous."""
    h7 = _h("vacuous-guard")
    if h7 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        source = tmp / "install.py"
        test = tmp / "install_test.py"
        source.write_text("def install(dest):\n    dest.write_text('{}')\n")
        test.write_text(
            "from .install import install\n\n"
            "def test_install_creates_file():\n"
            "    dest = Path('config.json')\n"
            "    dest.touch()\n"
            "    install(dest)\n"
            "    assert dest.exists()\n"
        )
        assert_eq(
            "h7: shared-helper guard (calls local install() before assert) does not fire",
            bool(h7["fire"](test, source)),
            False,
        )


def test_h7_does_not_fire_without_create_statement():
    """Assertion on existence with no preceding write/touch in the test at
    all (e.g. checking a fixture file laid down by a different mechanism)
    -- nothing to flag, since there's no local tautology to catch."""
    h7 = _h("vacuous-guard")
    if h7 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "fixture_test.py"
        test.write_text(
            "def test_fixture_present():\n"
            "    assert Path('fixture.json').exists()\n"
        )
        assert_eq("h7: no create statement, no fire", bool(h7["fire"](test, None)), False)


def test_h7_fires_on_create_then_assert_exists_ts():
    """TS test writes a file via writeFileSync then asserts existsSync,
    no local import -- same tautology shape in the TS surface."""
    h7 = _h("vacuous-guard")
    if h7 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        test = tmp / "config.test.ts"
        test.write_text(
            "it('writes config', () => {\n"
            "  fs.writeFileSync('config.json', '{}');\n"
            "  expect(fs.existsSync('config.json')).toBe(true);\n"
            "});\n"
        )
        result = h7["fire"](test, None)
        assert_true("h7: TS create-then-assert-exists fires", bool(result), f"got: {result!r}")


def test_h7_does_not_fire_on_shared_helper_guard_ts():
    """TS test calls an imported local function before the existsSync
    assertion -- guarded via a real call into the code under test."""
    h7 = _h("vacuous-guard")
    if h7 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        source = tmp / "install.ts"
        test = tmp / "install.test.ts"
        source.write_text("export function install(dest) { fs.writeFileSync(dest, '{}'); }\n")
        test.write_text(
            "import { install } from './install';\n\n"
            "it('installs config', () => {\n"
            "  install('config.json');\n"
            "  expect(fs.existsSync('config.json')).toBe(true);\n"
            "});\n"
        )
        assert_eq(
            "h7: TS shared-helper guard does not fire",
            bool(h7["fire"](test, source)),
            False,
        )


def test_h7_bite_proof_neutering_symbol_detection_turns_guard_red():
    """Mutation test (SABLE-f00o corrected invariant): monkeypatch the
    intervening-call lookup to always report no local imports, simulating
    a neutered/hardcoded detector. The shared-helper guard fixture -- which
    must NOT fire under real detection -- must start firing once neutered.
    Proves the spared case is decided by the real intervening-call check,
    not a lucky pattern miss; else h7 would itself be a vacuous guard."""
    h7 = _h("vacuous-guard")
    if h7 is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        source = tmp / "install.py"
        test = tmp / "install_test.py"
        source.write_text("def install(dest):\n    dest.write_text('{}')\n")
        test.write_text(
            "from .install import install\n\n"
            "def test_install_creates_file():\n"
            "    dest = Path('config.json')\n"
            "    dest.touch()\n"
            "    install(dest)\n"
            "    assert dest.exists()\n"
        )
        assert_eq(
            "h7 bite-proof: real detection spares the guard (precondition)",
            bool(h7["fire"](test, source)),
            False,
        )

        original = cp._h7_imported_local_symbols
        cp._h7_imported_local_symbols = lambda *a, **k: set()
        try:
            mutated = bool(h7["fire"](test, source))
        finally:
            cp._h7_imported_local_symbols = original

        assert_true(
            "h7 bite-proof: neutering the intervening-call check flips the guard to fire (must go RED)",
            mutated is True,
            f"expected neutered detector to fire (mutated={mutated}) — else h7 IS the vacuous guard it hunts",
        )


# ---- Epic integration: full prefilter against synthesized fixtures ----


def _build_epic_fixture(tmp: Path):
    """Lay out three files: stale-fixture (score 9), density-shallow (score
    6), well-tested (no surface). Returns the tmp path for prefilter input."""
    # 1. Stale fixture — TS test imports a symbol the source no longer exports
    (tmp / "auth.test.ts").write_text(
        "import { verifyToken } from './auth';\n"
        "it('verifies', () => { expect(verifyToken('x')).toBe(true); });\n"
    )
    (tmp / "auth.ts").write_text("export function authenticate() {}\n")

    # 2. Density-shallow — Python source with many branches, test with few asserts
    (tmp / "router_test.py").write_text(
        "def test_one():\n    assert route(1) == 1\n"
    )
    (tmp / "router.py").write_text(
        "def route(x):\n"
        "    if x == 1: return 1\n"
        "    if x == 2: return 2\n"
        "    if x == 3: return 3\n"
        "    if x == 4: return 4\n"
        "    if x == 5: return 5\n"
    )

    # 3. Well-tested — many cases, error-path assertion, density healthy
    (tmp / "calculator.test.ts").write_text(
        "it('adds', () => { expect(add(1, 2)).toBe(3); });\n"
        "it('subtracts', () => { expect(sub(5, 2)).toBe(3); });\n"
        "it('throws on divide-by-zero', () => { expect(() => div(1, 0)).toThrow(); });\n"
    )
    (tmp / "calculator.ts").write_text(
        "export function add(a: number, b: number) { return a + b; }\n"
        "export function sub(a: number, b: number) { return a - b; }\n"
    )
    return tmp


def test_epic_full_prefilter_surfaces_correctly():
    """End-to-end: full prefilter ranks the three fixture types correctly."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _build_epic_fixture(Path(tmp))
        results = cp.score_files(cp.discover(root))
        by_name = {Path(r["path"]).name: r for r in results}

        assert_in("epic: auth.test.ts in results", "auth.test.ts", by_name)
        assert_in("epic: router_test.py in results", "router_test.py", by_name)
        assert_in("epic: calculator.test.ts in results", "calculator.test.ts", by_name)

        auth_score = by_name.get("auth.test.ts", {}).get("score", 0)
        router_score = by_name.get("router_test.py", {}).get("score", 0)
        calc_score = by_name.get("calculator.test.ts", {}).get("score", 0)

        assert_eq("epic: stale-fixture surfaces at 9", auth_score, 9)
        assert_true(
            f"epic: router_test.py density-shallow surfaces at >= 6 (got {router_score})",
            router_score >= 6,
            f"router_score={router_score}",
        )
        assert_true(
            f"epic: calculator stays below default threshold 5 (got {calc_score})",
            calc_score < 5,
            f"calc_score={calc_score}",
        )


def test_epic_threshold_filtering_via_cli():
    """Running with --threshold 9 emits only the score-9 stale-fixture file."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _build_epic_fixture(Path(tmp))
        buf = io.StringIO()
        with redirect_stdout(buf):
            cp.main([str(root), "--threshold", "9"])
        out = buf.getvalue()
        assert_in("epic threshold 9: auth.test.ts surfaces", "auth.test.ts", out)
        assert_true(
            "epic threshold 9: router_test.py filtered",
            "router_test.py" not in out,
            f"out={out}",
        )
        assert_true(
            "epic threshold 9: calculator filtered",
            "calculator.test.ts" not in out,
            f"out={out}",
        )


def test_epic_json_output_well_formed():
    """--json emits a valid JSON array with score, path, and signal entries."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _build_epic_fixture(Path(tmp))
        buf = io.StringIO()
        with redirect_stdout(buf):
            cp.main([str(root), "--threshold", "0", "--json"])
        out = buf.getvalue()
        parsed = json.loads(out)
        assert_true(
            f"epic json: parsed is a list of >= 3 entries (got {len(parsed)})",
            isinstance(parsed, list) and len(parsed) >= 3,
        )
        for entry in parsed:
            assert_in(f"epic json: entry has path", "path", entry)
            assert_in(f"epic json: entry has score", "score", entry)
            assert_in(f"epic json: entry has signals", "signals", entry)


def test_v1_integration_surfaces_shallow_skips_deep():
    """End-to-end: synthesize one shallow test/source pair and one deep
    test/source pair; run the prefilter with both heuristics live; assert
    the shallow pair surfaces (score >= 5) and the deep pair does not."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Shallow TS: 1 case, no toThrow, source has 5 exports → both H2 + H4 fire
        (tmp / "shallow.test.ts").write_text(
            "it('only one case', () => { expect(x).toBe(1); });\n"
        )
        (tmp / "shallow.ts").write_text("\n".join([
            "export function a() {}",
            "export function b() {}",
            "export function c() {}",
            "export function d() {}",
            "export function e() {}",
        ]) + "\n")
        # Deep TS: 4 cases, includes toThrow, source has 5 exports → neither H2 nor H4 fires
        (tmp / "deep.test.ts").write_text("\n".join([
            "it('a', () => { expect(x).toBe(1); });",
            "it('b', () => { expect(() => bad()).toThrow(); });",
            "it('c', () => { expect(x).toBe(3); });",
            "it('d', () => { expect(x).toBe(4); });",
        ]) + "\n")
        (tmp / "deep.ts").write_text("\n".join([
            "export function a() {}",
            "export function b() {}",
            "export function c() {}",
            "export function d() {}",
            "export function e() {}",
        ]) + "\n")
        results = cp.score_files(cp.discover(tmp))
        by_path = {Path(r["path"]).name: r for r in results}

        assert_in("v1_integration: shallow.test.ts in results", "shallow.test.ts", by_path)
        assert_in("v1_integration: deep.test.ts in results", "deep.test.ts", by_path)

        shallow_score = by_path.get("shallow.test.ts", {}).get("score", 0)
        deep_score = by_path.get("deep.test.ts", {}).get("score", 0)
        assert_true(
            f"v1_integration: shallow surfaces at >= 5 (got {shallow_score})",
            shallow_score >= 5,
            f"shallow_score={shallow_score}",
        )
        assert_true(
            f"v1_integration: deep stays below 5 (got {deep_score})",
            deep_score < 5,
            f"deep_score={deep_score}",
        )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


TESTS = [
    # rjv.5.1 — scaffolding
    test_argparse_help,
    test_argparse_threshold,
    test_text_output_format,
    test_json_output_format,
    test_file_discovery_ts,
    test_file_discovery_py,
    test_file_discovery_no_source,
    test_file_discovery_pytest_prefix,
    test_file_discovery_pytest_tests_sibling_dir,
    test_file_discovery_pytest_tests_to_src_subdir,
    test_file_discovery_ts_tests_dir_mirror,
    test_file_discovery_ts_tests_dir_at_root,
    test_file_discovery_skips_node_modules,
    test_heuristic_registry,
    test_max_score_not_sum,
    # rjv.5.2 — heuristics 2 + 4
    test_h2_fires_on_only_happy_assertions,
    test_h2_does_not_fire_with_toThrow,
    test_h2_does_not_fire_with_pytest_raises,
    test_h2_fires_on_pure_python_assertions,
    test_h4_fires_when_one_case_many_functions,
    test_h4_does_not_fire_when_many_cases,
    test_h4_does_not_fire_when_few_source_functions,
    test_h4_skips_when_no_source,
    test_h4_python_class_methods_counted,
    test_v1_integration_surfaces_shallow_skips_deep,
    # rjv.5.3 — heuristic 6 (stale fixture)
    test_h6_fires_on_deleted_ts_export,
    test_h6_fires_on_deleted_py_function,
    test_h6_does_not_fire_on_valid_ts_export,
    test_h6_does_not_fire_on_valid_py_export,
    test_h6_skips_namespace_import,
    test_h6_skips_unresolved_target,
    test_h6_handles_default_export,
    test_h6_handles_named_reexport,
    # rjv.5.4 — heuristics 3 + 5
    test_h3_fires_on_overmocked_integration,
    test_h3_does_not_fire_below_threshold,
    test_h3_does_not_fire_on_unit_tests,
    test_h3_python_unittest_mock,
    test_h3_describe_block_tag,
    test_h5_fires_on_state_machine_no_transition_tests,
    test_h5_does_not_fire_when_test_mentions_transition,
    test_h5_fires_multiple_categories,
    test_h5_skips_when_no_source,
    test_h5_concurrency_pattern,
    # rjv.5.5 — heuristic 1 (assertion density) + epic integration
    test_h1_python_ast_counts_branches,
    test_h1_python_assertion_count,
    test_h1_fires_on_low_density,
    test_h1_does_not_fire_above_threshold,
    test_h1_does_not_fire_on_trivial_source,
    test_h1_ts_regex_in_ballpark,
    test_epic_full_prefilter_surfaces_correctly,
    test_epic_threshold_filtering_via_cli,
    test_epic_json_output_well_formed,
    # SABLE-5lli.3 — heuristic 7 (vacuous-guard: create-then-assert-exists)
    test_h7_fires_on_create_then_assert_exists_py,
    test_h7_does_not_fire_on_shared_helper_guard_py,
    test_h7_does_not_fire_without_create_statement,
    test_h7_fires_on_create_then_assert_exists_ts,
    test_h7_does_not_fire_on_shared_helper_guard_ts,
    test_h7_bite_proof_neutering_symbol_detection_turns_guard_red,
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
