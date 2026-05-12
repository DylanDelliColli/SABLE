#!/usr/bin/env python3
"""test_gaudi_prefilter — unit tests for the Gaudi prefilter.

Tests the same scaffolding shape as test_columbo_prefilter.py (argparse,
output formatting, file discovery, heuristic-registry contract), plus
the six Gaudi-specific heuristics: long-method, large-class,
long-parameter-list, bidirectional-import, god-module, shallow-pass-through.

Run with:

  python3 bin/test_gaudi_prefilter.py
  python3 -m pytest bin/test_gaudi_prefilter.py

Zero pytest dependency for direct invocation; pytest-discoverable too.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PREFILTER_PATH = SCRIPT_DIR / "gaudi-prefilter.py"
spec = importlib.util.spec_from_file_location("gaudi_prefilter", PREFILTER_PATH)
gp = importlib.util.module_from_spec(spec)
sys.modules["gaudi_prefilter"] = gp
spec.loader.exec_module(gp)


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


def with_heuristics(heuristics):
    def wrap(fn):
        def run():
            saved = list(gp.HEURISTICS)
            gp.HEURISTICS.clear()
            gp.HEURISTICS.extend(heuristics)
            try:
                fn()
            finally:
                gp.HEURISTICS.clear()
                gp.HEURISTICS.extend(saved)
        return run
    return wrap


# ---------------------------------------------------------------------------
# CLI / argparse / output format
# ---------------------------------------------------------------------------


def test_argparse_help():
    buf = io.StringIO()
    code = None
    try:
        with redirect_stdout(buf):
            gp.main(["--help"])
    except SystemExit as e:
        code = e.code
    assert_eq("argparse_help: exits with 0", code, 0)
    assert_in("argparse_help: prints usage", "usage:", buf.getvalue().lower())


def test_argparse_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "low.ts").write_text("// stub\n")

        @with_heuristics([
            {"name": "low", "score": 3, "fire": lambda p, c: True},
        ])
        def run():
            buf = io.StringIO()
            with redirect_stdout(buf):
                gp.main([str(tmp), "--threshold", "5"])
            assert_eq(
                "threshold: low-scored file excluded",
                buf.getvalue().strip(),
                "",
            )
        run()


def test_text_output_format():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "x.ts").write_text("// stub\n")

        @with_heuristics([
            {"name": "stub", "score": 7, "fire": lambda p, c: True},
        ])
        def run():
            buf = io.StringIO()
            with redirect_stdout(buf):
                gp.main([str(tmp)])
            out = buf.getvalue()
            assert_in("text_output: score appears", "score=7", out)
            assert_in("text_output: signal name appears", "stub", out)
        run()


def test_json_output_format():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "x.ts").write_text("// stub\n")

        @with_heuristics([
            {"name": "stub", "score": 6, "fire": lambda p, c: True},
        ])
        def run():
            buf = io.StringIO()
            with redirect_stdout(buf):
                gp.main([str(tmp), "--json"])
            data = json.loads(buf.getvalue())
            assert_eq("json: one entry", len(data), 1)
            assert_eq("json: score=6", data[0]["score"], 6)
            assert_eq("json: signal name", data[0]["signals"][0]["name"], "stub")
        run()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discovery_finds_source_files():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "foo.py").write_text("# stub\n")
        (tmp / "bar.ts").write_text("// stub\n")
        (tmp / "baz.tsx").write_text("// stub\n")
        files = gp.discover(tmp)
        names = sorted(f.name for f in files)
        assert_eq(
            "discovery: finds .py/.ts/.tsx files",
            names,
            ["bar.ts", "baz.tsx", "foo.py"],
        )


def test_discovery_skips_test_files():
    """Gaudi audits source, not tests — test files shouldn't appear."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "real.py").write_text("# stub\n")
        (tmp / "test_real.py").write_text("# stub\n")
        (tmp / "real.test.ts").write_text("// stub\n")
        (tmp / "real.ts").write_text("// stub\n")
        files = gp.discover(tmp)
        names = sorted(f.name for f in files)
        assert_eq(
            "discovery: skips test files",
            names,
            ["real.py", "real.ts"],
        )


def test_discovery_skips_vendored_dirs():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "node_modules" / "pkg").mkdir(parents=True)
        (tmp / "node_modules" / "pkg" / "vendor.ts").write_text("// stub\n")
        (tmp / ".venv" / "lib").mkdir(parents=True)
        (tmp / ".venv" / "lib" / "vendor.py").write_text("# stub\n")
        (tmp / ".next").mkdir()
        (tmp / ".next" / "build.js").write_text("// stub\n")
        (tmp / "ok.ts").write_text("// stub\n")
        files = gp.discover(tmp)
        names = sorted(f.name for f in files)
        assert_eq(
            "discovery: skips node_modules/.venv/.next",
            names,
            ["ok.ts"],
        )


# ---------------------------------------------------------------------------
# Heuristic 1 — long-method
# ---------------------------------------------------------------------------


def test_long_method_fires_python():
    """Python function with > 50 lines fires long-method."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        big_body = "\n".join(f"    x = {i}" for i in range(80))
        (tmp / "big.py").write_text(f"def big():\n{big_body}\n")
        result = gp.score_file(tmp / "big.py")
        signal_names = [s["name"] for s in result["signals"]]
        assert_in("long_method_py: fires", "long-method", signal_names)


def test_long_method_does_not_fire_short_python():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "small.py").write_text("def small():\n    return 1\n")
        result = gp.score_file(tmp / "small.py")
        signal_names = [s["name"] for s in result["signals"]]
        assert_true(
            "long_method_py: does not fire on short fn",
            "long-method" not in signal_names,
        )


def test_long_method_fires_typescript():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        body = "\n".join(f"  const x{i} = {i};" for i in range(80))
        (tmp / "big.ts").write_text(f"function big() {{\n{body}\n}}\n")
        result = gp.score_file(tmp / "big.ts")
        signal_names = [s["name"] for s in result["signals"]]
        assert_in("long_method_ts: fires", "long-method", signal_names)


# ---------------------------------------------------------------------------
# Heuristic 2 — large-class
# ---------------------------------------------------------------------------


def test_large_class_fires_python_method_count():
    """Python class with >10 methods fires large-class."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        methods = "\n".join(
            f"    def m{i}(self):\n        return {i}" for i in range(12)
        )
        (tmp / "big.py").write_text(f"class Big:\n{methods}\n")
        result = gp.score_file(tmp / "big.py")
        signal_names = [s["name"] for s in result["signals"]]
        assert_in("large_class_py: fires on >10 methods", "large-class", signal_names)


def test_large_class_does_not_fire_small_python():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "small.py").write_text(
            "class Small:\n    def a(self): pass\n    def b(self): pass\n"
        )
        result = gp.score_file(tmp / "small.py")
        signal_names = [s["name"] for s in result["signals"]]
        assert_true(
            "large_class_py: does not fire on small class",
            "large-class" not in signal_names,
        )


def test_large_class_fires_typescript_method_count():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        methods = "\n".join(f"  m{i}() {{}}" for i in range(12))
        (tmp / "big.ts").write_text(f"class Big {{\n{methods}\n}}\n")
        result = gp.score_file(tmp / "big.ts")
        signal_names = [s["name"] for s in result["signals"]]
        assert_in("large_class_ts: fires on >10 methods", "large-class", signal_names)


# ---------------------------------------------------------------------------
# Heuristic 3 — long-parameter-list
# ---------------------------------------------------------------------------


def test_long_param_list_fires_python():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "fn.py").write_text("def f(a, b, c, d, e, f, g): pass\n")
        result = gp.score_file(tmp / "fn.py")
        signal_names = [s["name"] for s in result["signals"]]
        assert_in("long_params_py: fires on 7 params", "long-parameter-list", signal_names)


def test_long_param_list_does_not_fire_short_python():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "fn.py").write_text("def f(a, b, c): pass\n")
        result = gp.score_file(tmp / "fn.py")
        signal_names = [s["name"] for s in result["signals"]]
        assert_true(
            "long_params_py: does not fire on 3 params",
            "long-parameter-list" not in signal_names,
        )


def test_long_param_list_fires_typescript():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "fn.ts").write_text(
            "function f(a: number, b: number, c: number, d: number, e: number, f: number, g: number) {}\n"
        )
        result = gp.score_file(tmp / "fn.ts")
        signal_names = [s["name"] for s in result["signals"]]
        assert_in("long_params_ts: fires", "long-parameter-list", signal_names)


# ---------------------------------------------------------------------------
# Heuristic 4 — bidirectional-import
# ---------------------------------------------------------------------------


def test_bidirectional_import_fires_ts():
    """A imports B; B imports A — fires on both files."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "a.ts").write_text("import { b } from './b';\nexport const a = 1;\n")
        (tmp / "b.ts").write_text("import { a } from './a';\nexport const b = 2;\n")
        results = gp.score_files(gp.discover(tmp))
        smelly = [r for r in results if any(s["name"] == "bidirectional-import" for s in r["signals"])]
        assert_eq("bidir_ts: both files fire", len(smelly), 2)


def test_bidirectional_import_does_not_fire_one_way():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "a.ts").write_text("import { b } from './b';\nexport const a = 1;\n")
        (tmp / "b.ts").write_text("export const b = 2;\n")
        results = gp.score_files(gp.discover(tmp))
        smelly = [r for r in results if any(s["name"] == "bidirectional-import" for s in r["signals"])]
        assert_eq("bidir_ts: one-way import doesn't fire", len(smelly), 0)


def test_bidirectional_import_fires_python():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "a.py").write_text("from .b import b\ndef a(): return b()\n")
        (tmp / "b.py").write_text("from .a import a\ndef b(): return a()\n")
        results = gp.score_files(gp.discover(tmp))
        smelly = [r for r in results if any(s["name"] == "bidirectional-import" for s in r["signals"])]
        assert_eq("bidir_py: both files fire", len(smelly), 2)


# ---------------------------------------------------------------------------
# Heuristic 5 — god-module
# ---------------------------------------------------------------------------


def test_god_module_fires():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lines = ["// pad"] * 450
        exports = "\n".join(f"export const e{i} = {i};" for i in range(20))
        (tmp / "god.ts").write_text("\n".join(lines) + "\n" + exports + "\n")
        result = gp.score_file(tmp / "god.ts")
        signal_names = [s["name"] for s in result["signals"]]
        assert_in("god_module: fires on big+many-exports", "god-module", signal_names)


def test_god_module_does_not_fire_short_file():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        exports = "\n".join(f"export const e{i} = {i};" for i in range(20))
        (tmp / "many_exports.ts").write_text(exports + "\n")
        result = gp.score_file(tmp / "many_exports.ts")
        signal_names = [s["name"] for s in result["signals"]]
        assert_true(
            "god_module: does not fire on short file with many exports",
            "god-module" not in signal_names,
        )


def test_god_module_does_not_fire_few_exports():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lines = ["// pad"] * 450
        (tmp / "long_one_export.ts").write_text(
            "\n".join(lines) + "\nexport const e = 1;\n"
        )
        result = gp.score_file(tmp / "long_one_export.ts")
        signal_names = [s["name"] for s in result["signals"]]
        assert_true(
            "god_module: does not fire on long file with one export",
            "god-module" not in signal_names,
        )


# ---------------------------------------------------------------------------
# Heuristic 6 — shallow-pass-through
# ---------------------------------------------------------------------------


def test_shallow_pass_through_fires_ts():
    """Tiny file with one exported function that just delegates fires."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "thin.ts").write_text(
            "import { realWork } from './other';\n"
            "export function doIt(x) { return realWork(x); }\n"
        )
        result = gp.score_file(tmp / "thin.ts")
        signal_names = [s["name"] for s in result["signals"]]
        assert_in("shallow_ts: fires", "shallow-pass-through", signal_names)


def test_shallow_pass_through_does_not_fire_substantive():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        body = "\n".join(f"  const x{i} = {i};" for i in range(20))
        (tmp / "substantial.ts").write_text(
            f"export function doIt(x) {{\n{body}\n  return x;\n}}\n"
        )
        result = gp.score_file(tmp / "substantial.ts")
        signal_names = [s["name"] for s in result["signals"]]
        assert_true(
            "shallow_ts: does not fire on substantial function",
            "shallow-pass-through" not in signal_names,
        )


# ---------------------------------------------------------------------------
# Score combination — max not sum
# ---------------------------------------------------------------------------


def test_max_score_not_sum():
    """Two heuristics firing — final score is max, not sum."""
    @with_heuristics([
        {"name": "five", "score": 5, "fire": lambda p, c: True},
        {"name": "eight", "score": 8, "fire": lambda p, c: True},
    ])
    def run():
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "x.ts").write_text("// stub\n")
            results = gp.score_files(gp.discover(tmp))
            assert_eq("max_not_sum: final score is 8, not 13", results[0]["score"], 8)
            names = sorted(s["name"] for s in results[0]["signals"])
            assert_eq("max_not_sum: both signals listed", names, ["eight", "five"])
    run()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


TESTS = [
    test_argparse_help,
    test_argparse_threshold,
    test_text_output_format,
    test_json_output_format,
    test_discovery_finds_source_files,
    test_discovery_skips_test_files,
    test_discovery_skips_vendored_dirs,
    test_long_method_fires_python,
    test_long_method_does_not_fire_short_python,
    test_long_method_fires_typescript,
    test_large_class_fires_python_method_count,
    test_large_class_does_not_fire_small_python,
    test_large_class_fires_typescript_method_count,
    test_long_param_list_fires_python,
    test_long_param_list_does_not_fire_short_python,
    test_long_param_list_fires_typescript,
    test_bidirectional_import_fires_ts,
    test_bidirectional_import_does_not_fire_one_way,
    test_bidirectional_import_fires_python,
    test_god_module_fires,
    test_god_module_does_not_fire_short_file,
    test_god_module_does_not_fire_few_exports,
    test_shallow_pass_through_fires_ts,
    test_shallow_pass_through_does_not_fire_substantive,
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
