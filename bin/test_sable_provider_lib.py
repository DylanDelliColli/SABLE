import pytest

from sable_provider_lib import (
    ProviderMapError,
    agent_name,
    agent_role,
    default_provider_map,
    format_provider_map,
    normalize_provider,
    parse_provider_map,
    pane_environment,
    provider_for_role,
    provider_map_from_state,
    provider_model,
    interactive_command,
    execution_provider,
    execution_provider_map,
    provider_boot_message,
    validate_provider_capabilities,
)


def test_default_map_is_all_claude_in_canonical_roles():
    assert default_provider_map() == {
        "optimus": "claude",
        "tarzan": "claude",
        "chuck": "claude",
        "worker": "claude",
    }


def test_parse_partial_map_fills_defaults_and_normalizes_case():
    assert parse_provider_map(" worker=CoDeX, tarzan=CLAUDE ") == {
        "optimus": "claude",
        "tarzan": "claude",
        "chuck": "claude",
        "worker": "codex",
    }


@pytest.mark.parametrize(
    "value",
    [
        "worker",
        "worker=",
        "=codex",
        "lincoln=codex",
        "worker=openai",
        "worker=codex,worker=claude",
    ],
)
def test_parse_rejects_ambiguous_or_unsupported_entries(value):
    with pytest.raises(ProviderMapError):
        parse_provider_map(value)


def test_legacy_state_defaults_to_all_claude():
    assert provider_map_from_state({"mode": "execution"}) == default_provider_map()


def test_state_rejects_unknown_role_and_invalid_provider():
    with pytest.raises(ProviderMapError):
        provider_map_from_state({"providers": {"lincoln": "codex"}})
    with pytest.raises(ProviderMapError):
        provider_map_from_state({"providers": {"worker": "openai"}})


def test_format_and_role_lookup_have_stable_interface():
    state = {"providers": {"worker": "codex", "tarzan": "codex"}}
    assert format_provider_map(provider_map_from_state(state)) == (
        "optimus=claude,tarzan=codex,chuck=claude,worker=codex"
    )
    assert provider_for_role(state, "worker") == "codex"


def test_identity_prefers_sable_variables_then_falls_back_to_claude():
    env = {
        "SABLE_AGENT_NAME": "tarzan",
        "SABLE_AGENT_ROLE": "manager",
        "CLAUDE_AGENT_NAME": "wrong",
        "CLAUDE_AGENT_ROLE": "producer",
    }
    assert agent_name(env) == "tarzan"
    assert agent_role(env) == "manager"
    assert agent_name({"CLAUDE_AGENT_NAME": "optimus"}) == "optimus"
    assert agent_role({"CLAUDE_AGENT_ROLE": "manager"}) == "manager"


def test_pane_environment_dual_stamps_claude_but_not_codex():
    assert pane_environment("claude", "optimus", "manager") == {
        "SABLE_PROVIDER": "claude",
        "SABLE_AGENT_NAME": "optimus",
        "SABLE_AGENT_ROLE": "manager",
        "CLAUDE_AGENT_NAME": "optimus",
        "CLAUDE_AGENT_ROLE": "manager",
    }
    assert pane_environment("codex", "tarzan", "manager") == {
        "SABLE_PROVIDER": "codex",
        "SABLE_AGENT_NAME": "tarzan",
        "SABLE_AGENT_ROLE": "manager",
    }


def test_provider_normalization_defaults_to_claude_and_rejects_unknown():
    assert normalize_provider(None) == "claude"
    assert normalize_provider(" CoDeX ") == "codex"
    with pytest.raises(ProviderMapError):
        normalize_provider("openai")


def test_provider_model_translates_existing_tier_names():
    assert provider_model("claude", "sonnet") == ("sonnet", None)
    assert provider_model("codex", "haiku") == ("gpt-5.6-terra", "low")
    assert provider_model("codex", "opus") == ("gpt-5.6-sol", "high")


def test_interactive_codex_command_supports_the_worker_lifecycle():
    command = interactive_command("codex", "sonnet")
    assert command.startswith("codex --no-alt-screen")
    assert "--dangerously-bypass-hook-trust" in command
    assert "--sandbox workspace-write" in command
    assert "--ask-for-approval never" in command
    assert "sandbox_workspace_write.network_access=true" in command
    assert "shell_environment_policy.inherit=all" in command
    assert "model_reasoning_effort=" in command
    assert " exec " not in command


def test_codex_command_grants_writable_roots_for_git(tmp_path):
    """SABLE-82k8m: Codex's workspace-write sandbox makes .git READ-ONLY even
    though it sits INSIDE the workspace — a deliberate, separate exclusion,
    proven by probe: granting an unrelated root leaves .git blocked while
    naming it explicitly unblocks it. Without this grant `git fetch` dies on
    FETCH_HEAD and no worker can self-push."""
    command = interactive_command(
        "codex", "sonnet", cwd=str(tmp_path),
        writable_roots=["/repo/.git"],
    )
    assert "sandbox_workspace_write.writable_roots=" in command
    assert "/repo/.git" in command


def test_codex_command_omits_writable_roots_when_none_requested():
    """No grant unless asked — the sandbox stays as tight as it started."""
    command = interactive_command("codex", "sonnet")
    assert "writable_roots" not in command


def test_claude_command_ignores_writable_roots(tmp_path):
    """REGRESSION: writable_roots is a Codex sandbox concept. It must never
    leak into the Claude command, which has no such flag."""
    command = interactive_command(
        "claude", "sonnet", cwd=str(tmp_path),
        writable_roots=["/repo/.git"],
    )
    assert "writable_roots" not in command
    assert "/repo/.git" not in command


def test_execution_provider_reads_frozen_state(tmp_path, monkeypatch):
    state = tmp_path / "mode.json"
    state.write_text(
        '{"mode":"execution","providers":{"optimus":"codex","worker":"codex"}}'
    )
    monkeypatch.setenv("SABLE_MODE_STATE", str(state))
    assert execution_provider("optimus") == "codex"
    assert execution_provider("tarzan") == "claude"


def test_execution_provider_rejects_nonexecution_state(tmp_path, monkeypatch):
    state = tmp_path / "mode.json"
    state.write_text('{"mode":"planning"}')
    monkeypatch.setenv("SABLE_MODE_STATE", str(state))
    with pytest.raises(ProviderMapError, match="execution mode"):
        execution_provider("worker")
    assert execution_provider_map() is None


def test_execution_provider_map_fails_closed_on_corrupt_state(tmp_path, monkeypatch):
    state = tmp_path / "mode.json"
    state.write_text("{broken")
    monkeypatch.setenv("SABLE_MODE_STATE", str(state))
    with pytest.raises(ProviderMapError, match="cannot read"):
        execution_provider_map()


def test_supported_providers_declare_required_fleet_capabilities():
    validate_provider_capabilities("claude")
    validate_provider_capabilities("codex")


def test_codex_boot_message_anchors_the_installed_role_card(tmp_path):
    role = tmp_path / ".claude/sable/roles/optimus.md"
    role.parent.mkdir(parents=True)
    role.write_text("# Optimus")
    message = provider_boot_message("codex", "optimus", "begin", base=str(tmp_path))
    assert str(role) in message
    assert message.endswith("begin")
    assert provider_boot_message("claude", "optimus", "begin", base=str(tmp_path)) == "begin"
