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
      "name":  str,                                  # signal label, e.g. "happy-path-only"
      "score": int,                                  # 0-10, fixed per heuristic
      "fire":  Callable[[Path, Path|None], bool],    # (test_path, source_path) -> fired?
    }

  Sibling beads (rjv.5.2-5.5) plug into HEURISTICS by appending entries.
  Per-file scoring: triggered = [h for h in HEURISTICS if h["fire"](t, s)];
  final score = max((h["score"] for h in triggered), default=0). Signals
  list = the names of triggered heuristics.

  Note: max — not sum. A single strong signal (e.g. stale-fixture, score 9)
  should outrank a pile of weak signals.

Currently the registry is empty (no heuristics shipped yet). 5.1 ships
the scaffolding; 5.2 adds happy-path-only + single-case-wonder; the
rest follow.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

# Public registry — heuristic beads append to this.
HEURISTICS: list[dict] = []


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
    {path, score, signals} dicts sorted descending by score."""
    results: list[dict] = []
    for test_path, source_path in pairs:
        triggered: list[dict] = []
        for h in HEURISTICS:
            try:
                if h["fire"](test_path, source_path):
                    triggered.append(h)
            except Exception:
                # A misbehaving heuristic shouldn't crash the whole run;
                # treat exceptions as "did not fire" so the rest of the
                # heuristics still get a chance.
                continue
        score = max((h["score"] for h in triggered), default=0)
        results.append({
            "path": str(test_path),
            "score": score,
            "signals": [{"name": h["name"], "score": h["score"]} for h in triggered],
        })
    results.sort(key=lambda r: (-r["score"], r["path"]))
    return results


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_text(results: list[dict]) -> str:
    """Aligned text output: <path>  score=N  signals=a,b,c (one per line)."""
    if not results:
        return ""
    width = max(len(r["path"]) for r in results)
    lines = []
    for r in results:
        names = ",".join(s["name"] for s in r["signals"]) or "-"
        lines.append(f"{r['path'].ljust(width)}  score={r['score']}  signals={names}")
    return "\n".join(lines)


def format_json(results: list[dict]) -> str:
    """Pretty JSON. Each entry preserves per-signal scores."""
    return json.dumps(results, indent=2)


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
