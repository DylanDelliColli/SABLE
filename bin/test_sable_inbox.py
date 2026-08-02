#!/usr/bin/env python3
"""Contract tests for the file-backed inbox consumer CLI (SABLE-1el7e)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

import sable_inbox_lib as inbox


_BIN = Path(__file__).resolve().parent
_CLI = _BIN / "sable-inbox"
_LOADER = SourceFileLoader("sable_inbox_cli", str(_CLI))
_SPEC = importlib.util.spec_from_loader("sable_inbox_cli", _LOADER)
sable_inbox = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sable_inbox)


def test_recipient_identity_prefers_worker_bead_over_lane_manager(monkeypatch):
    monkeypatch.setenv("SABLE_WORKER_PANE", "1")
    monkeypatch.setenv("SABLE_BEAD", "SABLE-work")
    monkeypatch.setenv("SABLE_AGENT_NAME", "tarzan")

    assert sable_inbox.resolve_recipient() == "SABLE-work"


def test_recipient_identity_uses_provider_neutral_manager_name(monkeypatch):
    monkeypatch.delenv("SABLE_WORKER_PANE", raising=False)
    monkeypatch.setenv("SABLE_AGENT_NAME", "optimus")
    monkeypatch.setenv("CLAUDE_AGENT_NAME", "wrong-legacy-name")

    assert sable_inbox.resolve_recipient() == "optimus"


def test_read_is_non_destructive_and_json_binds_body_to_id(monkeypatch, capsys):
    message = inbox.InboxMessage(
        "msg-1", "lincoln", "hold\nthen release", enqueued_at=100.25
    )
    monkeypatch.setattr(sable_inbox, "read", lambda recipient: [message])
    monkeypatch.setattr(
        sable_inbox,
        "ack",
        lambda *_args: pytest.fail("read must never acknowledge implicitly"),
    )

    assert sable_inbox.main(["read", "--recipient", "optimus", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {
            "id": "msg-1",
            "sender": "lincoln",
            "body": "hold\nthen release",
            "enqueued_at": 100.25,
        }
    ]


def test_ack_targets_exact_observed_message(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sable_inbox,
        "ack",
        lambda recipient, msg_id: calls.append((recipient, msg_id)),
    )

    assert sable_inbox.main(
        ["ack", "msg-1", "--recipient", "optimus"]
    ) == 0
    assert calls == [("optimus", "msg-1")]


def test_missing_identity_is_loud_not_an_empty_inbox(monkeypatch, capsys):
    for name in (
        "SABLE_WORKER_PANE",
        "SABLE_BEAD",
        "SABLE_AGENT_NAME",
        "CLAUDE_AGENT_NAME",
    ):
        monkeypatch.delenv(name, raising=False)

    assert sable_inbox.main(["read", "--json"]) == 2
    assert "recipient identity" in capsys.readouterr().err.lower()


def test_real_cli_read_then_ack_round_trip_never_executes_or_argvs_body(
    tmp_path, monkeypatch,
):
    root = tmp_path / "inbox"
    executed = tmp_path / "reader-must-not-execute-body"
    body = (
        "literal `printf backtick` stays data\n"
        f"literal $(touch {executed}) stays data\n"
        "quotes: '$PATH' \"${USER}\" ; | & < >\n"
    )
    monkeypatch.setenv("SABLE_TEST", "1")
    monkeypatch.setenv("SABLE_TEST_INBOX_ROOT", str(root))
    msg_id = inbox.enqueue("optimus", "lincoln", body)
    [published] = inbox.read("optimus")
    env = dict(os.environ)

    read_argv = [str(_CLI), "read", "--recipient", "optimus", "--json"]
    assert all(body not in argument for argument in read_argv)
    observed = subprocess.run(
        read_argv,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert observed.returncode == 0, observed.stderr
    assert json.loads(observed.stdout) == [
        {
            "id": msg_id,
            "sender": "lincoln",
            "body": body,
            "enqueued_at": published.enqueued_at,
        }
    ]
    assert not executed.exists(), "reader treated inert payload bytes as a command"
    assert [message.id for message in inbox.pending("optimus")] == [msg_id]

    acknowledged = subprocess.run(
        [str(_CLI), "ack", msg_id, "--recipient", "optimus"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert acknowledged.returncode == 0, acknowledged.stderr
    assert inbox.pending("optimus") == []
