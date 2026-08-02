#!/usr/bin/env python3
"""Host-global, per-user payload queue for SABLE messages (SABLE-m4kyf.3).

The tmux wake path must stay tiny and content-free.  This module owns the
other half of that split: durable message bodies under
``/tmp/sable-<uid>/inbox/<recipient>/``.  One atomically-published JSON file is
one message, so concurrent senders never contend for a shared queue file.

An absent or unreadable store is not an empty inbox.  ``pending`` and ``read``
raise ``InboxUnavailableError`` in that state; only an existing, readable
recipient directory with no message files returns an empty list.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_MESSAGE_FILE = re.compile(
    r"(?P<sequence>[0-9]{20})-(?P<id>[A-Za-z0-9][A-Za-z0-9_.-]*)\.json\Z"
)
_DRAIN_HEARTBEAT = ".drain-heartbeat.json"
_MAX_DRAIN_INTERVAL_SECONDS = 3600.0


class InboxError(RuntimeError):
    """Base class for queue failures that callers must report loudly."""


class InboxUnavailableError(InboxError):
    """The store could not be assessed; this never means 'no messages'."""


class InboxCorruptError(InboxError):
    """A published message could not be decoded or failed validation."""


class UnknownMessageError(InboxError):
    """The requested message id is not pending for this recipient."""


@dataclass(frozen=True)
class PendingMessage:
    """Content-free information returned while surveying an inbox."""

    id: str
    sender: str


@dataclass(frozen=True)
class InboxMessage:
    """One message read with its acknowledgement identity attached."""

    id: str
    sender: str
    body: str


@dataclass(frozen=True)
class DrainHealth:
    """Whether a host-side drainer has completed a recent assessable sweep."""

    fresh: bool
    reason: str
    age_seconds: float | None = None


def _inbox_root() -> Path:
    """Return the locked host-global, uid-scoped inbox root."""

    # Shell integration needs an isolated real-process store.  The override is
    # deliberately unreachable unless the explicit test marker is also set;
    # production callers cannot accidentally split the host-global queue by
    # inheriting a generic path variable.
    if os.environ.get("SABLE_TEST") == "1" and os.environ.get(
        "SABLE_TEST_INBOX_ROOT"
    ):
        return Path(os.environ["SABLE_TEST_INBOX_ROOT"])
    return Path("/tmp") / f"sable-{os.getuid()}" / "inbox"


def _safe_segment(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{field} must be one safe path segment")
    if value in {".", ".."}:
        raise ValueError(f"{field} must not be '.' or '..'")
    return value


def _recipient_dir(recipient: str) -> Path:
    return _inbox_root() / _safe_segment(recipient, "recipient")


def _make_root() -> Path:
    root = _inbox_root()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root.parent, 0o700)
        os.chmod(root, 0o700)
    except OSError as exc:
        raise InboxUnavailableError(
            f"could not create inbox store at {root}: {exc}"
        ) from exc
    return root


def _make_store(recipient_dir: Path) -> None:
    try:
        _make_root()
        recipient_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(recipient_dir, 0o700)
    except OSError as exc:
        raise InboxUnavailableError(
            f"could not create inbox store at {recipient_dir}: {exc}"
        ) from exc


def _require_readable_dir(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        if not stat.S_ISDIR(mode):
            raise InboxUnavailableError(f"inbox path is not a directory: {path}")
        # os.access alone reports true for uid 0.  Inspecting the permission
        # bits keeps the unreadable-store state observable in root-run CI too.
        if (
            mode & 0o444 == 0
            or mode & 0o111 == 0
            or not os.access(path, os.R_OK | os.X_OK)
        ):
            raise InboxUnavailableError(f"inbox store is unreadable: {path}")
    except InboxUnavailableError:
        raise
    except OSError as exc:
        raise InboxUnavailableError(
            f"could not assess inbox store at {path}: {exc}"
        ) from exc


def _published_paths(recipient: str) -> list[Path]:
    recipient_dir = _recipient_dir(recipient)
    _require_readable_dir(recipient_dir)
    try:
        entries = list(recipient_dir.iterdir())
    except OSError as exc:
        raise InboxUnavailableError(
            f"could not enumerate inbox store at {recipient_dir}: {exc}"
        ) from exc

    unexpected = [
        p.name
        for p in entries
        if not p.name.startswith(".") and not _MESSAGE_FILE.fullmatch(p.name)
    ]
    if unexpected:
        raise InboxCorruptError(
            f"unexpected entries in inbox for {recipient}: {', '.join(sorted(unexpected))}"
        )
    return sorted((p for p in entries if p.suffix == ".json"), key=lambda p: p.name)


def _decode(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InboxCorruptError(
            f"could not decode inbox message {path.name}: {exc}"
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"sequence", "id", "sender", "body"}
        or not isinstance(value["sequence"], int)
        or not isinstance(value["id"], str)
        or not isinstance(value["sender"], str)
        or not isinstance(value["body"], str)
        or (match := _MESSAGE_FILE.fullmatch(path.name)) is None
        or value["id"] != match.group("id")
        or value.get("sequence") != int(match.group("sequence"))
    ):
        raise InboxCorruptError(f"invalid inbox message schema in {path.name}")
    return value


def _next_sequence(recipient_dir: Path) -> int:
    """Allocate a FIFO position while holding the recipient's enqueue lock."""

    highest = 0
    for path in recipient_dir.iterdir():
        match = _MESSAGE_FILE.fullmatch(path.name)
        if match:
            highest = max(highest, int(match.group("sequence")))
    return highest + 1


def enqueue(recipient: str, sender: str, body: str) -> str:
    """Atomically enqueue one body and return its unique message id."""

    recipient_dir = _recipient_dir(recipient)
    if not isinstance(sender, str) or not sender:
        raise ValueError("sender must be a non-empty string")
    if not isinstance(body, str):
        raise TypeError("body must be a string")
    _make_store(recipient_dir)

    msg_id = uuid.uuid4().hex
    temp_path = recipient_dir / f".{msg_id}.{uuid.uuid4().hex}.tmp"
    try:
        lock_fd = os.open(
            recipient_dir / ".enqueue.lock", os.O_RDWR | os.O_CREAT, 0o600
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            sequence = _next_sequence(recipient_dir)
            final_path = recipient_dir / f"{sequence:020d}-{msg_id}.json"
            payload = json.dumps(
                {"sequence": sequence, "id": msg_id, "sender": sender, "body": body},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, final_path)
        finally:
            os.close(lock_fd)
        dir_fd = os.open(recipient_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise InboxUnavailableError(
            f"could not enqueue message for {recipient}: {exc}"
        ) from exc
    return msg_id


def pending(recipient: str) -> list[PendingMessage]:
    """Return pending message metadata, or raise if the store is unassessable."""

    return [
        PendingMessage(id=value["id"], sender=value["sender"])
        for value in (_decode(path) for path in _published_paths(recipient))
    ]


def pending_recipients() -> list[str]:
    """Return recipients with queued payloads, never treating absence as empty.

    The heartbeat and its atomic staging files are the only root-level control
    state. Every other entry must be a real, safe recipient directory; hidden
    junk and symlinks are corruption, not recipients to silently skip or paths
    to follow outside the uid-scoped store.
    """

    root = _inbox_root()
    _require_readable_dir(root)
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise InboxUnavailableError(
            f"could not enumerate inbox recipients at {root}: {exc}"
        ) from exc
    def is_control(path: Path) -> bool:
        return path.name == _DRAIN_HEARTBEAT or (
            path.name.startswith(".drain-heartbeat.") and path.name.endswith(".tmp")
        )

    unexpected = [
        path.name
        for path in entries
        if not is_control(path)
        and (
            path.name.startswith(".")
            or path.is_symlink()
            or not path.is_dir()
            or not _SAFE_SEGMENT.fullmatch(path.name)
        )
    ]
    if unexpected:
        raise InboxCorruptError(
            "unexpected entries in inbox root: " + ", ".join(sorted(unexpected))
        )
    recipients = sorted(
        path.name
        for path in entries
        if not is_control(path) and path.is_dir() and not path.is_symlink()
    )
    return [recipient for recipient in recipients if pending(recipient)]


def _valid_interval(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0 < float(value) <= _MAX_DRAIN_INTERVAL_SECONDS
    )


def record_drain_heartbeat(
    *,
    observed_at: float | None = None,
    interval_seconds: float,
) -> Path:
    """Atomically publish one completed host-global drain sweep.

    The recorded cadence is part of the evidence: readers consider the sweep
    fresh for exactly two configured intervals.  This keeps a custom timer
    cadence and sender-side fallback gate consistent without a repo-local flag
    flip or per-worktree state.
    """

    if not _valid_interval(interval_seconds):
        raise ValueError(
            "interval_seconds must be a finite positive number no greater than "
            f"{_MAX_DRAIN_INTERVAL_SECONDS:g}"
        )
    when = time.time() if observed_at is None else observed_at
    if (
        not isinstance(when, (int, float))
        or isinstance(when, bool)
        or not math.isfinite(float(when))
        or float(when) < 0
    ):
        raise ValueError("observed_at must be a finite non-negative timestamp")

    root = _make_root()
    final_path = root / _DRAIN_HEARTBEAT
    temp_path = root / f".drain-heartbeat.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(
        {
            "schema": 1,
            "observed_at": float(when),
            "interval_seconds": float(interval_seconds),
        },
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, final_path)
        dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise InboxUnavailableError(
            f"could not publish drain heartbeat at {final_path}: {exc}"
        ) from exc
    return final_path


def drain_health(*, now: float | None = None) -> DrainHealth:
    """Read the host-global drain heartbeat, failing closed on every anomaly."""

    path = _inbox_root() / _DRAIN_HEARTBEAT
    current = time.time() if now is None else now
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DrainHealth(False, f"drain heartbeat absent at {path}")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return DrainHealth(False, f"drain heartbeat invalid at {path}: {exc}")

    if not isinstance(value, dict) or set(value) != {
        "schema", "observed_at", "interval_seconds"
    }:
        return DrainHealth(False, "drain heartbeat invalid: wrong schema")
    observed = value.get("observed_at")
    interval = value.get("interval_seconds")
    if value.get("schema") != 1 or not _valid_interval(interval) or (
        not isinstance(observed, (int, float))
        or isinstance(observed, bool)
        or not math.isfinite(float(observed))
        or float(observed) < 0
    ):
        return DrainHealth(False, "drain heartbeat invalid: malformed values")
    if (
        not isinstance(current, (int, float))
        or isinstance(current, bool)
        or not math.isfinite(float(current))
    ):
        return DrainHealth(False, "drain heartbeat invalid: current clock is non-finite")

    age = float(current) - float(observed)
    if age < 0:
        return DrainHealth(False, "drain heartbeat invalid: timestamp is in the future", age)
    ttl = 2.0 * float(interval)
    if age > ttl:
        return DrainHealth(
            False,
            f"drain heartbeat stale by {age - ttl:.1f}s (age {age:.1f}s, limit {ttl:.1f}s)",
            age,
        )
    return DrainHealth(
        True,
        f"drain heartbeat fresh (age {age:.1f}s, limit {ttl:.1f}s)",
        age,
    )


def read(recipient: str) -> list[InboxMessage]:
    """Return a FIFO snapshot with each body bound to its id and sender."""

    return [
        InboxMessage(id=value["id"], sender=value["sender"], body=value["body"])
        for value in (_decode(path) for path in _published_paths(recipient))
    ]


def ack(recipient: str, msg_id: str) -> None:
    """Remove one pending message; unknown ids fail loudly."""

    _safe_segment(msg_id, "message id")
    recipient_dir = _recipient_dir(recipient)
    paths = _published_paths(recipient)
    matches = [
        path
        for path in paths
        if _MESSAGE_FILE.fullmatch(path.name).group("id") == msg_id
    ]
    if not matches:
        raise UnknownMessageError(f"message {msg_id} is not pending for {recipient}")
    if len(matches) != 1:
        raise InboxCorruptError(f"duplicate message id {msg_id} for {recipient}")
    try:
        matches[0].unlink()
    except FileNotFoundError as exc:
        raise UnknownMessageError(
            f"message {msg_id} is not pending for {recipient}"
        ) from exc
    except OSError as exc:
        raise InboxUnavailableError(
            f"could not acknowledge message {msg_id}: {exc}"
        ) from exc
