"""Provider policy shared by SABLE's mode and pane orchestration.

The provider map is execution-session intent, not a dispatch-time heuristic:
each resident execution role has a provider and every worker in the session
shares one provider.  This module owns validation, defaulting, and stable
rendering so launchers and hooks do not grow their own subtly different maps.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

from sable_mode_store_lib import (
    ModeStateCorrupt,
    ModeStateMissing,
    read_mode_state,
    resolve_mode_state_path,
)

PROVIDER_ROLES = ("optimus", "tarzan", "chuck", "worker")
SUPPORTED_PROVIDERS = frozenset({"claude", "codex"})
DEFAULT_PROVIDER = "claude"
REQUIRED_CAPABILITIES = frozenset({
    "interactive_tui",
    "pane_messaging",
    "lifecycle_hooks",
    "workspace_write",
    "network",
})
PROVIDER_CAPABILITIES = {
    "claude": REQUIRED_CAPABILITIES,
    "codex": REQUIRED_CAPABILITIES,
}
MODEL_TIERS = {
    "claude": {
        "fast": "haiku", "balanced": "sonnet", "deep": "opus",
        "haiku": "haiku", "sonnet": "sonnet", "opus": "opus",
    },
    "codex": {
        "fast": "gpt-5.6-terra", "balanced": "gpt-5.6-sol",
        "deep": "gpt-5.6-sol", "haiku": "gpt-5.6-terra",
        "sonnet": "gpt-5.6-sol", "opus": "gpt-5.6-sol",
    },
}
CODEX_REASONING = {
    "fast": "low", "balanced": "medium", "deep": "high",
    "haiku": "low", "sonnet": "medium", "opus": "high",
}


class ProviderMapError(ValueError):
    """The provider map cannot describe a valid execution session."""


CODEX_HOOK_GRAPH_REMEDY = (
    "sable-orchestration-install --user --merge-settings"
)


def codex_hook_graph_path(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the user-scope Codex hook graph exactly as the installer does."""

    env = os.environ if environment is None else environment
    codex_home = (env.get("CODEX_HOME") or "").strip()
    if codex_home:
        return Path(codex_home) / "hooks.json"
    claude_user_dir = (env.get("CLAUDE_USER_DIR") or "").strip()
    if claude_user_dir:
        return Path(claude_user_dir).parent / ".codex" / "hooks.json"
    home = (env.get("HOME") or str(Path.home())).strip()
    return Path(home) / ".codex" / "hooks.json"


def validate_codex_hook_graph(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Fail closed unless the installed Codex hook graph is non-empty JSON."""

    path = codex_hook_graph_path(environment)
    try:
        raw = path.read_text()
    except FileNotFoundError as exc:
        raise ProviderMapError(
            f"Codex hook graph is missing at {path}; install it with: "
            f"{CODEX_HOOK_GRAPH_REMEDY}"
        ) from exc
    except OSError as exc:
        raise ProviderMapError(
            f"Codex hook graph cannot be read at {path}: {exc}; repair it with: "
            f"{CODEX_HOOK_GRAPH_REMEDY}"
        ) from exc
    if not raw.strip():
        raise ProviderMapError(
            f"Codex hook graph is empty at {path}; install it with: "
            f"{CODEX_HOOK_GRAPH_REMEDY}"
        )
    try:
        graph = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderMapError(
            f"Codex hook graph is malformed JSON at {path}: {exc.msg}; "
            f"repair it with: {CODEX_HOOK_GRAPH_REMEDY}"
        ) from exc
    if not isinstance(graph, dict) or not graph:
        raise ProviderMapError(
            f"Codex hook graph is empty at {path}; install it with: "
            f"{CODEX_HOOK_GRAPH_REMEDY}"
        )
    return path


def default_provider_map() -> dict[str, str]:
    return {role: DEFAULT_PROVIDER for role in PROVIDER_ROLES}


def parse_provider_map(value: str | None) -> dict[str, str]:
    """Parse ``role=provider`` CSV and fill omitted roles with Claude.

    Duplicate entries are rejected rather than applying last-write-wins: the
    map is an immutable session contract, so ambiguous startup input must fail
    before the mode-state file is written.
    """

    result = default_provider_map()
    if value is None or not value.strip():
        return result

    seen: set[str] = set()
    for raw_entry in value.split(","):
        entry = raw_entry.strip()
        if "=" not in entry:
            raise ProviderMapError(
                f"invalid provider entry {entry!r}; expected role=claude|codex"
            )
        role, provider = (part.strip().lower() for part in entry.split("=", 1))
        if role not in PROVIDER_ROLES:
            allowed = ", ".join(PROVIDER_ROLES)
            raise ProviderMapError(
                f"unknown provider role {role!r}; expected one of: {allowed}"
            )
        if role in seen:
            raise ProviderMapError(f"duplicate provider role {role!r}")
        if provider not in SUPPORTED_PROVIDERS:
            allowed = ", ".join(sorted(SUPPORTED_PROVIDERS))
            raise ProviderMapError(
                f"invalid provider {provider!r} for {role}; expected: {allowed}"
            )
        seen.add(role)
        result[role] = provider
    return result


def provider_map_from_state(state: Mapping[str, object]) -> dict[str, str]:
    """Read a state map, treating legacy execution state as all-Claude."""

    raw = state.get("providers")
    if raw is None:
        return default_provider_map()
    if not isinstance(raw, Mapping):
        raise ProviderMapError("mode-state providers must be an object")

    result = default_provider_map()
    unknown = set(raw) - set(PROVIDER_ROLES)
    if unknown:
        raise ProviderMapError(
            "mode-state contains unknown provider role(s): "
            + ", ".join(sorted(str(role) for role in unknown))
        )
    for role, provider in raw.items():
        if not isinstance(provider, str) or provider.lower() not in SUPPORTED_PROVIDERS:
            raise ProviderMapError(f"mode-state has invalid provider for {role}")
        result[str(role)] = provider.lower()
    return result


def format_provider_map(provider_map: Mapping[str, str]) -> str:
    """Render in canonical role order for shell callers and status output."""

    normalized = provider_map_from_state({"providers": provider_map})
    return ",".join(f"{role}={normalized[role]}" for role in PROVIDER_ROLES)


def provider_for_role(state: Mapping[str, object], role: str) -> str:
    normalized_role = role.strip().lower()
    if normalized_role not in PROVIDER_ROLES:
        raise ProviderMapError(f"unknown provider role {role!r}")
    return provider_map_from_state(state)[normalized_role]


def execution_provider_map(*, base: str | None = None) -> dict[str, str] | None:
    """Return active execution providers, or None outside execution mode.

    A missing state means no execution session. A present but malformed state
    is an invariant violation and fails closed rather than selecting Claude.
    """
    path = resolve_mode_state_path(base)
    try:
        state = read_mode_state(path)
    except ModeStateMissing:
        return None
    except ModeStateCorrupt as exc:
        raise ProviderMapError(
            f"cannot read execution provider map from {path}: {exc}"
        ) from exc
    if state.get("mode") != "execution":
        return None
    return provider_map_from_state(state)


def execution_provider(role: str, *, base: str | None = None) -> str:
    """Return the provider frozen for ``role`` in active execution state."""

    providers = execution_provider_map(base=base)
    if providers is None:
        raise ProviderMapError("provider-aware panes require execution mode")
    normalized_role = role.strip().lower()
    if normalized_role not in providers:
        raise ProviderMapError(f"unknown provider role {role!r}")
    return providers[normalized_role]


def validate_provider_capabilities(provider: str) -> None:
    normalized = normalize_provider(provider)
    missing = REQUIRED_CAPABILITIES - PROVIDER_CAPABILITIES.get(
        normalized, frozenset()
    )
    if missing:
        raise ProviderMapError(
            f"provider {normalized} lacks required fleet capabilities: "
            + ", ".join(sorted(missing))
        )


def provider_model(provider: str, tier: str) -> tuple[str, str | None]:
    """Translate SABLE's existing model tiers into a provider model/effort."""

    normalized = normalize_provider(provider)
    validate_provider_capabilities(normalized)
    key = (tier or "balanced").strip().lower()
    model = MODEL_TIERS[normalized].get(key)
    if model is None:
        return tier, None
    reasoning = CODEX_REASONING.get(key) if normalized == "codex" else None
    return model, reasoning


def interactive_command(
    provider: str,
    tier: str,
    *,
    claude_permission: str = "--permission-mode bypassPermissions",
    cwd: str | None = None,
    add_dirs: Sequence[str] = (),
    writable_roots: Sequence[str] = (),
) -> str:
    """Shell command for a persistent interactive provider TUI.

    ``writable_roots`` is CODEX-ONLY and exists for one reason: Codex's
    workspace-write sandbox treats ``.git`` as read-only even though it lives
    inside the workspace — a separate exclusion from the workspace root itself,
    proven by probe (granting an unrelated root leaves ``.git`` blocked;
    naming it explicitly unblocks it). Without a grant, ``git fetch`` fails on
    FETCH_HEAD and no worker can self-push (SABLE-82k8m).

    Callers pass the roots rather than having them derived here, because the
    correct root is the git COMMON dir, not ``<cwd>/.git``: a worker runs in a
    LINKED WORKTREE where ``.git`` is a file and the real git dir lives under
    the main repo. Resolving that needs repo context this function
    deliberately does not have.
    """

    normalized = normalize_provider(provider)
    model, reasoning = provider_model(normalized, tier)
    if normalized == "claude":
        return f"claude --model {shlex.quote(model)} {claude_permission}".strip()
    argv = [
        "codex", "--no-alt-screen", "--dangerously-bypass-hook-trust",
        "--model", model,
        "--sandbox", "workspace-write",
        "--ask-for-approval", "never",
        "--config", "sandbox_workspace_write.network_access=true",
        "--config", "shell_environment_policy.inherit=all",
    ]
    if writable_roots:
        roots = ",".join(f'"{path}"' for path in writable_roots)
        argv += ["--config", f"sandbox_workspace_write.writable_roots=[{roots}]"]
    if cwd:
        argv += ["--cd", cwd]
    for path in add_dirs:
        argv += ["--add-dir", path]
    if reasoning:
        argv += ["--config", f'model_reasoning_effort="{reasoning}"']
    return shlex.join(argv)


def role_card_path(role: str, *, base: str | None = None) -> Path | None:
    """Resolve the same project-first role card used by SessionStart hooks."""

    candidates = [
        Path(base or os.getcwd()) / ".claude" / "sable" / "roles" / f"{role}.md",
        Path.home() / ".claude" / "sable" / "roles" / f"{role}.md",
    ]
    return next((path for path in candidates if path.is_file()), None)


def provider_boot_message(
    provider: str, role: str, message: str, *, base: str | None = None
) -> str:
    """Add the role-card anchor Codex does not receive from Claude hooks."""

    if normalize_provider(provider) != "codex":
        return message
    path = role_card_path(role, base=base)
    if path is None:
        raise ProviderMapError(
            f"cannot launch Codex role {role}: no installed role card found"
        )
    return (
        f"Before doing anything else, read {path} in full and adopt it as your "
        f"binding SABLE role and operating protocol. Then follow this instruction: "
        f"{message}"
    )


def normalize_provider(value: str | None) -> str:
    """Normalize a pane provider, preserving Claude as the compatibility default."""

    provider = (value or DEFAULT_PROVIDER).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ProviderMapError(f"unsupported provider {provider!r}")
    return provider


def agent_name(environment: Mapping[str, str]) -> str | None:
    """Resolve provider-neutral process identity before the Claude fallback."""

    value = environment.get("SABLE_AGENT_NAME") or environment.get("CLAUDE_AGENT_NAME")
    return value.strip() if value and value.strip() else None


def agent_role(environment: Mapping[str, str]) -> str | None:
    value = environment.get("SABLE_AGENT_ROLE") or environment.get("CLAUDE_AGENT_ROLE")
    return value.strip() if value and value.strip() else None


def pane_environment(
    provider: str,
    name: str,
    role: str,
    *,
    include_claude_aliases: bool = True,
) -> dict[str, str]:
    """Environment stamped into a SABLE-managed interactive pane.

    Claude aliases remain during migration so hooks not yet moved to the
    provider-neutral names preserve their current behavior.
    """

    normalized = normalize_provider(provider)
    env = {
        "SABLE_PROVIDER": normalized,
        "SABLE_AGENT_NAME": name,
        "SABLE_AGENT_ROLE": role,
    }
    if include_claude_aliases and normalized == "claude":
        env.update(
            {
                "CLAUDE_AGENT_NAME": name,
                "CLAUDE_AGENT_ROLE": role,
            }
        )
    return env
