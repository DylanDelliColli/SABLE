#!/usr/bin/env python3
"""gaudi-prefilter — static-analysis code-smell ranker.

Triages audit-mode targets before Gaudi's interview budget gets spent.
Walks source files under a path, runs each through a registry of code-smell
heuristics from Fowler's catalog, and prints a ranked list of files most
likely to need architecture review.

Models columbo-prefilter.py's registry pattern, but operates on *source*
files only (no test-pair). Heuristics that need a corpus-wide view (e.g.
bidirectional-import) receive the full file list as a second argument; the
others only see the path being scored.

Usage:
  bin/gaudi-prefilter.py <path>                  # text output, threshold 5
  bin/gaudi-prefilter.py <path> --threshold 7    # only score >= 7
  bin/gaudi-prefilter.py <path> --json           # machine-readable

Heuristic registry — extension contract:

  HEURISTICS entries are dicts:
    {
      "name":   str,                                       # smell label
      "score":  int,                                       # 0-10, fixed
      "fire":   Callable[[Path, dict|None], bool | str],   # (file, corpus) -> fired?
    }

  fire() returns True (fired, no detail), str (fired with detail), or
  False (not fired). Score per file = max(score of triggered heuristics).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

HEURISTICS: list[dict] = []


# ---------------------------------------------------------------------------
# Language + IO helpers
# ---------------------------------------------------------------------------


def _lang(path: Path) -> str:
    name = path.name
    if name.endswith((".ts", ".tsx", ".js", ".jsx")):
        return "ts"
    if name.endswith(".py"):
        return "py"
    return "other"


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".py")

# Skip dirs — same closed list Columbo's prefilter uses, mirrored here so the
# two stay in sync if either grows.
_SKIP_DIRS = frozenset({
    "node_modules", ".next", ".nuxt", ".svelte-kit", ".turbo",
    "dist", "build", "out", "target", "coverage",
    ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".git", ".cache", ".tox",
})

# Test-file patterns to skip — Gaudi audits source, not tests.
_TEST_FILE_PATTERNS = [
    re.compile(r"\.test\.[jt]sx?$"),
    re.compile(r"\.spec\.[jt]sx?$"),
    re.compile(r"^test_.*\.py$"),
    re.compile(r"_test\.py$"),
]


def _is_test_file(path: Path) -> bool:
    return any(p.search(path.name) for p in _TEST_FILE_PATTERNS)


def _walk_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        return
    stack: list[Path] = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if entry.name in _SKIP_DIRS:
                        continue
                    stack.append(entry)
                elif entry.is_file():
                    yield entry
            except OSError:
                continue


def discover(path: Path) -> list[Path]:
    """Return source files (no test files, no vendored dirs) under path."""
    out: list[Path] = []
    for f in _walk_files(path):
        if not f.name.endswith(_SOURCE_SUFFIXES):
            continue
        if _is_test_file(f):
            continue
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _signals_for(path: Path, corpus: Optional[dict]) -> list[dict]:
    triggered: list[dict] = []
    for h in HEURISTICS:
        try:
            outcome = h["fire"](path, corpus)
        except Exception:
            continue
        if not outcome:
            continue
        entry = {"name": h["name"], "score": h["score"]}
        if isinstance(outcome, str):
            entry["detail"] = outcome
        triggered.append(entry)
    return triggered


def score_file(path: Path) -> dict:
    """Score a single file with no corpus context.

    Heuristics that need cross-file context (bidirectional-import) won't fire
    here — they only fire under score_files() where the corpus is built.
    """
    signals = _signals_for(path, None)
    score = max((s["score"] for s in signals), default=0)
    return {"path": str(path), "score": score, "signals": signals}


def score_files(files: Iterable[Path]) -> list[dict]:
    """Score a corpus, building cross-file context (import graph) once and
    making it available to corpus-aware heuristics."""
    files = list(files)
    corpus = _build_corpus(files)
    results: list[dict] = []
    for f in files:
        signals = _signals_for(f, corpus)
        score = max((s["score"] for s in signals), default=0)
        results.append({"path": str(f), "score": score, "signals": signals})
    results.sort(key=lambda r: (-r["score"], r["path"]))
    return results


def _build_corpus(files: list[Path]) -> dict:
    """Build the cross-file context heuristics may consult.

    Currently only the import graph for bidirectional-import detection.
    Maps each file to the set of resolved files it imports from.
    """
    imports_by_file: dict[Path, set[Path]] = {}
    for f in files:
        imports_by_file[f] = _resolve_imports(f)
    return {"imports_by_file": imports_by_file}


# ---------------------------------------------------------------------------
# Import resolution (used by bidirectional-import)
# ---------------------------------------------------------------------------

_TS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[\w*{},\s]+\s+from\s+)?|from\s+|require\s*\()['"]([^'"]+)['"]"""
)
_PY_FROM_IMPORT_RE = re.compile(r"^from\s+(\.+\S*|\S+)\s+import\s+", re.MULTILINE)


def _resolve_imports(path: Path) -> set[Path]:
    lang = _lang(path)
    text = _read(path)
    out: set[Path] = set()
    if lang == "ts":
        for m in _TS_IMPORT_RE.finditer(text):
            spec = m.group(1)
            if not spec.startswith("."):
                continue
            target = _resolve_ts_path(path.parent, spec)
            if target:
                out.add(target)
    elif lang == "py":
        for m in _PY_FROM_IMPORT_RE.finditer(text):
            spec = m.group(1)
            if not spec.startswith("."):
                continue
            target = _resolve_py_path(path.parent, spec)
            if target:
                out.add(target)
    return out


def _resolve_ts_path(base: Path, spec: str) -> Optional[Path]:
    try:
        target = (base / spec).resolve()
    except OSError:
        return None
    if target.is_file():
        return target
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        candidate = target.with_suffix(ext)
        if candidate.exists():
            return candidate
    for ext in (".ts", ".tsx"):
        candidate = target / f"index{ext}"
        if candidate.exists():
            return candidate
    return None


def _resolve_py_path(base: Path, spec: str) -> Optional[Path]:
    parts = spec.lstrip(".").split(".") if spec.lstrip(".") else []
    # Number of leading dots = how many parents to walk up
    leading_dots = len(spec) - len(spec.lstrip("."))
    cur = base
    for _ in range(leading_dots - 1):
        cur = cur.parent
    if parts:
        target = cur.joinpath(*parts)
    else:
        target = cur
    candidate = target.with_suffix(".py")
    if candidate.exists():
        return candidate
    candidate = target / "__init__.py"
    if candidate.exists():
        return candidate
    return None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def format_text(results: list[dict]) -> str:
    if not results:
        return ""
    width = max(len(r["path"]) for r in results)
    lines = []
    for r in results:
        sigs = ",".join(
            f"{s['name']}:{s['detail']}" if s.get("detail") else s["name"]
            for s in r["signals"]
        ) or "-"
        lines.append(f"{r['path'].ljust(width)}  score={r['score']}  signals={sigs}")
    return "\n".join(lines)


def format_json(results: list[dict]) -> str:
    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Heuristic 1 — long-method (score 6)
# ---------------------------------------------------------------------------
# Fires when a function exceeds 50 lines. Python: stdlib ast precise count.
# TS: regex-walk counting lines from `function name(`/`name = (...) =>` to
# the matching closing brace. Brace counting is approximate; documented lossy.

_LONG_METHOD_THRESHOLD = 50


def _h_long_method(path: Path, corpus: Optional[dict]):
    lang = _lang(path)
    text = _read(path)
    if lang == "py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(node, "end_lineno", node.lineno)
                if end - node.lineno > _LONG_METHOD_THRESHOLD:
                    return node.name
        return False
    if lang == "ts":
        return _ts_longest_function(text) > _LONG_METHOD_THRESHOLD
    return False


_TS_FN_START = re.compile(
    r"(?m)(?:^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\("
    r"|^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>\s*\{)"
)


def _ts_longest_function(text: str) -> int:
    """Return the line count of the longest TS function in text. Approximate
    via brace counting from each function start."""
    lines = text.split("\n")
    longest = 0
    for m in _TS_FN_START.finditer(text):
        start_idx = m.start()
        start_line = text[:start_idx].count("\n")
        # find the opening brace from the match
        brace_start = text.find("{", m.end() - 1)
        if brace_start < 0:
            continue
        depth = 0
        i = brace_start
        while i < len(text):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end_line = text[:i].count("\n")
                    longest = max(longest, end_line - start_line)
                    break
            i += 1
    return longest


HEURISTICS.append({"name": "long-method", "score": 6, "fire": _h_long_method})


# ---------------------------------------------------------------------------
# Heuristic 2 — large-class (score 7)
# ---------------------------------------------------------------------------
# Fires when a class has > 10 methods OR spans > 200 lines.

_LARGE_CLASS_METHOD_THRESHOLD = 10
_LARGE_CLASS_LINE_THRESHOLD = 200


def _h_large_class(path: Path, corpus: Optional[dict]):
    lang = _lang(path)
    text = _read(path)
    if lang == "py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = sum(
                    1 for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
                end = getattr(node, "end_lineno", node.lineno)
                lines = end - node.lineno
                if methods > _LARGE_CLASS_METHOD_THRESHOLD or lines > _LARGE_CLASS_LINE_THRESHOLD:
                    return node.name
        return False
    if lang == "ts":
        return _ts_largest_class_method_count(text) > _LARGE_CLASS_METHOD_THRESHOLD
    return False


_TS_CLASS_START = re.compile(r"(?m)^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)")
_TS_METHOD_LINE = re.compile(
    r"(?m)^\s+(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*"
    r"(\w+)\s*\([^)]*\)\s*[:{]"
)
_TS_RESERVED_BLOCKS = {"if", "for", "while", "switch", "return", "throw", "do", "catch", "else"}


def _ts_largest_class_method_count(text: str) -> int:
    largest = 0
    for m in _TS_CLASS_START.finditer(text):
        brace_start = text.find("{", m.end())
        if brace_start < 0:
            continue
        depth = 0
        i = brace_start
        end_idx = len(text)
        while i < len(text):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break
            i += 1
        body = text[brace_start + 1:end_idx]
        method_count = sum(
            1 for mm in _TS_METHOD_LINE.finditer(body)
            if mm.group(1) not in _TS_RESERVED_BLOCKS
        )
        largest = max(largest, method_count)
    return largest


HEURISTICS.append({"name": "large-class", "score": 7, "fire": _h_large_class})


# ---------------------------------------------------------------------------
# Heuristic 3 — long-parameter-list (score 6)
# ---------------------------------------------------------------------------
# Fires when any function in the file has > 5 parameters.

_LONG_PARAMS_THRESHOLD = 5


def _h_long_params(path: Path, corpus: Optional[dict]):
    lang = _lang(path)
    text = _read(path)
    if lang == "py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                count = len(node.args.args) + len(node.args.kwonlyargs)
                # Skip "self"/"cls" if present
                if node.args.args and node.args.args[0].arg in ("self", "cls"):
                    count -= 1
                if count > _LONG_PARAMS_THRESHOLD:
                    return node.name
        return False
    if lang == "ts":
        return _ts_any_long_param_list(text)
    return False


def _ts_any_long_param_list(text: str) -> bool:
    """Walk function-start matches and count commas in the param list."""
    for m in re.finditer(r"function\s+(\w+)\s*\(([^)]*)\)", text):
        params = m.group(2).strip()
        if not params:
            continue
        # Count top-level commas (ignore generics like Map<K, V>)
        depth = 0
        count = 1
        for c in params:
            if c in "<({":
                depth += 1
            elif c in ">)}":
                depth -= 1
            elif c == "," and depth == 0:
                count += 1
        if count > _LONG_PARAMS_THRESHOLD:
            return True
    return False


HEURISTICS.append({"name": "long-parameter-list", "score": 6, "fire": _h_long_params})


# ---------------------------------------------------------------------------
# Heuristic 4 — bidirectional-import (score 9)
# ---------------------------------------------------------------------------
# Fires when this file imports another file that imports back. Direct cycle
# only (length 2); transitive cycles deferred to v2.

def _h_bidirectional_import(path: Path, corpus: Optional[dict]):
    if corpus is None:
        return False
    graph: dict[Path, set[Path]] = corpus.get("imports_by_file", {})
    resolved_self = None
    try:
        resolved_self = path.resolve()
    except OSError:
        return False
    targets = graph.get(path) or graph.get(resolved_self) or set()
    for target in targets:
        try:
            target_resolved = target.resolve()
        except OSError:
            continue
        reverse_targets = graph.get(target) or graph.get(target_resolved) or set()
        for rt in reverse_targets:
            try:
                if rt.resolve() == resolved_self:
                    return target.name
            except OSError:
                continue
    return False


HEURISTICS.append({"name": "bidirectional-import", "score": 9, "fire": _h_bidirectional_import})


# ---------------------------------------------------------------------------
# Heuristic 5 — god-module (score 8)
# ---------------------------------------------------------------------------
# Fires when a file is > 400 lines AND exports > 15 symbols.

_GOD_LINE_THRESHOLD = 400
_GOD_EXPORT_THRESHOLD = 15

_TS_EXPORT_RE = re.compile(
    r"(?m)^\s*export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function|class|const|let|var|type|interface|enum)\s+\w+"
)
_TS_EXPORT_BRACE_RE = re.compile(r"export\s*\{([^}]+)\}")


def _h_god_module(path: Path, corpus: Optional[dict]):
    text = _read(path)
    line_count = text.count("\n")
    if line_count < _GOD_LINE_THRESHOLD:
        return False
    lang = _lang(path)
    if lang == "ts":
        export_count = len(_TS_EXPORT_RE.findall(text))
        for m in _TS_EXPORT_BRACE_RE.finditer(text):
            export_count += sum(1 for x in m.group(1).split(",") if x.strip())
        if export_count > _GOD_EXPORT_THRESHOLD:
            return f"{line_count}lines,{export_count}exports"
        return False
    if lang == "py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False
        export_count = sum(
            1 for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        # Module-level assignments to non-underscore names count too
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and not t.id.startswith("_"):
                        export_count += 1
        if export_count > _GOD_EXPORT_THRESHOLD:
            return f"{line_count}lines,{export_count}exports"
    return False


HEURISTICS.append({"name": "god-module", "score": 8, "fire": _h_god_module})


# ---------------------------------------------------------------------------
# Heuristic 6 — shallow-pass-through (score 7)
# ---------------------------------------------------------------------------
# Fires on tiny files (< 30 lines) with a single exported function/class
# whose body is < 5 lines and calls into another module. Speculative
# Generality + Lazy Class + Middle Man territory. Conservative — the
# combined preconditions make false positives unlikely.

_SHALLOW_FILE_LINE_THRESHOLD = 30
_SHALLOW_BODY_LINE_THRESHOLD = 5


def _h_shallow_pass_through(path: Path, corpus: Optional[dict]):
    text = _read(path)
    if text.count("\n") > _SHALLOW_FILE_LINE_THRESHOLD:
        return False
    lang = _lang(path)
    if lang == "ts":
        exports = list(_TS_EXPORT_RE.finditer(text))
        if len(exports) != 1:
            return False
        # Find the function body — naive: between first `{` after match and matching `}`
        start = text.find("{", exports[0].end())
        if start < 0:
            return False
        depth = 0
        i = start
        end_idx = len(text)
        while i < len(text):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break
            i += 1
        body = text[start + 1:end_idx]
        body_lines = body.count("\n")
        if body_lines >= _SHALLOW_BODY_LINE_THRESHOLD:
            return False
        # Must reference an imported name (i.e. delegate)
        imports_present = bool(re.search(r"(?m)^import\s|require\s*\(", text))
        return imports_present
    if lang == "py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False
        top_funcs = [
            n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        top_classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        if len(top_funcs) + len(top_classes) != 1:
            return False
        target = (top_funcs + top_classes)[0]
        end = getattr(target, "end_lineno", target.lineno)
        if end - target.lineno >= _SHALLOW_BODY_LINE_THRESHOLD:
            return False
        imports_present = any(
            isinstance(n, (ast.Import, ast.ImportFrom)) for n in tree.body
        )
        return imports_present
    return False


HEURISTICS.append({"name": "shallow-pass-through", "score": 7, "fire": _h_shallow_pass_through})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gaudi-prefilter",
        description="Rank source files by code-smell signals.",
    )
    p.add_argument("path", help="File or directory to scan for source files.")
    p.add_argument("--threshold", type=int, default=5,
                   help="Only emit files scoring >= THRESHOLD (default: 5).")
    p.add_argument("--json", action="store_true", help="Emit JSON.")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    target = Path(args.path)
    if not target.exists():
        sys.stderr.write(f"gaudi-prefilter: path does not exist: {target}\n")
        return 2
    files = discover(target)
    results = score_files(files)
    filtered = [r for r in results if r["score"] >= args.threshold]
    out = format_json(filtered) if args.json else format_text(filtered)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
