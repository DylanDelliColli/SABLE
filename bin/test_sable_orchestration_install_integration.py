#!/usr/bin/env python3
"""Integration tests for the hook sibling-library closure (SABLE-nn54x).

REAL COMPOSITION, NO FIXTURES: the REAL bin/sable-orchestration-install is run
against the REAL repo into a throwaway scope, and then the REAL installed
hooks/multi-manager/inline-body-guard.sh is executed with REAL PreToolUse JSON
on stdin. Nothing is mocked and nothing synthetic stands in for the hook, its
library, or the resolution between them -- the defect lived precisely in that
resolution, so a fixture in the middle would test nothing.

WHY THE INSTALLED PATH IS THE ONLY PATH THAT COUNTS HERE. From the repo the
hook's "$HOOK_DIR/../../bin/<lib>.py" lands on repo/bin/ and has always
worked; from the installed copy it lands on BASE/bin/, which the installer
never created. Every test that drove the REPO copy passed throughout, which
is how the guard came to fire on every Bash call fleet-wide while checking
nothing.

THE SCOPE IS ALWAYS A tmp_path. This suite must never install into the real
~/.claude: landing a hook or a settings row on the developer's live scope is
an unbrokered activation.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "bin" / "sable-orchestration-install"
LIB_NAME = "sable_inline_body_guard_lib.py"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="installer and hook are bash+python3; not available in this clean room",
)

CORRUPT_BODY = 'bd update SABLE-abc123 --append-notes "ran `bd hooks install` by accident"'
CLEAN_BODY = 'bd update SABLE-abc123 --append-notes "a plain sentence, no danger here"'


@pytest.fixture
def installed_scope(tmp_path):
    """Run the real installer into a throwaway --user scope; yield its BASE."""
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


def test_installed_inline_body_guard_can_load_its_library_and_denies_a_corrupt_body(installed_scope):
    # THE ASSERTION THAT FAILED BEFORE THIS FIX, and the one that would have
    # caught the original defect: not "is the hook there" but "can the hook
    # reach its own dependency from where it was installed".
    out = run_installed_guard(installed_scope, CORRUPT_BODY)
    assert out.get("permissionDecision") == "deny", out
    assert "--file" in out.get("permissionDecisionReason", "")


def test_installed_hook_allows_a_clean_body(installed_scope):
    # BOTH POLARITIES. Without this, a hook that denies everything -- or one
    # that cannot load its library and fails open on the deny case for the
    # wrong reason -- passes the test above.
    #
    # The hook's ordinary allow is SILENCE: exit 0 with no JSON at all. It
    # speaks only to deny, or to announce a could-not-assess. So an empty
    # payload here is the assertion, and it is also what distinguishes a real
    # allow from the fail-open allow the next test provokes.
    out = run_installed_guard(installed_scope, CLEAN_BODY)
    assert out == {}, out


def test_the_library_lands_where_the_installed_hook_resolves_it(installed_scope):
    hook_dir = installed_scope / "hooks" / "multi-manager"
    resolved = Path(os.path.normpath(hook_dir / ".." / ".." / "bin" / LIB_NAME))
    assert resolved.is_file(), f"dependency closure incomplete: {resolved} absent"
    assert resolved.read_bytes() == (REPO / "bin" / LIB_NAME).read_bytes()


def test_removing_the_installed_library_reproduces_the_wired_but_inert_state(installed_scope):
    """Plant-and-fail, kept permanently.

    Deleting the installed library must turn the SAME corrupt body from a deny
    into a loud could-not-assess allow. This proves two things at once: the
    deny above genuinely depends on the closure (so it cannot be passing for an
    unrelated reason), and the failure mode is still fail-OPEN-and-LOUD rather
    than fail-closed, which is what kept the original incident to seconds
    instead of refusing every bd call in the fleet at once.
    """
    hook_dir = installed_scope / "hooks" / "multi-manager"
    resolved = Path(os.path.normpath(hook_dir / ".." / ".." / "bin" / LIB_NAME))
    assert run_installed_guard(installed_scope, CORRUPT_BODY)["permissionDecision"] == "deny"

    resolved.unlink()

    out = run_installed_guard(installed_scope, CORRUPT_BODY)
    assert out.get("permissionDecision") == "allow", "guard failed CLOSED — it must fail open"
    assert "COULD NOT ASSESS" in out.get("additionalContext", ""), \
        "guard passed the command SILENTLY; the could-not-assess banner is the whole reason this was caught"


def test_installed_guard_is_registered_in_the_scope_settings(installed_scope):
    # Wiring and closure are separate rungs (SABLE-xbwo2 vs this bead). A guard
    # that loads its library but is named by no settings row never fires; a
    # guard that is registered but cannot load its library fires and checks
    # nothing. Assert both, or one rung's green light masks the other's red.
    data = json.loads((installed_scope / "settings.json").read_text())
    commands = [
        h.get("command", "")
        for blocks in data.get("hooks", {}).values()
        for b in blocks
        for h in b.get("hooks", [])
    ]
    assert any("inline-body-guard.sh" in c for c in commands), commands
