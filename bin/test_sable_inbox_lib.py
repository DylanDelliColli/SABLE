#!/usr/bin/env python3
"""Unit contract for the file-backed SABLE payload inbox (SABLE-m4kyf.3)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import sable_inbox_lib as inbox  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "sable-123" / "inbox"
    monkeypatch.setattr(inbox, "_inbox_root", lambda: root)
    return root


def test_enqueue_pending_ack_and_unknown_ack_is_loud(store):
    msg_id = inbox.enqueue("optimus", "lincoln", "payload")

    assert inbox.pending("optimus") == [inbox.PendingMessage(msg_id, "lincoln")]
    inbox.ack("optimus", msg_id)
    assert inbox.pending("optimus") == []
    with pytest.raises(inbox.UnknownMessageError, match=msg_id):
        inbox.ack("optimus", msg_id)


def test_body_round_trips_byte_exact_for_tmux_corruption_shapes(store):
    body = "first line\nsecond line\ntrailing semicolon;\nmultibyte: café 雪 🚦;"
    expected = body.encode("utf-8")

    inbox.enqueue("tarzan", "chuck", body)

    [actual] = inbox.read("tarzan")
    assert actual.body.encode("utf-8") == expected


def test_read_keeps_identity_attached_when_lower_id_arrives_after_pending(
    store, monkeypatch
):
    ids = iter(["f" * 32, "temp-one", "0" * 32, "temp-two"])
    monkeypatch.setattr(
        inbox.uuid, "uuid4", lambda: type("UUID", (), {"hex": next(ids)})()
    )

    first_id = inbox.enqueue("optimus", "lincoln", "hold")
    survey = inbox.pending("optimus")
    second_id = inbox.enqueue("optimus", "chuck", "release")

    messages = inbox.read("optimus")
    assert survey == [inbox.PendingMessage(first_id, "lincoln")]
    assert messages == [
        inbox.InboxMessage(first_id, "lincoln", "hold"),
        inbox.InboxMessage(second_id, "chuck", "release"),
    ]

    inbox.ack("optimus", messages[0].id)
    assert inbox.read("optimus") == [
        inbox.InboxMessage(second_id, "chuck", "release")
    ]


def test_sequential_enqueues_are_read_in_fifo_order_even_when_ids_sort_oppositely(
    store, monkeypatch
):
    ids = iter(["f" * 32, "temp-one", "0" * 32, "temp-two"])
    monkeypatch.setattr(
        inbox.uuid, "uuid4", lambda: type("UUID", (), {"hex": next(ids)})()
    )

    first_id = inbox.enqueue("victor", "lincoln", "first")
    second_id = inbox.enqueue("victor", "lincoln", "second")

    assert inbox.read("victor") == [
        inbox.InboxMessage(first_id, "lincoln", "first"),
        inbox.InboxMessage(second_id, "lincoln", "second"),
    ]


def test_pending_has_three_distinct_observable_states(store):
    with pytest.raises(inbox.InboxUnavailableError, match="could not assess"):
        inbox.pending("victor")

    msg_id = inbox.enqueue("victor", "chuck", "work")
    assert inbox.pending("victor") == [inbox.PendingMessage(msg_id, "chuck")]

    inbox.ack("victor", msg_id)
    assert inbox.pending("victor") == []
