#!/usr/bin/env python3
"""Integration tests for installed hook behavior (SABLE-nn54x / SABLE-4cad2).

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

SABLE-4cad2 adds the other installed-path seam: notes-clobber-guard must emit a
valid, loud fail-open decision when `bd` is absent from the hook's PATH.

THE SCOPE IS ALWAYS A tmp_path. This suite must never install into the real
~/.claude: landing a hook or a settings row on the developer's live scope is
an unbrokered activation.
"""
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "bin" / "sable-orchestration-install"
LIB_NAME = "sable_inline_body_guard_lib.py"
BASE_SNIPPET = REPO / "templates" / "base-settings-snippet.json"
BASE_HOOKS = {
    "tdd-evidence.sh": ("PreToolUse", "Bash", 3000),
    "tdd-gate.sh": ("PreToolUse", "Bash", 5000),
    "bead-description-gate.sh": ("PreToolUse", "Bash", 3000),
    "tdd-remind.sh": ("PreToolUse", "Edit|Write", 3000),
    "agent-tdd-enforce.sh": ("PreToolUse", "Agent", 3000),
    "bead-quality.sh": ("PostToolUse", "Bash", 5000),
}

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
        ["bash", str(INSTALLER), "--user", "--merge-settings"],
        env={**os.environ, "CLAUDE_USER_DIR": str(base)},
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, f"installer failed:\n{result.stdout}\n{result.stderr}"
    return base


@pytest.fixture
def installed_notes_scope(tmp_path):
    """Install the notes guard with every user-owned path fully isolated."""
    base = tmp_path / "notes-claude"
    home = tmp_path / "notes-home"
    codex_home = tmp_path / "notes-codex"
    base.mkdir()
    home.mkdir()
    codex_home.mkdir()
    result = subprocess.run(
        ["bash", str(INSTALLER), "--user"],
        env={
            **os.environ,
            "HOME": str(home),
            "CLAUDE_USER_DIR": str(base),
            "CODEX_HOME": str(codex_home),
            "SABLE_REPO_DIR": str(REPO),
        },
        capture_output=True,
        text=True,
        timeout=180,
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


def run_real_install(base, merge_settings=True):
    """Run the real installer against an isolated user scope."""
    settings_args = ["--merge-settings"] if merge_settings else []
    return subprocess.run(
        ["bash", str(INSTALLER), "--user", *settings_args],
        env={**os.environ, "CLAUDE_USER_DIR": str(base)},
        capture_output=True, text=True, timeout=180,
    )


def run_project_install(project, codex_home, *args):
    """Run the real installer with every writable destination isolated."""
    return subprocess.run(
        ["bash", str(INSTALLER), "--project", *args],
        env={
            **os.environ,
            "SABLE_PROJECT_DIR": str(project),
            "CODEX_HOME": str(codex_home),
        },
        capture_output=True, text=True, timeout=180,
    )


def codex_hooks_path(base):
    return base.parent / ".codex" / "hooks.json"


def hook_rows(path):
    data = json.loads(path.read_text())
    return [
        (event, block.get("matcher", ""), hook)
        for event, blocks in data.get("hooks", {}).items()
        for block in blocks
        for hook in block.get("hooks", [])
    ]


def no_bd_path(tmp_path):
    """A minimal hook toolchain that cannot inherit a host bd binary."""
    path = tmp_path / "no-bd-bin"
    path.mkdir()
    for command in ("bash", "cat", "head", "python3"):
        source = shutil.which(command)
        assert source is not None, f"required hook command is absent: {command}"
        (path / command).symlink_to(Path(source).resolve())
    assert shutil.which("bd", path=str(path)) is None
    return path


def test_installed_live_symlink_resolves_artifacts_from_source_checkout(tmp_path):
    """A PATH lookup through relative link hops derives the physical checkout."""
    staged_bin = tmp_path / "staged" / "bin"
    staged_bin.mkdir(parents=True)
    staged_installer = staged_bin / INSTALLER.name
    staged_installer.symlink_to(os.path.relpath(INSTALLER, staged_bin))

    installed_bin = tmp_path / "local" / "bin"
    installed_bin.mkdir(parents=True)
    installed_installer = installed_bin / INSTALLER.name
    installed_installer.symlink_to(os.path.relpath(staged_installer, installed_bin))

    base = tmp_path / "claude"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_USER_DIR": str(base),
        "CODEX_HOME": str(tmp_path / "codex"),
        "PATH": str(installed_bin) + os.pathsep + os.environ["PATH"],
    }
    env.pop("SABLE_REPO_DIR", None)

    result = subprocess.run(
        [INSTALLER.name, "--user"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, (
        f"live-symlink installer failed:\n{result.stdout}\n{result.stderr}"
    )
    installed_agents = base / "sable" / "agents.yaml"
    installed_hook = base / "hooks" / "multi-manager" / "mode-interlock.sh"
    assert installed_agents.read_bytes() == (
        REPO / "templates" / "multi-manager" / "agents.yaml"
    ).read_bytes()
    assert installed_hook.read_bytes() == (
        REPO / "hooks" / "multi-manager" / "mode-interlock.sh"
    ).read_bytes()


def test_invalid_explicit_repo_refuses_before_install_writes(tmp_path):
    invalid_repo = tmp_path / "not-a-sable-checkout"
    invalid_repo.mkdir()
    base = tmp_path / "claude"

    result = subprocess.run(
        [str(INSTALLER), "--user"],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "CLAUDE_USER_DIR": str(base),
            "CODEX_HOME": str(tmp_path / "codex"),
            "SABLE_REPO_DIR": str(invalid_repo),
        },
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode != 0
    diagnostic = result.stdout + result.stderr
    assert "invalid SABLE_REPO_DIR" in diagnostic
    assert str(invalid_repo) in diagnostic
    assert "templates" in diagnostic
    assert not base.exists(), "source validation ran after install writes began"


def test_partial_repo_with_empty_hook_and_skill_dirs_refuses_before_writes(tmp_path):
    """Directory-shaped placeholders must not satisfy source validation."""
    partial_repo = tmp_path / "partial-sable-checkout"
    required_templates = (
        "templates/multi-manager/agents.yaml",
        "templates/multi-manager/settings-snippet.json",
        "templates/multi-manager/roles/lincoln.md",
        "templates/multi-manager/roles/optimus.md",
        "templates/multi-manager/roles/tarzan.md",
        "templates/multi-manager/roles/chuck.md",
    )
    for relative in required_templates:
        destination = partial_repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, destination)
    (partial_repo / "hooks" / "multi-manager").mkdir(parents=True)
    (partial_repo / "skills").mkdir()
    base = tmp_path / "claude"

    result = subprocess.run(
        [str(INSTALLER), "--user"],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "CLAUDE_USER_DIR": str(base),
            "CODEX_HOME": str(tmp_path / "codex"),
            "SABLE_REPO_DIR": str(partial_repo),
        },
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode != 0
    diagnostic = result.stdout + result.stderr
    assert "mode-interlock.sh" in diagnostic
    assert "skills/*/SKILL.md" in diagnostic
    assert not base.exists(), "partial source validation ran after install writes began"


def test_canonical_base_snippet_has_the_complete_gate_graph():
    rows = hook_rows(BASE_SNIPPET)
    actual = {}
    for event, matcher, hook in rows:
        command = hook.get("command", "")
        for name in BASE_HOOKS:
            if command.endswith("/" + name):
                actual[name] = (event, matcher, hook.get("timeout"))
    assert actual == BASE_HOOKS
    lifecycle_commands = [
        (event, hook.get("command", "")) for event, _, hook in rows
    ]
    assert lifecycle_commands.count(
        ("SessionStart", "sable-doctor --quiet 2>&1 || true")
    ) == 1
    assert lifecycle_commands.count(("SessionStart", "bd prime")) == 1
    assert lifecycle_commands.count(("PreCompact", "bd prime")) == 1


def test_direct_real_install_is_print_only_without_consent(tmp_path):
    base = tmp_path / "claude"
    base.mkdir()
    settings = base / "settings.json"
    original = b'{\n  "permissions": {"allow": ["Read"]}\n}\n'
    settings.write_bytes(original)
    codex_hooks = codex_hooks_path(base)
    codex_hooks.parent.mkdir()
    codex_original = b'{\n  "hooks": {"CustomEvent": []}\n}\n'
    codex_hooks.write_bytes(codex_original)

    result = run_real_install(base, merge_settings=False)

    assert result.returncode == 0, result.stderr
    assert settings.read_bytes() == original
    assert codex_hooks.read_bytes() == codex_original
    assert "tdd-gate.sh" in result.stdout
    assert "inline-body-guard.sh" in result.stdout
    assert "NOT APPLIED" in result.stdout
    assert (base / "hooks" / "multi-manager" / "inline-body-guard.sh").is_file()
    assert not list(base.glob(".install-bak-*"))
    assert not settings.with_suffix(".json.bak").exists()


def test_project_merge_reports_codex_scope_mismatch_without_writing_codex(tmp_path):
    """REGRESSION (priority<=1): a successful project merge must not hide Codex."""
    project = tmp_path / "project"
    codex_home = tmp_path / "codex"
    project.mkdir()

    result = run_project_install(
        project, codex_home, "--merge-settings",
    )

    assert result.returncode == 0, result.stderr
    assert not (codex_home / "hooks.json").exists()
    assert (
        "Codex lifecycle hooks are user-scoped and were not installed. "
        "Re-run: sable-orchestration-install --user --merge-settings"
    ) in result.stdout


def test_malformed_settings_refuses_before_install_and_preserves_exact_bytes(tmp_path):
    base = tmp_path / "claude"
    base.mkdir()
    settings = base / "settings.json"
    original = b'{"hooks": {"PreToolUse": [}\n'
    settings.write_bytes(original)

    result = run_real_install(base)

    assert result.returncode != 0, "malformed live settings were silently replaced"
    assert settings.read_bytes() == original
    assert settings.with_suffix(".json.bak").read_bytes() == original
    diagnostic = result.stdout + result.stderr
    assert "REFUSED" in diagnostic
    assert "invalid JSON" in diagnostic
    assert str(settings) in diagnostic
    assert str(settings.with_suffix(".json.bak")) in diagnostic
    assert not any((base / "hooks" / "multi-manager").glob("*.sh")), \
        "settings validation happened after install artifacts were already copied"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores file mode, so unreadable settings cannot be simulated")
def test_unreadable_settings_refuses_before_install_and_preserves_exact_bytes(tmp_path):
    base = tmp_path / "claude"
    base.mkdir()
    settings = base / "settings.json"
    original = b'{"hooks": {"custom": []}}\n'
    settings.write_bytes(original)
    settings.chmod(0o000)

    try:
        result = run_real_install(base)
    finally:
        settings.chmod(0o600)

    assert result.returncode != 0
    assert settings.read_bytes() == original
    diagnostic = result.stdout + result.stderr
    assert "REFUSED" in diagnostic
    assert "could not read or back up" in diagnostic
    assert str(settings) in diagnostic
    assert str(settings.with_suffix(".json.bak")) in diagnostic
    assert not any((base / "hooks" / "multi-manager").glob("*.sh"))


def test_valid_settings_merge_is_idempotent_and_preserves_unrelated_hooks(tmp_path):
    base = tmp_path / "claude"
    base.mkdir()
    settings = base / "settings.json"
    custom_hook = {
        "matcher": "CustomTool",
        "hooks": [{"type": "command", "command": "run-my-private-hook"}],
    }
    settings.write_text(json.dumps({
        "permissions": {"allow": ["Read"]},
        "hooks": {"PreToolUse": [custom_hook]},
    }, indent=2) + "\n")
    codex_hooks = codex_hooks_path(base)
    codex_hooks.parent.mkdir()
    codex_custom = {
        "matcher": "CustomTool",
        "hooks": [{"type": "command", "command": "run-my-codex-hook"}],
    }
    codex_hooks.write_text(json.dumps({
        "codex_setting": "preserve-me",
        "hooks": {"PreToolUse": [codex_custom]},
    }, indent=2) + "\n")

    first = run_real_install(base)
    assert first.returncode == 0, first.stderr
    first_bytes = settings.read_bytes()
    first_data = json.loads(first_bytes)
    assert first_data["permissions"] == {"allow": ["Read"]}
    assert custom_hook in first_data["hooks"]["PreToolUse"]
    first_codex_bytes = codex_hooks.read_bytes()
    first_codex = json.loads(first_codex_bytes)
    assert first_codex["codex_setting"] == "preserve-me"
    assert codex_custom in first_codex["hooks"]["PreToolUse"]
    codex_commands = [
        hook.get("command", "")
        for _, _, hook in hook_rows(codex_hooks)
    ]
    for name in BASE_HOOKS:
        assert sum(command.endswith("/" + name) for command in codex_commands) == 1
        assert (base / "hooks" / name).is_file()

    second = run_real_install(base)
    assert second.returncode == 0, second.stderr
    assert settings.read_bytes() == first_bytes
    assert codex_hooks.read_bytes() == first_codex_bytes
    second_data = json.loads(settings.read_bytes())
    commands = [
        hook.get("command")
        for block in second_data["hooks"]["PreToolUse"]
        for hook in block.get("hooks", [])
    ]
    assert commands.count("run-my-private-hook") == 1


def test_codex_tdd_gate_wiring_denies_without_and_allows_with_evidence(installed_scope):
    codex_hooks = codex_hooks_path(installed_scope)
    commands = [
        hook.get("command", "")
        for _, _, hook in hook_rows(codex_hooks)
    ]
    expected_gate = f"bash {installed_scope}/hooks/tdd-gate.sh"
    expected_evidence = f"bash {installed_scope}/hooks/tdd-evidence.sh"
    assert commands.count(expected_gate) == 1
    assert commands.count(expected_evidence) == 1

    session_id = f"codex-base-canary-{os.getpid()}-{uuid.uuid4().hex}"
    close_payload = json.dumps({
        "tool_input": {"command": "bd close SABLE-canarya SABLE-canaryb"},
        "session_id": session_id,
    })
    denied = subprocess.run(
        ["bash", str(installed_scope / "hooks" / "tdd-gate.sh")],
        input=close_payload, capture_output=True, text=True, timeout=60,
    )
    assert denied.returncode == 0, denied.stderr
    decision = json.loads(denied.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"

    evidence_payload = json.dumps({
        "tool_input": {"command": "python -m pytest bin/test_example.py -q"},
        "session_id": session_id,
    })
    observed = subprocess.run(
        ["bash", str(installed_scope / "hooks" / "tdd-evidence.sh")],
        input=evidence_payload, capture_output=True, text=True, timeout=60,
    )
    assert observed.returncode == 0, observed.stderr
    allowed = subprocess.run(
        ["bash", str(installed_scope / "hooks" / "tdd-gate.sh")],
        input=close_payload, capture_output=True, text=True, timeout=60,
    )
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout == ""


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


def test_installed_notes_clobber_guard_no_bd_door_is_loud(
    installed_notes_scope, tmp_path,
):
    hook = (
        installed_notes_scope / "hooks" / "multi-manager"
        / "notes-clobber-guard.sh"
    )
    assert hook.is_file(), f"guard was not installed at {hook}"
    path = no_bd_path(tmp_path)
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {
            "command": 'bd update SABLE-canary --notes "replacement"',
        },
        "hook_event_name": "PreToolUse",
    })
    result = subprocess.run(
        [str(path / "bash"), str(hook)],
        input=payload,
        env={**os.environ, "PATH": str(path)},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "allow"
    context = output["additionalContext"]
    assert "COULD NOT ASSESS" in context
    assert "not available on the hook's PATH" in context


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
