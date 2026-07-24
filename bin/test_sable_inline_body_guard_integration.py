#!/usr/bin/env python3
"""Integration tests for inline-body-guard at its INSTALLED path (SABLE-jjn0d).

REAL COMPOSITION: the real bin/sable-orchestration-install copies the real
hook and its real sibling library into a throwaway scope, and the real
installed hooks/multi-manager/inline-body-guard.sh is then executed with real
PreToolUse JSON on stdin. No fixture hook, no stubbed library, no mocked
subprocess -- the whole point is the seam between the shell wrapper, the
installed copy, and the python library it resolves through.

WHY NOT THE REPO COPY. Testing only the repo copy is what let the original
activation gap hide (SABLE-nn54x): from the repo the hook's
"$HOOK_DIR/../../bin/<lib>.py" resolves and has always worked, so a
repo-driven suite stayed green for the entire time the installed guard was
firing on every Bash call and checking nothing.

THE SCOPE IS ALWAYS A tmp_path -- never the developer's live ~/.claude.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "bin" / "sable-orchestration-install"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="hook is bash+python3; not available in this clean room",
)

# The exact reproduction: a wrapper adopted specifically to avoid the hazard,
# whose own literal the calling shell corrupts before python ever exists.
WRAPPER_CORRUPT = (
    'python3 -c "import subprocess; subprocess.run([\'bd\',\'update\','
    '\'SABLE-x\',\'--append-notes\',\'Hooks run as `parented` directly\'])"'
)
# Same wrapper, same tool, no substitution hazard.
WRAPPER_CLEAN = (
    'python3 -c "import subprocess; subprocess.run([\'bd\',\'update\','
    '\'SABLE-x\',\'--append-notes\',\'Hooks run as parented directly\'])"'
)
# Hazard present, no bd/sable-msg write: the idiom that must keep working.
WRAPPER_NO_BD_WRITE = 'python3 -c "print($(date +%s))"'


@pytest.fixture
def installed_scope(tmp_path):
    base = tmp_path / "claude"
    base.mkdir()
    result = subprocess.run(
        ["bash", str(INSTALLER), "--user"],
        env={**os.environ, "CLAUDE_USER_DIR": str(base)},
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, f"installer failed:\n{result.stdout}\n{result.stderr}"
    return base


def run_installed_guard(base, command):
    """Drive the INSTALLED hook. Returns the hookSpecificOutput dict, or {}
    for the hook's ordinary allow (which is silence: exit 0, no JSON)."""
    hook = base / "hooks" / "multi-manager" / "inline-body-guard.sh"
    assert hook.is_file(), f"guard was not installed at {hook}"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    result = subprocess.run(
        ["bash", str(hook)], input=payload,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr}"
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)["hookSpecificOutput"]


def test_installed_hook_refuses_the_wrapper_shape_end_to_end(installed_scope):
    out = run_installed_guard(installed_scope, WRAPPER_CORRUPT)
    assert out.get("permissionDecision") == "deny", out
    reason = out.get("permissionDecisionReason", "")
    assert "--file" in reason and "--stdin" in reason, reason


def test_installed_hook_allows_a_clean_wrapper(installed_scope):
    # Both polarities at the installed path: the same wrapper, same tool, only
    # the hazard removed. Without this, a guard that denied every wrapper --
    # or one that could not load its library and denied for an unrelated
    # reason -- would pass the test above.
    assert run_installed_guard(installed_scope, WRAPPER_CLEAN) == {}


def test_installed_hook_allows_a_wrapper_with_a_hazard_but_no_bd_write(installed_scope):
    # The rejected-widening control, carried through to the installed path:
    # $(...) in an ordinary wrapper is the fleet's own practice, not residue.
    assert run_installed_guard(installed_scope, WRAPPER_NO_BD_WRITE) == {}


def test_installed_hook_still_refuses_the_classic_inline_body_shape(installed_scope):
    # The wrapper row is an ADDITION. Assert the original SABLE-qwthx surface
    # still bites at the installed path, so the new row cannot have been
    # traded for the old one.
    out = run_installed_guard(
        installed_scope,
        'bd update SABLE-abc123 --append-notes "ran `bd hooks install` by accident"',
    )
    assert out.get("permissionDecision") == "deny", out
