#!/usr/bin/env bash
# bead-description-gate.sh — Validate bead descriptions on creation
# Trigger: PreToolUse on Bash (matching bd create) | Timeout: 3000ms
#
# One evaluator owns parsing, validation, and JSON rendering. The previous
# shell pipeline launched separate Python and grep processes for nearly every
# field and rule, making this high-frequency hook slower and harder to change
# without adding a new parsing path.
#
# Manager sessions hard-block incomplete descriptions; ordinary sessions get
# the same findings as a nudge. Epics are exempt. Sherlock and Columbo labels
# add their template-specific contracts. Batch/stdin inputs remain warn-only
# because their content is unavailable at hook time.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"

# Taxonomy accessor used by telemetry tests and installers. The taxonomy stays
# owned by sable_telemetry_lib.py; this hook never carries a second list.
if [ "${1:-}" = "--print-origin-labels" ]; then
  if [ -x "$HOOK_DIR/../bin/sable-telemetry" ]; then
    exec "$HOOK_DIR/../bin/sable-telemetry" --print-origin-labels
  fi
  exec sable-telemetry --print-origin-labels
fi

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""
HOOK_INPUT="$HOOK_INPUT" python3 - "$HOOK_DIR" <<'PY'
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


def emit(payload):
    print(json.dumps(payload))


def deny(reason):
    emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    })


def nudge(context):
    emit({"additionalContext": context})


try:
    payload = json.loads(os.environ.get("HOOK_INPUT", ""))
except Exception:
    sys.exit(0)

command = (payload.get("tool_input") or {}).get("command", "") or ""
if not command:
    sys.exit(0)

# These exact argv vectors only inspect create usage. Tokenize instead of
# comparing source spelling so equivalent whitespace/quoting remains benign.
# punctuation_chars makes shell compounds/redirections additional tokens, so a
# help vector cannot exempt another command in the same Bash invocation.
try:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    command_argv = list(lexer)
except ValueError:
    command_argv = []
if command_argv[:2] != ["bd", "create"]:
    sys.exit(0)
if command_argv in (
    ["bd", "create"],
    ["bd", "create", "-h"],
    ["bd", "create", "--help"],
):
    sys.exit(0)

if re.search(r"--type(?:=| )?epic", command, re.IGNORECASE):
    sys.exit(0)

block = bool(os.environ.get("CLAUDE_AGENT_NAME")) or os.environ.get("CLAUDE_AGENT_ROLE") == "manager"


def first_group(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1)
    return ""


labels = first_group(
    [
        r'--labels?[= ]"([^"]+)"',
        r"--labels?[= ]'([^']+)'",
        r"--labels?[= ]([^\s\"']+)",
    ],
    command,
)
label_set = set(filter(None, labels.split(",")))
origin_present = any(label.startswith("origin:") for label in label_set)
sherlock = "sherlock-finding" in label_set
columbo_spec = "columbo-test-spec" in label_set
columbo_gap = "columbo-test-gap" in label_set

body_file = first_group(
    [
        r'--body-file[= ]"([^"]+)"',
        r"--body-file[= ]'([^']+)'",
        r"--body-file[= ](\S+)",
    ],
    command,
)
has_graph = re.search(r"--graph(?:\s|=)", command) is not None
has_file_flag = re.search(r"(?:--file(?:\s|=)|(?:^|\s)-f(?:\s|=))", command) is not None
has_stdin = "--stdin" in command

description = ""
if body_file and body_file != "-":
    path = Path(body_file)
    if not path.is_file():
        nudge(
            "SABLE bead quality: --body-file path does not exist yet; quality check skipped. "
            "Ensure the file passes the Fresh Agent Test (file paths, test spec, acceptance "
            "criteria) before creating."
        )
        sys.exit(0)
    try:
        description = path.read_text()
    except Exception:
        sys.exit(0)
elif body_file or has_graph or has_file_flag or has_stdin:
    nudge(
        "SABLE bead quality: bd create uses a batch/stdin mode "
        "(--graph/--file/--stdin/--body-file -). Quality check skipped at hook time. "
        "Ensure each bead in the batch passes the Fresh Agent Test: file paths, function "
        "names, what to change, test file reference, acceptance criteria."
    )
    sys.exit(0)
else:
    # Preserve the established flag grammar: long/short forms, space/equals,
    # either quote style, and multiline quoted content.
    flag = r"(?:--description|(?<![^\s])-d)(?:\s|=)"
    description = first_group(
        [
            flag + r'"((?:[^"\\]|\\.)*)"',
            flag + r"'((?:[^'\\]|\\.)*)'",
        ],
        command,
    )
    has_description = (
        "--description" in command
        or re.search(r"(?:^|\s)-d(?:\s|=)", command) is not None
    )
    if not has_description:
        if block:
            deny(
                "SABLE bead quality: bd create has no --description flag. Manager context "
                "requires a description that passes the Fresh Agent Test (file paths, test "
                "spec, acceptance criteria). Add --description and retry."
            )
        else:
            nudge(
                "SABLE bead quality: This bd create has no --description flag. Every bead "
                "needs a description that passes the Fresh Agent Test: file paths, function "
                "names, what to change, test file path, and acceptance criteria."
            )
        sys.exit(0)

if not description:
    sys.exit(0)

missing = []


def require(pattern, message, flags=re.MULTILINE):
    if re.search(pattern, description, flags) is None:
        missing.append(message)


if sherlock:
    require(r"^## Rationale", "## Rationale section")
    require(r"Fingerprint:", "Evidence with at least one Fingerprint: line")
    require(r"^## Proposed approach", "## Proposed approach section")
    require(r"^## Scope estimate", "## Scope estimate section")
    require(r"^## Risk if not addressed", "## Risk if not addressed section")

if columbo_spec:
    require(r"^## Feature under test", "## Feature under test section")
    require(r"^## Test file", "## Test file section")
    require(r"^## Test layer", "## Test layer section (UNIT | E2E | EVAL)")
    require(r"^## Cases", "## Cases section")
    require(r"(?:Why|why):", "## Cases must include at least one Why: sub-line per case")
    require(r"^## Categories", "## Categories section")
    require(r"^## Fixtures", "## Fixtures / setup section (use 'Fixtures: none.' if no setup)")
    require(r"^## Out of scope", "## Out of scope section")

if columbo_gap:
    require(r"^## Symptom", "## Symptom section")
    require(r"^## Cited test file", "## Cited test file section")
    require(r"^## Cited source file", "## Cited source file section")
    require(
        r"^## Existing test quality",
        "## Existing test quality section (★/★★/★★★ grade or 'none — net-new test required')",
    )
    require(
        r"^## Fingerprint",
        "## Fingerprint section (literal substring grep-able from cited file)",
    )
    require(r"^## Cases to add", "## Cases to add section")
    require(r"^## Categories", "## Categories section")
    require(r"^## Risk if not addressed", "## Risk if not addressed section")

if re.search(
    r"(test|\.test\.|\.spec\.|__tests__|pytest|vitest|TDD|red.green|\[no-test\])",
    description,
    re.IGNORECASE,
) is None:
    missing.append("test spec (which test file, what assertions)")

if re.search(
    r"(\.(ts|tsx|py|js|jsx|sh|go|rs|rb|md|json|yaml|yml|toml|kdl|cfg|ini|txt)"
    r"|frontend/|src/|lib/|components/|hooks/|templates/|docs/|feedback/|bin/"
    r"|location-briefing/|\.[a-zA-Z][a-zA-Z0-9_-]*/"
    r"|\b(Makefile|Dockerfile|Justfile|Rakefile)\b)",
    description,
    re.IGNORECASE,
) is None:
    missing.append("file paths (exact files to create/modify)")

if not missing:
    if not origin_present:
        hook_dir = Path(sys.argv[1])
        repo_cli = hook_dir.parent / "bin" / "sable-telemetry"
        command_line = [str(repo_cli if os.access(repo_cli, os.X_OK) else "sable-telemetry"),
                        "--print-origin-labels"]
        try:
            result = subprocess.run(
                command_line,
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
            hint = ",".join(line for line in result.stdout.splitlines() if line)
        except Exception:
            hint = ""
        values = f" Valid values: {hint}." if hint else ""
        nudge(
            "SABLE bead quality: no origin: label found (e.g. origin:planned)."
            + values
            + " Soft nudge only — creation is never blocked; add one when convenient "
            "to improve intake-attribution telemetry."
        )
    sys.exit(0)

missing_text = "; ".join(missing)
if not block:
    nudge(
        f"SABLE bead quality: Description is missing: {missing_text}. Good beads include "
        "file paths, function names, test file references, and acceptance criteria so "
        "agents can act immediately without re-exploring."
    )
    sys.exit(0)

if sherlock:
    reason = (
        "SABLE bead quality (sherlock-finding): Description missing required sections "
        f"per templates/sherlock-bead.md — {missing_text}. Fix the description and retry. "
        "Sherlock findings have a higher quality bar than the default Fresh Agent Test."
    )
elif columbo_spec:
    reason = (
        "SABLE bead quality (columbo-test-spec): Description missing required sections "
        f"per templates/columbo-bead.md — {missing_text}. Fix the description and retry. "
        "Columbo test-spec beads form the worker's contract — the skeleton file plus the "
        "bead's Cases section together specify what must be tested."
    )
elif columbo_gap:
    reason = (
        "SABLE bead quality (columbo-test-gap): Description missing required sections "
        f"per templates/columbo-bead.md — {missing_text}. Fix the description and retry. "
        "Columbo gap beads must include a Fingerprint to survive line drift between audit "
        "and execution."
    )
else:
    reason = (
        f"SABLE bead quality: Description missing — {missing_text}. Manager context "
        "requires beads pass the Fresh Agent Test before creation. Add the missing "
        "sections and retry."
    )
deny(reason)
PY
