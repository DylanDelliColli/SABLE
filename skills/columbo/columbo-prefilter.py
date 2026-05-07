#!/usr/bin/env python3
"""columbo-prefilter — static-analysis test-shallowness ranker.

Triages audit-mode targets before Columbo's interview budget gets spent.
Reads test files (and their paired source files when discoverable) under
a given path, runs each through a registry of shallowness heuristics,
and prints a ranked list of files most likely to be undertested.

Usage:
  bin/columbo-prefilter.py <path>                  # text output, threshold 5
  bin/columbo-prefilter.py <path> --threshold 7    # only score >= 7
  bin/columbo-prefilter.py <path> --json           # machine-readable
  bin/columbo-prefilter.py --help

Heuristic registry — extension contract:

  Each entry in HEURISTICS is a dict:
    {
      "name":  str,                                       # signal label
      "score": int,                                       # 0-10, fixed per heuristic
      "fire":  Callable[[Path, Path|None], bool | str],   # (test, source) -> fired?
    }

  fire() returns:
    - True  → fired (no detail; signal label is just `name`)
    - str   → fired with detail (signal label is `name:detail`, e.g.
              `stale-fixture:processRefund`); falsy strings ("") count as
              not fired
    - False → not fired

  Sibling beads (rjv.5.2-5.5) plug into HEURISTICS by appending entries.
  Per-file scoring: triggered heuristics contribute to the file's signal
  list. Final score = max(h["score"] for triggered) — not sum.

  Note: max — not sum. A single strong signal (e.g. stale-fixture, score 9)
  should outrank a pile of weak signals.

Currently the registry is empty (no heuristics shipped yet). 5.1 ships
the scaffolding; 5.2 adds happy-path-only + single-case-wonder; the
rest follow.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

# Public registry — heuristic beads append to this.
HEURISTICS: list[dict] = []


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def _lang(path: Path) -> str:
    """Return 'ts' for JS/TS files, 'py' for Python, 'other' for the rest."""
    name = path.name
    if name.endswith((".ts", ".tsx", ".js", ".jsx")):
        return "ts"
    if name.endswith(".py"):
        return "py"
    return "other"


def _read(path: Path) -> str:
    """Read text with error replacement; non-UTF8 bytes won't crash a heuristic."""
    return path.read_text(errors="replace")


# ---------------------------------------------------------------------------
# File discovery + pairing
# ---------------------------------------------------------------------------

# (test-suffix, source-suffix) tuples in checking order. The first
# suffix that matches a candidate filename determines its source pair.
# Order matters: ".test.tsx" must be checked before ".test.ts" so the
# longer suffix wins.
_TEST_SUFFIX_PAIRS: list[tuple[str, str]] = [
    (".test.tsx", ".tsx"),
    (".test.ts",  ".ts"),
    (".test.jsx", ".jsx"),
    (".test.js",  ".js"),
    (".spec.tsx", ".tsx"),
    (".spec.ts",  ".ts"),
    (".spec.jsx", ".jsx"),
    (".spec.js",  ".js"),
    ("_test.py",  ".py"),
]


def _is_test_file(path: Path) -> Optional[tuple[str, str]]:
    """Return the (test-suffix, source-suffix) pair that matches path, or None."""
    name = path.name
    for test_suffix, source_suffix in _TEST_SUFFIX_PAIRS:
        if name.endswith(test_suffix):
            return (test_suffix, source_suffix)
    return None


def _source_for(test_path: Path, suffixes: tuple[str, str]) -> Optional[Path]:
    """Given a test file and its (test-suffix, source-suffix) pair, return the
    source file if it exists alongside, else None."""
    test_suffix, source_suffix = suffixes
    base = test_path.name[: -len(test_suffix)]
    candidate = test_path.parent / (base + source_suffix)
    return candidate if candidate.exists() else None


def discover(path: Path) -> list[tuple[Path, Optional[Path]]]:
    """Walk path (file, directory, or implicit recursive) and return
    (test_file, source_file_or_None) pairs.

    - If path is a file: treat as a single candidate.
    - If path is a directory: walk recursively for test files.
    """
    pairs: list[tuple[Path, Optional[Path]]] = []
    if path.is_file():
        match = _is_test_file(path)
        if match is not None:
            pairs.append((path, _source_for(path, match)))
        return pairs

    # Directory walk
    if path.is_dir():
        for f in sorted(path.rglob("*")):
            if not f.is_file():
                continue
            match = _is_test_file(f)
            if match is not None:
                pairs.append((f, _source_for(f, match)))
    return pairs


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_files(pairs: Iterable[tuple[Path, Optional[Path]]]) -> list[dict]:
    """Run each (test, source) pair through HEURISTICS, return a list of
    {path, score, signals} dicts sorted descending by score.

    Each triggered heuristic contributes a signal entry. If the heuristic's
    fire() returned a non-empty string, that string is included as a
    `detail` field on the signal (the registry contract — see module
    docstring)."""
    results: list[dict] = []
    for test_path, source_path in pairs:
        triggered: list[tuple[dict, Optional[str]]] = []
        for h in HEURISTICS:
            try:
                outcome = h["fire"](test_path, source_path)
            except Exception:
                # Misbehaving heuristic doesn't crash the run; treat as
                # "did not fire" and let the others have their chance.
                continue
            if not outcome:
                continue
            detail = outcome if isinstance(outcome, str) else None
            triggered.append((h, detail))
        score = max((h["score"] for h, _ in triggered), default=0)
        signals = []
        for h, d in triggered:
            entry = {"name": h["name"], "score": h["score"]}
            if d:
                entry["detail"] = d
            signals.append(entry)
        results.append({
            "path": str(test_path),
            "score": score,
            "signals": signals,
        })
    results.sort(key=lambda r: (-r["score"], r["path"]))
    return results


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_text(results: list[dict]) -> str:
    """Aligned text output: <path>  score=N  signals=a,b:detail,c (one per line)."""
    if not results:
        return ""
    width = max(len(r["path"]) for r in results)
    lines = []
    for r in results:
        names = ",".join(
            f"{s['name']}:{s['detail']}" if s.get("detail") else s["name"]
            for s in r["signals"]
        ) or "-"
        lines.append(f"{r['path'].ljust(width)}  score={r['score']}  signals={names}")
    return "\n".join(lines)


def format_json(results: list[dict]) -> str:
    """Pretty JSON. Each entry preserves per-signal scores."""
    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Heuristic 2 — happy-path-only (score 7)
# ---------------------------------------------------------------------------
#
# Fires when a test file contains zero negative-space assertions (toThrow,
# rejects, pytest.raises, etc.). Pure regex against the test file; ignores
# the source. False negatives possible if a comment in the test mentions a
# negative-space pattern — accepted per bead spec.

_H2_NEGATIVE_SPACE = re.compile(
    r"expect.*toThrow"
    r"|expect.*rejects"
    r"|expect.*toReject"
    r"|\.toThrowError\("
    r"|expect.*not\.toBe(?:Undefined|Null)"
    r"|pytest\.raises"
    r"|self\.assertRaises"
    r"|with raises"
    r"|with pytest\.raises"
)


def _h2_fire(test_path: Path, source_path: Optional[Path]) -> bool:
    if _lang(test_path) not in ("ts", "py"):
        return False
    return _H2_NEGATIVE_SPACE.search(_read(test_path)) is None


HEURISTICS.append({"name": "happy-path-only", "score": 7, "fire": _h2_fire})


# ---------------------------------------------------------------------------
# Heuristic 4 — single-case wonder (score 5)
# ---------------------------------------------------------------------------
#
# Fires when test has exactly 1 case AND source has >3 public functions.
# Skipped when source is None (can't compare) or test has 0 cases (empty
# test is a different problem). Source detection is regex-approximate per
# bead spec — TS class methods are NOT counted to avoid keyword false
# positives; Python class methods ARE counted (lowercase-first-letter
# requirement excludes _private).

_TS_TEST = re.compile(r"(?m)^\s*(?:it|test)\s*\(")
_PY_TEST = re.compile(r"(?m)^\s*(?:async\s+)?def\s+test_")

# TS source: count exported functions and exported arrow consts. Skipping
# class methods avoids false positives from `if (`, `for (`, etc. — at the
# cost of undercounting tests that target classes. Acceptable for a
# heuristic; user can always opt in to interview the file regardless.
_TS_SOURCE_FUNCS = [
    re.compile(r"(?m)^export\s+(?:async\s+)?function\b"),
    re.compile(r"(?m)^export\s+const\s+\w+\s*=\s*(?:async\s+)?\("),
]

# Python source: top-level + indented `def` / `async def`, where the
# function name starts with a lowercase letter (excludes `_private` and
# `__dunder__`).
_PY_SOURCE_FUNCS = [
    re.compile(r"(?m)^(?:async\s+)?def\s+[a-z]\w*"),
    re.compile(r"(?m)^\s+(?:async\s+)?def\s+[a-z]\w*"),
]


def _h4_fire(test_path: Path, source_path: Optional[Path]) -> bool:
    if source_path is None:
        return False
    lang = _lang(test_path)
    if lang not in ("ts", "py"):
        return False
    test_text = _read(test_path)
    source_text = _read(source_path)
    if lang == "ts":
        test_count = len(_TS_TEST.findall(test_text))
        source_count = sum(len(p.findall(source_text)) for p in _TS_SOURCE_FUNCS)
    else:
        test_count = len(_PY_TEST.findall(test_text))
        source_count = sum(len(p.findall(source_text)) for p in _PY_SOURCE_FUNCS)
    if test_count == 0:
        return False
    return test_count == 1 and source_count > 3


HEURISTICS.append({"name": "single-case-wonder", "score": 5, "fire": _h4_fire})


# ---------------------------------------------------------------------------
# Heuristic 6 — stale fixture (score 9)
# ---------------------------------------------------------------------------
#
# Fires when a test file imports a symbol that the target module no
# longer exports. The fire() function returns the missing symbol name
# (truthy str) so the signal can be reported as `stale-fixture:<symbol>`.
#
# Documented limitations:
#   - Re-export chains (`export * from './bar'`) are NOT followed —
#     accept the false negative to avoid expensive multi-file traversal.
#   - Combined imports (`import foo, { bar } from 'x'`) only have their
#     named portion verified; the default name is missed. Rare in tests.
#   - External imports (non-relative paths like 'react') are skipped.

_TS_NAMED_IMPORT = re.compile(
    r"import\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]"
)
_TS_DEFAULT_IMPORT = re.compile(
    r"import\s+(\w+)\s+from\s*['\"]([^'\"]+)['\"]"
)
# We don't need to capture namespace imports — they're unverifiable, so
# we just exclude them by structure (no \w+ first-token match thanks to *).

_PY_FROM_IMPORT = re.compile(
    r"^from\s+(\S+)\s+import\s+(.+)$",
    re.MULTILINE,
)


def _parse_imports_ts(text: str) -> list[tuple[list[str], str]]:
    """Return list of (symbols, module_spec). symbols may include the
    sentinel '__default__' for default imports."""
    imports: list[tuple[list[str], str]] = []
    for m in _TS_NAMED_IMPORT.finditer(text):
        names_part, module = m.group(1), m.group(2)
        symbols: list[str] = []
        for piece in names_part.split(","):
            piece = piece.strip()
            if not piece:
                continue
            # `foo as bar` → keep `foo` (verify export of original name)
            if " as " in piece:
                piece = piece.split(" as ")[0].strip()
            symbols.append(piece)
        if symbols:
            imports.append((symbols, module))
    for m in _TS_DEFAULT_IMPORT.finditer(text):
        # _TS_DEFAULT_IMPORT also matches what _TS_NAMED_IMPORT caught
        # because both start with `import`. We separate them by checking
        # whether the next non-whitespace after `import` is `{` or `*`.
        # If so, this isn't a default import — skip.
        before = text[max(0, m.start()): m.start() + 7]
        # Look right after the 'import' keyword
        after_import_idx = m.start() + len("import")
        rest = text[after_import_idx:m.start() + 200].lstrip()
        if rest.startswith("{") or rest.startswith("*"):
            continue
        imports.append((["__default__"], m.group(2)))
    return imports


def _parse_imports_py(text: str) -> list[tuple[list[str], str]]:
    imports: list[tuple[list[str], str]] = []
    for m in _PY_FROM_IMPORT.finditer(text):
        module, names_part = m.group(1), m.group(2).strip()
        if names_part == "*":
            continue
        # Strip parentheses for the rare multi-line case
        names_part = names_part.replace("(", "").replace(")", "")
        symbols: list[str] = []
        for piece in names_part.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if " as " in piece:
                piece = piece.split(" as ")[0].strip()
            symbols.append(piece)
        if symbols:
            imports.append((symbols, module))
    return imports


def _resolve_target_ts(test_dir: Path, module_spec: str) -> Optional[Path]:
    if not module_spec.startswith("."):
        return None  # external dep
    base = (test_dir / module_spec).resolve()
    # If the spec already points at an existing file with extension, use it.
    if base.is_file():
        return base
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        candidate = base.with_suffix(ext)
        if candidate.exists():
            return candidate
    # /index variants for directory-style imports
    for ext in (".ts", ".tsx"):
        candidate = base / f"index{ext}"
        if candidate.exists():
            return candidate
    return None


def _resolve_target_py(test_dir: Path, module_spec: str) -> Optional[Path]:
    if not module_spec.startswith("."):
        return None  # external dep
    parts = module_spec.lstrip(".").split(".")
    if not parts or parts == [""]:
        return None
    base = test_dir.joinpath(*parts)
    candidate = base.with_suffix(".py")
    if candidate.exists():
        return candidate
    candidate = base / "__init__.py"
    if candidate.exists():
        return candidate
    return None


def _is_exported_ts(text: str, symbol: str) -> bool:
    if symbol == "__default__":
        return bool(re.search(r"export\s+default\b", text))
    s = re.escape(symbol)
    patterns = [
        rf"export\s+(?:async\s+)?function\s+{s}\b",
        rf"export\s+class\s+{s}\b",
        rf"export\s+const\s+{s}\b",
        rf"export\s+let\s+{s}\b",
        rf"export\s+var\s+{s}\b",
        rf"export\s+type\s+{s}\b",
        rf"export\s+interface\s+{s}\b",
        rf"export\s+enum\s+{s}\b",
        rf"export\s+default\s+(?:async\s+)?function\s+{s}\b",
        rf"export\s+default\s+class\s+{s}\b",
        # Named re-export — matches `export { foo }` or `export { foo, bar }`
        # with or without a trailing `from './path'` clause.
        rf"export\s*\{{[^}}]*\b{s}\b[^}}]*\}}",
    ]
    return any(re.search(p, text) for p in patterns)


def _is_exported_py(text: str, symbol: str) -> bool:
    s = re.escape(symbol)
    patterns = [
        rf"^def\s+{s}\b",
        rf"^async\s+def\s+{s}\b",
        rf"^class\s+{s}\b",
        rf"^{s}\s*=",
    ]
    if any(re.search(p, text, re.MULTILINE) for p in patterns):
        return True
    # __all__ entry
    return bool(re.search(rf"__all__\s*=\s*\[[^\]]*['\"]{s}['\"]", text))


def _h6_fire(test_path: Path, source_path: Optional[Path]):
    lang = _lang(test_path)
    if lang not in ("ts", "py"):
        return False
    text = _read(test_path)
    test_dir = test_path.parent
    if lang == "ts":
        imports = _parse_imports_ts(text)
        resolve = _resolve_target_ts
        is_exported = _is_exported_ts
    else:
        imports = _parse_imports_py(text)
        resolve = _resolve_target_py
        is_exported = _is_exported_py

    for symbols, module_spec in imports:
        target = resolve(test_dir, module_spec)
        if target is None:
            continue
        target_text = _read(target)
        for symbol in symbols:
            if not is_exported(target_text, symbol):
                # Return the missing symbol — the registry treats truthy
                # strings as "fired with this detail."
                display = "default" if symbol == "__default__" else symbol
                return display
    return False


HEURISTICS.append({"name": "stale-fixture", "score": 9, "fire": _h6_fire})


# ---------------------------------------------------------------------------
# Heuristic 3 — mock saturation (score 8)
# ---------------------------------------------------------------------------
#
# Fires when an integration-tagged test mocks more than 70% of its
# imports. Skips non-integration tests entirely (mocks in unit tests
# are expected and not a signal of shallowness).

_INTEGRATION_TAG_FILENAME = re.compile(r"\.(integration|it|e2e)\.|_integration_test\.py$")
_INTEGRATION_TAG_PRAGMA = re.compile(r"(?://|#)\s*@integration\b")
_INTEGRATION_TAG_DESCRIBE = re.compile(
    r"\b(?:describe|context)\s*\(\s*[\'\"][^\'\"]*\bintegration\b",
    re.IGNORECASE,
)

_TS_MOCK_PATTERNS = [
    re.compile(r"\bvi\.mock\("),
    re.compile(r"\bjest\.mock\("),
    re.compile(r"\bvi\.fn\(\)"),
    re.compile(r"\bMockedFunction\b"),
]
_PY_MOCK_PATTERNS = [
    re.compile(r"@mock\.patch\("),
    re.compile(r"@patch\("),
    re.compile(r"\bmock\.patch\("),
    re.compile(r"\bMagicMock\("),
    re.compile(r"\bMock\("),
]

_TS_IMPORT_LINE = re.compile(r"(?m)^import\s")
_PY_IMPORT_LINE = re.compile(r"(?m)^(?:from\s+\S+\s+import\s|import\s)")


def _is_integration_test(test_path: Path, text: str) -> bool:
    if _INTEGRATION_TAG_FILENAME.search(test_path.name.lower()):
        return True
    head = "\n".join(text.split("\n")[:10])
    if _INTEGRATION_TAG_PRAGMA.search(head):
        return True
    if _INTEGRATION_TAG_DESCRIBE.search(text):
        return True
    return False


def _h3_fire(test_path: Path, source_path: Optional[Path]) -> bool:
    lang = _lang(test_path)
    if lang not in ("ts", "py"):
        return False
    text = _read(test_path)
    if not _is_integration_test(test_path, text):
        return False
    if lang == "ts":
        mock_count = sum(len(p.findall(text)) for p in _TS_MOCK_PATTERNS)
        import_count = len(_TS_IMPORT_LINE.findall(text))
    else:
        mock_count = sum(len(p.findall(text)) for p in _PY_MOCK_PATTERNS)
        import_count = len(_PY_IMPORT_LINE.findall(text))
    if import_count == 0:
        return False
    return (mock_count / import_count) > 0.7


HEURISTICS.append({"name": "mock-saturation", "score": 8, "fire": _h3_fire})


# ---------------------------------------------------------------------------
# Heuristic 5 — missing categories (score 6)
# ---------------------------------------------------------------------------
#
# Pattern → category lookup table (closed list — keep small to avoid
# noise). Each entry: (source-pattern detector, category name,
# test-reference matcher). If the source matches the detector but the
# test never references the category keywords, fire with that category
# in the detail.
#
# Bead-spec note: spec called for "score 6 per missing" with stacking;
# implemented here as a single fire(score=6) with all missing categories
# joined in the detail (e.g. detail="state-machine,security"). The user
# still gets the full category list; ranking against other heuristics
# uses the static 6 rather than 6×N. Simpler model, same surface
# information.


def _has_state_machine(text: str, lang: str) -> bool:
    """switch (TS) or match (PY) with at least 3 case clauses."""
    if lang == "ts":
        if not re.search(r"\bswitch\s*\(", text):
            return False
    elif lang == "py":
        if not re.search(r"\bmatch\s+\w", text):
            return False
    else:
        return False
    return len(re.findall(r"\bcase\s+", text)) >= 3


def _has_concurrency(text: str, lang: str) -> bool:
    """async + a synchronization primitive."""
    if lang == "ts":
        return bool(
            re.search(r"\basync\b", text)
            and re.search(r"\b(?:Lock|Mutex|Semaphore)\b", text)
        )
    if lang == "py":
        return bool(
            re.search(r"\basync\s+def\b", text)
            and re.search(
                r"\b(?:asyncio\.Lock|threading\.Lock|Mutex|Semaphore)\b", text
            )
        )
    return False


def _has_security(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:authenticate|verify_token|requireRole|hasPermission|RLS)\b",
            text,
        )
    )


def _has_boundary_handler(text: str) -> bool:
    """Co-occurrence of an HTTP-route-ish keyword with a request-ish keyword."""
    return bool(
        re.search(r"\b(?:handler|route|endpoint)\b", text)
        and re.search(r"\b(?:req|request)\b", text)
    )


_TEST_REF_STATE_MACHINE = re.compile(
    r"\b(?:transition|state[\s-]?machine|state\b)", re.IGNORECASE
)
_TEST_REF_CONCURRENCY = re.compile(
    r"\b(?:concurren|race|simultaneous|out[\s-]of[\s-]order)", re.IGNORECASE
)
_TEST_REF_SECURITY = re.compile(
    r"\b(?:auth|unauthor|forbid|permission|injection)", re.IGNORECASE
)
_TEST_REF_BOUNDARY = re.compile(
    r"\b(?:invalid input|malformed|negative space)", re.IGNORECASE
)


def _h5_fire(test_path: Path, source_path: Optional[Path]):
    if source_path is None:
        return False
    lang = _lang(test_path)
    if lang not in ("ts", "py"):
        return False
    test_text = _read(test_path)
    source_text = _read(source_path)

    checks = [
        ("state-machine", _has_state_machine(source_text, lang), _TEST_REF_STATE_MACHINE),
        ("concurrency",   _has_concurrency(source_text, lang),   _TEST_REF_CONCURRENCY),
        ("security",      _has_security(source_text),            _TEST_REF_SECURITY),
        ("boundary",      _has_boundary_handler(source_text),    _TEST_REF_BOUNDARY),
    ]
    missing = [
        name for name, source_has_pattern, test_ref in checks
        if source_has_pattern and not test_ref.search(test_text)
    ]
    if not missing:
        return False
    return ",".join(missing)


HEURISTICS.append({"name": "missing-categories", "score": 6, "fire": _h5_fire})


# ---------------------------------------------------------------------------
# Heuristic 1 — assertion density (score 6)
# ---------------------------------------------------------------------------
#
# Fires when assertions / max(branches, 1) < 1.0 AND branches >= 3. The
# >=3 floor keeps the heuristic from flagging trivial sources where one
# assertion is genuinely enough.
#
# Branch counting:
#   Python — stdlib `ast` walk. Precise: counts If/For/While/IfExp,
#     Match cases, Try (1 + len(handlers)), and BoolOp short-circuit
#     branches (len(values) - 1).
#   TypeScript — regex approximation. Documented lossy: counts
#     if(/case /try{/catch(/&&/||/ternary `? ... :`. Can be ±20% on
#     dense ternary expressions; acceptable because the heuristic uses
#     a < 1.0 ratio threshold (rough is fine).


def _count_assertions(text: str, lang: str) -> int:
    if lang == "ts":
        patterns = [
            re.compile(r"\bexpect\("),
            re.compile(r"\bassert\.(?:equal|deepEqual|strictEqual|throws)\b"),
        ]
    elif lang == "py":
        patterns = [
            re.compile(r"(?m)^\s*assert\s"),
            re.compile(r"\bself\.assert[A-Z]\w*\("),
        ]
    else:
        return 0
    return sum(len(p.findall(text)) for p in patterns)


def _count_branches_py(source: str) -> int:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.For, ast.While, ast.IfExp)):
            count += 1
        elif isinstance(node, ast.Try):
            # Try block itself + each except handler is a separate branch
            count += 1 + len(node.handlers)
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            # Each operand beyond the first is a short-circuit branch
            count += max(0, len(node.values) - 1)
        elif isinstance(node, getattr(ast, "Match", type(None))):
            count += len(node.cases)
    return count


_TS_BRANCH_PATTERNS = [
    re.compile(r"\bif\s*\("),
    re.compile(r"\bcase\s+"),
    re.compile(r"\btry\s*\{"),
    re.compile(r"\bcatch\s*\("),
    re.compile(r"&&"),
    re.compile(r"\|\|"),
    # Ternary — `cond ? a : b`. Loose; may overcount when `?:` appears
    # in optional chaining or type annotations. Bead-spec accepts ±20%.
    re.compile(r"\?\s*[^:]+:"),
]


def _count_branches_ts(source: str) -> int:
    return sum(len(p.findall(source)) for p in _TS_BRANCH_PATTERNS)


def _h1_fire(test_path: Path, source_path: Optional[Path]) -> bool:
    if source_path is None:
        return False
    lang = _lang(test_path)
    if lang not in ("ts", "py"):
        return False
    test_text = _read(test_path)
    source_text = _read(source_path)
    assertions = _count_assertions(test_text, lang)
    if lang == "py":
        branches = _count_branches_py(source_text)
    else:
        branches = _count_branches_ts(source_text)
    if branches < 3:
        return False
    return (assertions / branches) < 1.0


HEURISTICS.append({"name": "assertion-density", "score": 6, "fire": _h1_fire})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="columbo-prefilter",
        description="Rank test files by shallowness signals.",
    )
    p.add_argument("path", help="File, directory, or glob to scan for test files.")
    p.add_argument(
        "--threshold",
        type=int,
        default=5,
        help="Only emit files scoring >= THRESHOLD (default: 5).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of aligned text.",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    target = Path(args.path)
    if not target.exists():
        sys.stderr.write(f"columbo-prefilter: path does not exist: {target}\n")
        return 2

    pairs = discover(target)
    results = score_files(pairs)
    filtered = [r for r in results if r["score"] >= args.threshold]

    out = format_json(filtered) if args.json else format_text(filtered)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
