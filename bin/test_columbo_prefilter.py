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
