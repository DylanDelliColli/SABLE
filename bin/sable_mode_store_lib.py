"""Atomic storage authority for SABLE orchestration mode state.

The mode file is a tiny state machine, not a loose JSON document. Every
read-modify-write transition takes one adjacent advisory lock; provider
immutability is checked while that lock is held; publication is a durable
temporary-file fsync followed by one atomic replacement. Readers therefore
observe either the previous complete state or the next complete state.

Missing state means orchestration has not been activated. Present but
unreadable, malformed, or structurally invalid state is a separate failure and
must never be interpreted as missing.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MODES = frozenset({"planning", "execution"})
PLANNING_TIERS = frozenset({"quick", "full", "discovery"})
PLANNING_SUBSTAGES = (
    "framing",
    "research",
    "architecture",
    "test-strategy",
    "decomposition",
)


class ModeStateError(RuntimeError):
    """Base class for state-store failures safe for an operator to report."""


class ModeStateMissing(ModeStateError):
    """No mode-state file exists."""


class ModeStateCorrupt(ModeStateError):
    """A mode-state file exists but cannot be trusted."""


class ModeTransitionRefused(ModeStateError):
    """A valid state does not permit the requested transition."""


def resolve_mode_state_path(base: str | os.PathLike[str] | None = None) -> Path:
    """Resolve one mode-state path shared by a repository's worktrees."""

    override = os.environ.get("SABLE_MODE_STATE")
    if override:
        return Path(override)
    cwd = os.fspath(base) if base is not None else os.getcwd()
    try:
        common = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = Path(cwd) / common_path
        return (
            common_path.resolve().parent
            / ".claude/sable/state/mode-state.json"
        )
    except (OSError, subprocess.CalledProcessError):
        return Path.home() / ".claude/sable/state/mode-state.json"


def _validate_mode_state(raw: object, path: Path) -> dict:
    if not isinstance(raw, dict):
        raise ModeStateCorrupt(f"corrupt mode state at {path}: root must be an object")

    state = dict(raw)
    mode = state.get("mode")
    if mode not in MODES:
        raise ModeStateCorrupt(
            f"corrupt mode state at {path}: mode must be planning or execution"
        )

    since = state.get("since")
    if since is not None and not isinstance(since, str):
        raise ModeStateCorrupt(f"corrupt mode state at {path}: since must be a string")
    fleet = state.get("fleet")
    if fleet is not None and (
        not isinstance(fleet, list)
        or any(not isinstance(member, str) for member in fleet)
    ):
        raise ModeStateCorrupt(
            f"corrupt mode state at {path}: fleet must be a list of strings"
        )

    if mode == "planning":
        tier = state.get("tier", "full")
        if tier not in PLANNING_TIERS:
            raise ModeStateCorrupt(
                f"corrupt mode state at {path}: invalid planning tier {tier!r}"
            )
        substage = state.get("substage")
        if substage is not None and substage not in PLANNING_SUBSTAGES:
            raise ModeStateCorrupt(
                f"corrupt mode state at {path}: invalid planning substage "
                f"{substage!r}"
            )
    elif "substage" in state:
        raise ModeStateCorrupt(
            f"corrupt mode state at {path}: execution state cannot carry a substage"
        )

    if mode == "execution":
        try:
            from sable_provider_lib import provider_map_from_state

            provider_map_from_state(state)
        except (ImportError, ValueError) as exc:
            raise ModeStateCorrupt(
                f"corrupt mode state at {path}: {exc}"
            ) from exc
    return state


def read_mode_state(path: str | os.PathLike[str]) -> dict:
    """Read and validate state, distinguishing absence from corruption."""

    target = Path(path)
    try:
        payload = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ModeStateMissing(f"no mode state at {target}") from exc
    except OSError as exc:
        raise ModeStateCorrupt(
            f"corrupt mode state at {target}: cannot read: {exc}"
        ) from exc
    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ModeStateCorrupt(
            f"corrupt mode state at {target}: invalid JSON: {exc}"
        ) from exc
    return _validate_mode_state(raw, target)


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(path)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_unlocked(path: Path, state: Mapping[str, object]) -> None:
    """Publish validated state atomically. Caller must hold ``_exclusive_lock``."""

    data = _validate_mode_state(dict(state), path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _mutate_mode_state(
    path: str | os.PathLike[str],
    mutator: Callable[[dict], Mapping[str, object]],
) -> dict:
    """Serialize one read-modify-write transition and return committed state."""

    target = Path(path)
    with _exclusive_lock(target):
        current = read_mode_state(target)
        requested = mutator(copy.deepcopy(current))
        committed = _validate_mode_state(dict(requested), target)
        _atomic_write_unlocked(target, committed)
        return committed


def set_mode_state(
    path: str | os.PathLike[str],
    mode: str,
    *,
    fleet: Sequence[str] = (),
    tier: str = "full",
    providers: Mapping[str, str] | None = None,
    since: str | None = None,
    handoff: Mapping[str, object] | None = None,
    handoff_validator: Callable[[dict], Mapping[str, object]] | None = None,
) -> dict:
    """Set a mode while checking execution authority and providers under lock."""

    if mode not in MODES:
        raise ModeTransitionRefused(
            f"invalid mode {mode!r}; expected planning or execution"
        )
    if tier not in PLANNING_TIERS:
        raise ModeTransitionRefused(
            f"invalid tier {tier!r}; expected quick, full, or discovery"
        )
    if mode == "planning" and providers is not None:
        raise ModeTransitionRefused("provider maps are valid only in execution mode")
    if mode == "planning" and (handoff is not None or handoff_validator is not None):
        raise ModeTransitionRefused("handoff proof is valid only in execution mode")

    target = Path(path)
    with _exclusive_lock(target):
        try:
            current = read_mode_state(target)
        except ModeStateMissing:
            current = None

        if mode == "execution":
            from sable_handoff_lib import (
                HandoffRefused,
                validate_execution_authority,
            )
            from sable_provider_lib import (
                default_provider_map,
                provider_map_from_state,
            )

            requested_handoff: Mapping[str, object] | None = handoff
            if handoff_validator is not None:
                if current is None:
                    raise ModeTransitionRefused(
                        "execution transition requires planning state"
                    )
                try:
                    requested_handoff = handoff_validator(copy.deepcopy(current))
                except HandoffRefused as exc:
                    raise ModeTransitionRefused(str(exc)) from exc
            elif requested_handoff is None and (
                current is not None and current.get("mode") == "execution"
            ):
                requested_handoff = current.get("handoff")  # idempotent reapply
            try:
                validate_execution_authority(
                    {"mode": "execution", "handoff": requested_handoff}
                )
            except HandoffRefused as exc:
                raise ModeTransitionRefused(
                    f"execution transition requires handoff proof: {exc}"
                ) from exc

            if (
                isinstance(requested_handoff, Mapping)
                and requested_handoff.get("kind") == "approved"
                and (
                    current is None
                    or current.get("mode") != "planning"
                    or current.get("handoff") != requested_handoff
                )
            ):
                raise ModeTransitionRefused(
                    "approved handoff must be carried from the active planning state"
                )

            requested_providers = dict(providers or default_provider_map())
            # Validate the requested map before comparing it.
            requested_providers = provider_map_from_state(
                {"providers": requested_providers}
            )
            if (
                current is not None
                and current.get("mode") == "execution"
                and provider_map_from_state(current) != requested_providers
            ):
                raise ModeTransitionRefused(
                    "execution provider map is immutable; stand down or clear "
                    "the current mode before changing providers"
                )
        else:
            requested_providers = {}
            requested_handoff = None

        state: dict[str, object] = {
            "mode": mode,
            "since": since
            or datetime.now(timezone.utc).astimezone().strftime(
                "%Y-%m-%dT%H:%M:%S%z"
            ),
            "fleet": [member for member in fleet if member],
        }
        if mode == "planning":
            state["substage"] = "framing"
            state["tier"] = tier
        else:
            state["providers"] = requested_providers
            state["handoff"] = dict(requested_handoff or {})
        _atomic_write_unlocked(target, state)
        return state


def set_planning_handoff(
    path: str | os.PathLike[str],
    handoff: Mapping[str, object],
    *,
    expected_state: Mapping[str, object],
) -> dict:
    """Attach approval only if the planning snapshot is still byte-for-byte current."""

    expected = dict(expected_state)

    def update(state: dict) -> dict:
        if state.get("mode") != "planning":
            raise ModeTransitionRefused("handoff receipt requires planning mode")
        if state != expected:
            raise ModeTransitionRefused(
                "planning state changed before receipt publication"
            )
        state["handoff"] = dict(handoff)
        return state

    return _mutate_mode_state(path, update)


def set_planning_substage(
    path: str | os.PathLike[str], substage: str
) -> dict:
    if substage not in PLANNING_SUBSTAGES:
        raise ModeTransitionRefused(
            f"invalid substage {substage!r}; expected one of: "
            + " ".join(PLANNING_SUBSTAGES)
        )

    def update(state: dict) -> dict:
        if state.get("mode") != "planning":
            raise ModeTransitionRefused("planning substage requires planning mode")
        state.pop("handoff", None)
        state["substage"] = substage
        return state

    return _mutate_mode_state(path, update)


def advance_planning_substage(path: str | os.PathLike[str]) -> dict:
    def advance(state: dict) -> dict:
        if state.get("mode") != "planning":
            raise ModeTransitionRefused("planning substage requires planning mode")
        state.pop("handoff", None)
        current = state.get("substage")
        if current not in PLANNING_SUBSTAGES:
            raise ModeTransitionRefused("planning state has no valid substage")
        index = PLANNING_SUBSTAGES.index(current)
        if index + 1 >= len(PLANNING_SUBSTAGES):
            raise ModeTransitionRefused("planning is already at decomposition")
        state["substage"] = PLANNING_SUBSTAGES[index + 1]
        return state

    return _mutate_mode_state(path, advance)


def clear_mode_state(path: str | os.PathLike[str]) -> None:
    """Remove state under the transition lock, even when its bytes are corrupt."""

    target = Path(path)
    with _exclusive_lock(target):
        try:
            target.unlink()
        except FileNotFoundError as exc:
            raise ModeStateMissing(f"no mode state at {target}") from exc
        _fsync_directory(target.parent)
