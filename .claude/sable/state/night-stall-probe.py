#!/usr/bin/env python3
"""Read-only fleet stall probe invoked by the cockpit's active contract.

Exit 0 means observable work/hold posture is healthy, 1 means at least one
manager has a confirmed dropped wake while the fleet is below capacity, and 2
means the verdict genuinely cannot be assessed. The driver owns transport and
rendering only; pane-state and verdict semantics live in the tracked bin libs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


WORKER_CAP = 4  # in-progress bead ceiling used by the operator contract
TOOL_TIMEOUT_SECONDS = 30


def _repo_bin() -> Path:
    return Path(__file__).resolve().parents[3] / "bin"


def _run(argv: list[str]) -> tuple[str | None, str | None]:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"could not run {argv[0]}: {exc}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no detail"
        return None, f"{' '.join(argv)} exited {result.returncode}: {detail}"
    return result.stdout, None


def _json_list_count(argv: list[str], label: str) -> tuple[int | None, str | None]:
    raw, error = _run(argv)
    if error:
        return None, f"{label}: {error}"
    try:
        value: Any = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError) as exc:
        return None, f"{label}: invalid JSON: {exc}"
    if not isinstance(value, list):
        return None, f"{label}: expected a JSON list, got {type(value).__name__}"
    return len(value), None


def _provider_map(
    pane_rows: str,
    resolved: dict[str, str],
    roles: tuple[str, ...],
    normalize_provider,
) -> tuple[dict[str, str], list[str]]:
    """Resolve the provider stamped on the same pane/role registry row."""
    providers: dict[str, str] = {}
    errors: list[str] = []
    for line in pane_rows.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        pane, role = parts[:2]
        if role not in roles or resolved.get(role) != pane:
            continue
        if len(parts) < 3 or not parts[2].strip():
            errors.append(
                f"{role} pane {pane} has no @sable_provider registration"
            )
            continue
        try:
            providers[role] = normalize_provider(parts[2])
        except Exception as exc:  # provider library owns the exact taxonomy
            errors.append(f"{role} pane {pane} has invalid @sable_provider: {exc}")
    for role, pane in resolved.items():
        if role not in providers and not any(
            reason.startswith(f"{role} pane {pane} ") for reason in errors
        ):
            errors.append(f"{role} pane {pane} has no @sable_provider registration")
    return providers, errors


def _dependency_failure(exc: BaseException) -> int:
    print(f"COULD-NOT-ASSESS: stall-probe dependency unavailable: {exc}")
    print("VERDICT: DEGRADED")
    return 2


def main() -> int:
    repo_bin = _repo_bin()
    required = (
        "sable_pane_lib.py",
        "sable_provider_lib.py",
        "sable_stall_probe_lib.py",
    )
    missing = [Path(name).stem for name in required if not (repo_bin / name).is_file()]
    if missing:
        print(
            "COULD-NOT-ASSESS: stall-probe dependencies unavailable: "
            + ", ".join(missing)
        )
        print("VERDICT: DEGRADED")
        return 2

    sys.path.insert(0, str(repo_bin))
    try:
        from sable_pane_lib import tmux_base
        from sable_provider_lib import normalize_provider
        from sable_stall_probe_lib import (
            MANAGER_ROLES,
            deliberate_hold,
            manager_axis_states,
            resolve_manager_panes,
            stall_verdict,
        )
    except Exception as exc:
        return _dependency_failure(exc)

    tmux = tmux_base(os.environ.get("SABLE_TMUX_SOCKET") or None)
    pane_rows, pane_error = _run(
        [
            *tmux,
            "list-panes",
            "-a",
            "-F",
            "#{pane_id} #{@sable_role} #{@sable_provider}",
        ]
    )
    hard_unassessed: list[str] = []
    if pane_error:
        hard_unassessed.append(f"manager registry: {pane_error}")
        pane_rows = ""

    resolved = resolve_manager_panes(pane_rows or "")
    providers, provider_errors = _provider_map(
        pane_rows or "", resolved, MANAGER_ROLES, normalize_provider
    )
    hard_unassessed.extend(provider_errors)

    captures: dict[str, str] = {}

    def capture(pane: str) -> tuple[str | None, str | None]:
        if pane in captures:
            return captures[pane], None
        text, error = _run(
            [*tmux, "capture-pane", "-p", "-J", "-e", "-t", pane]
        )
        if text is not None:
            captures[pane] = text
        return text, error

    states, state_errors = manager_axis_states(resolved, capture)
    hard_unassessed.extend(state_errors)

    held: dict[str, bool | None] = {}
    hold_unassessed: list[str] = []
    for role in MANAGER_ROLES:
        if states.get(role) != "IDLE":
            continue
        pane = resolved.get(role)
        provider = providers.get(role)
        if pane is None or provider is None:
            continue
        text, error = capture(pane)
        if text is None:
            if error:
                hard_unassessed.append(f"{role} hold axis: {error}")
            continue
        decision = deliberate_hold(text, provider)
        held[role] = decision
        if decision is None:
            hold_unassessed.append(
                f"{role} pane {pane}: hold unassessed; the composer is active, "
                "not locatable, or contains non-SABLE text"
            )

    ready, ready_error = _json_list_count(
        ["bd", "ready", "--json", "--limit", "0"], "ready pool"
    )
    if ready_error:
        hard_unassessed.append(ready_error)
    in_flight, in_flight_error = _json_list_count(
        ["bd", "list", "--status", "in_progress", "--json", "--limit", "0"],
        "in-progress pool",
    )
    if in_flight_error:
        hard_unassessed.append(in_flight_error)

    verdict, exit_code = stall_verdict(
        states,
        held,
        ready,
        in_flight,
        WORKER_CAP,
        degraded=hard_unassessed,
        hold_unassessed=hold_unassessed,
    )

    print(f"manager states : {states}")
    print(f"hold decisions: {held}")
    print(f"ready={ready!r} in_flight={in_flight!r} cap={WORKER_CAP}")
    for reason in hard_unassessed:
        print(f"COULD-NOT-ASSESS: {reason}")
    for reason in hold_unassessed:
        print(f"UNASSESSED HOLD AXIS: {reason}")
    print(f"VERDICT: {verdict}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
