#!/usr/bin/env python3
"""sable_stack_detect — the /sable-onboarding stack + .sable-contract library
(SABLE-gn7a.1). Consumed by the onboarding scanner (SABLE-gn7a.3) and the
onboarding skill (SABLE-gn7a.4). Three responsibilities:

  1. .SABLE GRAMMAR (parse / validate / build / write). The grammar is defined
     by ONE parser in the wild — the resolvers in
     hooks/multi-manager/lib-identity.sh:

         val=$(sed -n 's/^testCommand=//p'      "$repo/.sable" | head -1)
         val=$(sed -n 's/^integrationBranch=//p' "$repo/.sable" | head -1)

     so a line is a valid `KEY=` line **iff** it begins with exactly `KEY=`
     (case-sensitive, no whitespace before the key and none around the `=`);
     the value is the remainder of that line, verbatim, to end-of-line (it may
     itself contain spaces or `=` — the repo's own .sable testCommand is a full
     `for … do … done` one-liner); and when several matching lines exist the
     FIRST wins (`head -1`). This module is the single Python mirror of that
     grammar; test_scan_contract_matches_lib_identity_resolvers proves the two
     agree line-for-line against the REAL shell functions, not a copy.

  2. STACK DETECTION keyed on LOCKFILE + package.json `packageManager`, never a
     bare manifest. pre-push-rebase-test.sh's fallback emits `npm test` for any
     package.json — wrong for a pnpm/yarn repo (SABLE-uvul tracks unifying the
     two). Detection here keys the JS manager on the lockfile (pnpm-lock.yaml →
     pnpm, yarn.lock → yarn, package-lock.json → npm) or, absent a lockfile, on
     the `packageManager` field; a bare package.json with neither signal yields
     NO candidate. Multi-stack repos surface ALL candidates (detect-and-ask,
     never pick-dominant); a repo with no detectable framework returns the
     explicit `none` signal so the skill knows to ask (documented no-tests-yet
     escape) rather than guessing.

  3. EXECUTE-ONCE. Run a candidate command exactly as pre-push does
     (`cd <repo> && timeout <n> sh -c "<cmd>"`, stderr folded into stdout,
     exit 124 == timeout) and return the result. write() REFUSES to emit a
     `testCommand=` line unless its execute-once run passed — a contract can
     never be written asserting a command that does not actually pass.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import NamedTuple, Optional

# The two keys the lib-identity.sh resolvers understand. Order is the order
# validate() tries them per line; they are disjoint so order is immaterial.
TEST_COMMAND_KEY = "testCommand"
INTEGRATION_BRANCH_KEY = "integrationBranch"
KNOWN_KEYS = (TEST_COMMAND_KEY, INTEGRATION_BRANCH_KEY)

# Explicit "no framework detected" signal (Detection.signal). A sentinel string,
# not None/"" — the skill branches on it to ask the human, so it must be
# unmistakable and greppable rather than a falsy accident.
NONE = "none"


class WriteRefused(Exception):
    """Raised when write() is asked to persist a testCommand= line whose
    execute-once run did not pass. Carries the surfaced exit code so callers
    (and the skill) can report why the contract was not written."""

    def __init__(self, message: str, exit_code: Optional[int] = None):
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# (1) .sable grammar — the single Python mirror of the sed one-parser.
# ---------------------------------------------------------------------------

class LineClass(NamedTuple):
    """One line of a .sable file, classified against the grammar."""
    lineno: int          # 1-based
    text: str            # the line, newline stripped
    verdict: str         # "accept" | "reject"
    key: Optional[str]   # matched key when accepted, else None
    value: Optional[str] # parsed value when accepted, else None


def _logical_lines(text: str):
    """Split .sable text the way sed sees it: on '\\n' only (NOT on the exotic
    boundaries str.splitlines() honors), dropping the trailing empty segment a
    final newline produces. Yields (lineno, line_without_newline)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for i, line in enumerate(lines, start=1):
        yield i, line


def classify_line(line: str, key: str):
    """Classify a single line against `sed -n 's/^KEY=//p'`.

    Returns ("accept", value) when the line begins with exactly ``KEY=``
    (case-sensitive, key anchored at the start, `=` immediately after the key),
    with ``value`` the verbatim remainder (possibly empty, may contain `=`);
    otherwise ("reject", None). A trailing newline, if present, is not part of
    the value — callers pass newline-stripped lines.
    """
    prefix = key + "="
    if line.startswith(prefix):
        return "accept", line[len(prefix):]
    return "reject", None


def validate(text: str, keys=KNOWN_KEYS):
    """Classify every logical line of `.sable` text accept/reject.

    A line is accepted iff it matches one of `keys` under the sed grammar
    (see classify_line). Blank lines, comments, spaced keys, and wrong-case
    keys all reject. Returns a list of LineClass, one per line, in order.
    """
    results = []
    for lineno, line in _logical_lines(text):
        matched = None
        for key in keys:
            verdict, value = classify_line(line, key)
            if verdict == "accept":
                matched = (key, value)
                break
        if matched:
            results.append(LineClass(lineno, line, "accept", matched[0], matched[1]))
        else:
            results.append(LineClass(lineno, line, "reject", None, None))
    return results


def parse(text: str, key: str):
    """Resolve KEY from `.sable` text: first matching line's value, mirroring
    `sed -n 's/^KEY=//p' | head -1`; None when no line matches."""
    for _lineno, line in _logical_lines(text):
        verdict, value = classify_line(line, key)
        if verdict == "accept":
            return value
    return None


def parse_file(path: str, key: str):
    """File-level parse(): resolve KEY from the .sable file at `path`, or None
    when the file is absent or has no matching line. Mirrors the resolvers'
    `[ -f "$repo/.sable" ]` guard so a missing file classifies as reject."""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return parse(fh.read(), key)


def _reject_multiline(value: str, key: str):
    """Guard: a .sable value MUST be single-line. A newline would be silently
    truncated at the first line by `head -1`, writing a contract different from
    the one confirmed — refuse loudly instead."""
    if "\n" in value or "\r" in value:
        raise ValueError(
            "%s value must be a single line (the .sable grammar is line-based; "
            "a newline would be truncated by `head -1`): %r" % (key, value)
        )


def build_sable(*, test_command: Optional[str] = None,
                integration_branch: Optional[str] = None) -> str:
    """Render `.sable` content: `testCommand=<value>` and/or
    `integrationBranch=<value>`, in that order, each a single line, exactly as
    the confirmed values (no escaping, no reflow — the grammar is verbatim).
    Trailing newline when non-empty; empty string when nothing to write."""
    lines = []
    if test_command is not None:
        _reject_multiline(test_command, TEST_COMMAND_KEY)
        lines.append(TEST_COMMAND_KEY + "=" + test_command)
    if integration_branch is not None:
        _reject_multiline(integration_branch, INTEGRATION_BRANCH_KEY)
        lines.append(INTEGRATION_BRANCH_KEY + "=" + integration_branch)
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def write(path: str, *, test_command: Optional[str] = None,
          integration_branch: Optional[str] = None,
          execute_result: "Optional[ExecuteResult]" = None) -> str:
    """Write a `.sable` file at `path` and return the content written.

    Refuses (WriteRefused) to persist a `testCommand=` line unless
    `execute_result` is a passing ExecuteResult — the execute-once run must
    have actually succeeded. `integrationBranch=` carries no execution and is
    written unconditionally. The refusal surfaces the failing exit code.
    """
    if test_command is not None:
        if execute_result is None:
            raise WriteRefused(
                "refusing to write %s=%r: no execute-once run was supplied"
                % (TEST_COMMAND_KEY, test_command)
            )
        if not execute_result.ok:
            raise WriteRefused(
                "refusing to write %s=%r: execute-once failed (exit %d%s)"
                % (TEST_COMMAND_KEY, test_command, execute_result.exit_code,
                   ", timed out" if execute_result.timed_out else ""),
                exit_code=execute_result.exit_code,
            )
    content = build_sable(test_command=test_command,
                          integration_branch=integration_branch)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return content


# ---------------------------------------------------------------------------
# (2) Stack detection — lockfile/packageManager-keyed, never a bare manifest.
# ---------------------------------------------------------------------------

class Candidate(NamedTuple):
    """One detected test-command candidate awaiting human confirmation."""
    command: str  # the shell command, e.g. "pnpm test"
    source: str   # the file/field that keyed it, e.g. "pnpm-lock.yaml"
    kind: str     # coarse stack tag, e.g. "js/pnpm", "python", "go"


class Detection(NamedTuple):
    candidates: tuple  # tuple[Candidate, ...], every stack found (detect-and-ask)

    @property
    def detected(self) -> bool:
        return len(self.candidates) > 0

    @property
    def signal(self) -> str:
        """'detected' when >=1 candidate, else the explicit NONE sentinel."""
        return "detected" if self.candidates else NONE

    @property
    def commands(self):
        return [c.command for c in self.candidates]


def _isfile(repo_path: str, name: str) -> bool:
    return os.path.isfile(os.path.join(repo_path, name))


def package_manager_field(repo_path: str):
    """Return the bare manager name from package.json's `packageManager`
    (e.g. "pnpm@8.6.0" -> "pnpm"), or None when there is no package.json, no
    field, or the JSON is unreadable. Corepack pins the manager here even when
    a lockfile is absent, so it is a legitimate second keying signal."""
    path = os.path.join(repo_path, "package.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return None
    pm = data.get("packageManager") if isinstance(data, dict) else None
    if not isinstance(pm, str):
        return None
    name = pm.split("@", 1)[0].strip()
    return name or None


def _detect_js(repo_path: str):
    """JS test candidates keyed on the LOCKFILE (strongest signal), else the
    packageManager field. A bare package.json with neither yields nothing —
    the pitfall this module exists to avoid. Each present lockfile surfaces its
    own candidate (never pick-dominant across ambiguous lockfiles)."""
    candidates = []
    if _isfile(repo_path, "pnpm-lock.yaml"):
        candidates.append(Candidate("pnpm test", "pnpm-lock.yaml", "js/pnpm"))
    if _isfile(repo_path, "yarn.lock"):
        candidates.append(Candidate("yarn test", "yarn.lock", "js/yarn"))
    if _isfile(repo_path, "package-lock.json"):
        candidates.append(Candidate("npm test", "package-lock.json", "js/npm"))
    if candidates:
        return candidates
    pm = package_manager_field(repo_path)
    cmd = {"pnpm": "pnpm test", "yarn": "yarn test", "npm": "npm test"}.get(pm)
    if cmd:
        return [Candidate(cmd, "packageManager=" + pm, "js/" + pm)]
    return []


def _detect_python(repo_path: str):
    """A single pytest candidate when any of pyproject.toml / pytest.ini /
    setup.cfg is present (they map to the same runner — do not duplicate)."""
    for name in ("pyproject.toml", "pytest.ini", "setup.cfg"):
        if _isfile(repo_path, name):
            return [Candidate("pytest", name, "python")]
    return []


def makefile_has_test_target(repo_path: str) -> bool:
    """True when a Makefile declares a `test` target (a line beginning `test:`,
    optionally with prerequisites). `.PHONY: test` is a declaration, not the
    rule, and does not match."""
    path = os.path.join(repo_path, "Makefile")
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if re.match(r"^test[ \t]*:", line):
                    return True
    except OSError:
        return False
    return False


def detect_stack(repo_path: str) -> Detection:
    """Detect ALL test-command candidates for `repo_path` (detect-and-ask).

    JS is keyed on lockfile/packageManager (see _detect_js); pyproject.toml /
    pytest.ini / setup.cfg -> pytest; go.mod -> `go test ./...`; Cargo.toml ->
    `cargo test`; a Makefile `test:` target -> `make test`. An empty candidate
    list is the explicit `none` signal (Detection.signal == NONE).
    """
    candidates = []
    candidates.extend(_detect_js(repo_path))
    candidates.extend(_detect_python(repo_path))
    if _isfile(repo_path, "go.mod"):
        candidates.append(Candidate("go test ./...", "go.mod", "go"))
    if _isfile(repo_path, "Cargo.toml"):
        candidates.append(Candidate("cargo test", "Cargo.toml", "rust"))
    if makefile_has_test_target(repo_path):
        candidates.append(Candidate("make test", "Makefile", "make"))
    return Detection(tuple(candidates))


# ---------------------------------------------------------------------------
# (3) Execute-once — mirror pre-push-rebase-test.sh's TEST phase exactly.
# ---------------------------------------------------------------------------

class ExecuteResult(NamedTuple):
    command: str
    exit_code: int
    timed_out: bool
    output: str  # combined stdout+stderr, as pre-push captures with 2>&1

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def execute_once(command: str, cwd: Optional[str] = None,
                 timeout: int = 60) -> ExecuteResult:
    """Run `command` ONCE, mirroring pre-push-rebase-test.sh:393
    (`cd "$CWD" && timeout "$TIMEOUT" sh -c "$CMD" 2>&1`). stderr is folded into
    stdout; exit 124 (GNU timeout's kill code) sets timed_out. Never raises on a
    nonzero command exit — the exit code is returned for the caller to gate on.
    """
    proc = subprocess.run(
        ["timeout", str(timeout), "sh", "-c", command],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return ExecuteResult(
        command=command,
        exit_code=proc.returncode,
        timed_out=proc.returncode == 124,
        output=proc.stdout or "",
    )
