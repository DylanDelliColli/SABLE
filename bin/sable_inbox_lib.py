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

import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


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


def _inbox_root() -> Path:
    """Return the locked host-global, uid-scoped inbox root."""

    return Path("/tmp") / f"sable-{os.getuid()}" / "inbox"


def _safe_segment(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{field} must be one safe path segment")
    if value in {".", ".."}:
        raise ValueError(f"{field} must not be '.' or '..'")
    return value


def _recipient_dir(recipient: str) -> Path:
    return _inbox_root() / _safe_segment(recipient, "recipient")


def _message_path(recipient: str, msg_id: str) -> Path:
    return _recipient_dir(recipient) / f"{_safe_segment(msg_id, 'message id')}.json"


def _make_store(recipient_dir: Path) -> None:
    try:
        recipient_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(_inbox_root().parent, 0o700)
        os.chmod(_inbox_root(), 0o700)
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
        if not p.name.startswith(".") and p.suffix != ".json"
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
        or set(value) != {"id", "sender", "body"}
        or not isinstance(value["id"], str)
        or not isinstance(value["sender"], str)
        or not isinstance(value["body"], str)
        or value["id"] != path.stem
    ):
        raise InboxCorruptError(f"invalid inbox message schema in {path.name}")
    return value


def enqueue(recipient: str, sender: str, body: str) -> str:
    """Atomically enqueue one body and return its unique message id."""

    recipient_dir = _recipient_dir(recipient)
    if not isinstance(sender, str) or not sender:
        raise ValueError("sender must be a non-empty string")
    if not isinstance(body, str):
        raise TypeError("body must be a string")
    _make_store(recipient_dir)

    msg_id = uuid.uuid4().hex
    final_path = recipient_dir / f"{msg_id}.json"
    temp_path = recipient_dir / f".{msg_id}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(
        {"id": msg_id, "sender": sender, "body": body},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, final_path)
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


def read(recipient: str) -> list[str]:
    """Return pending bodies without acknowledging them."""

    return [_decode(path)["body"] for path in _published_paths(recipient)]


def ack(recipient: str, msg_id: str) -> None:
    """Remove one pending message; unknown ids fail loudly."""

    path = _message_path(recipient, msg_id)
    _require_readable_dir(path.parent)
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise UnknownMessageError(
            f"message {msg_id} is not pending for {recipient}"
        ) from exc
    except OSError as exc:
        raise InboxUnavailableError(
            f"could not acknowledge message {msg_id}: {exc}"
        ) from exc
