#!/usr/bin/env python3
"""Unit contract for the file-backed SABLE payload inbox (SABLE-m4kyf.3)."""
import json
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


def bound_payloads(messages):
    return [(message.id, message.sender, message.body) for message in messages]


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
    assert bound_payloads(messages) == [
        (first_id, "lincoln", "hold"),
        (second_id, "chuck", "release"),
    ]

    inbox.ack("optimus", messages[0].id)
    assert bound_payloads(inbox.read("optimus")) == [
        (second_id, "chuck", "release")
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

    assert bound_payloads(inbox.read("victor")) == [
        (first_id, "lincoln", "first"),
        (second_id, "lincoln", "second"),
    ]


def test_read_surfaces_atomic_artifact_mtime_as_enqueue_ordering_evidence(store):
    msg_id = inbox.enqueue("optimus", "lincoln", "older queued instruction")
    [path] = inbox._published_paths("optimus")
    os.utime(path, (100.25, 100.25))

    [message] = inbox.read("optimus")

    assert message.id == msg_id
    assert message.enqueued_at == pytest.approx(100.25)


def test_pending_has_three_distinct_observable_states(store):
    with pytest.raises(inbox.InboxUnavailableError, match="could not assess"):
        inbox.pending("victor")

    msg_id = inbox.enqueue("victor", "chuck", "work")
    assert inbox.pending("victor") == [inbox.PendingMessage(msg_id, "chuck")]

    inbox.ack("victor", msg_id)
    assert inbox.pending("victor") == []


def test_drain_heartbeat_is_host_global_atomic_and_fresh_for_two_cadences(store):
    heartbeat = inbox.record_drain_heartbeat(
        observed_at=100.0,
        interval_seconds=30.0,
    )

    assert heartbeat == store / ".drain-heartbeat.json"
    assert json.loads(heartbeat.read_text()) == {
        "schema": 1,
        "observed_at": 100.0,
        "interval_seconds": 30.0,
    }
    assert list(store.glob(".drain-heartbeat.*.tmp")) == []

    fresh = inbox.drain_health(now=159.9)
    stale = inbox.drain_health(now=160.1)
    assert fresh.fresh is True
    assert fresh.age_seconds == pytest.approx(59.9)
    assert stale.fresh is False
    assert stale.age_seconds == pytest.approx(60.1)
    assert "stale" in stale.reason


def test_drain_health_fails_closed_for_absent_corrupt_future_and_bad_ttl(store):
    path = store / ".drain-heartbeat.json"

    absent = inbox.drain_health(now=100.0)
    assert absent.fresh is False and "absent" in absent.reason

    store.mkdir(parents=True)
    path.write_text("not json")
    corrupt = inbox.drain_health(now=100.0)
    assert corrupt.fresh is False and "invalid" in corrupt.reason

    path.write_text(json.dumps({
        "schema": 1,
        "observed_at": 101.0,
        "interval_seconds": 30.0,
    }))
    future = inbox.drain_health(now=100.0)
    assert future.fresh is False and "future" in future.reason

    path.write_text(json.dumps({
        "schema": 1,
        "observed_at": 90.0,
        "interval_seconds": True,
    }))
    invalid_ttl = inbox.drain_health(now=100.0)
    assert invalid_ttl.fresh is False and "invalid" in invalid_ttl.reason


def test_inbox_root_override_is_test_only(monkeypatch, tmp_path):
    alternate = tmp_path / "isolated-inbox"
    monkeypatch.setenv("SABLE_TEST_INBOX_ROOT", str(alternate))
    monkeypatch.delenv("SABLE_TEST", raising=False)
    assert inbox._inbox_root() != alternate

    monkeypatch.setenv("SABLE_TEST", "1")
    assert inbox._inbox_root() == alternate


def test_pending_recipients_ignores_only_control_state_and_rejects_junk(store):
    inbox.record_drain_heartbeat(observed_at=100.0, interval_seconds=30.0)
    inbox.enqueue("optimus", "lincoln", "one")
    empty_id = inbox.enqueue("tarzan", "lincoln", "two")
    inbox.ack("tarzan", empty_id)

    assert inbox.pending_recipients() == ["optimus"]

    (store / "stray.txt").write_text("not a recipient")
    with pytest.raises(inbox.InboxCorruptError, match="stray.txt"):
        inbox.pending_recipients()

    (store / "stray.txt").unlink()
    (store / ".hidden-junk").write_text("must not disappear from the census")
    with pytest.raises(inbox.InboxCorruptError, match="hidden-junk"):
        inbox.pending_recipients()


def test_pending_recipients_never_follows_a_recipient_symlink(store, tmp_path):
    store.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store / "optimus").symlink_to(outside, target_is_directory=True)

    with pytest.raises(inbox.InboxCorruptError, match="optimus"):
        inbox.pending_recipients()
