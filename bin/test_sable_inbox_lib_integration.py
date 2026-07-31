#!/usr/bin/env python3
"""Real-filesystem integration tests for the SABLE payload inbox."""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import sable_inbox_lib as inbox  # noqa: E402


@pytest.fixture
def scratch_store(tmp_path, monkeypatch):
    root = tmp_path / "sable-456" / "inbox"
    monkeypatch.setattr(inbox, "_inbox_root", lambda: root)
    return root


def test_concurrent_enqueues_both_survive_and_create_recipient(scratch_store):
    recipient_dir = scratch_store / "optimus"
    assert not recipient_dir.exists()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = set(
            pool.map(
                lambda body: inbox.enqueue("optimus", "lincoln", body),
                ["one", "two"],
            )
        )

    assert recipient_dir.is_dir()
    assert len(ids) == 2
    assert {message.id for message in inbox.pending("optimus")} == ids
    assert {message.body for message in inbox.read("optimus")} == {"one", "two"}


def test_unreadable_store_is_loud_and_never_empty(scratch_store):
    inbox.enqueue("tarzan", "chuck", "still pending")
    recipient_dir = scratch_store / "tarzan"

    recipient_dir.chmod(0o000)
    try:
        with pytest.raises(inbox.InboxUnavailableError, match="unreadable"):
            inbox.pending("tarzan")
    finally:
        recipient_dir.chmod(0o700)

    # Both polarities prove the plant exercised the assessment boundary: the
    # same store is readable again and its message was never collapsed/lost.
    assert [message.body for message in inbox.read("tarzan")] == ["still pending"]
