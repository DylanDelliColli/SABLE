#!/usr/bin/env python3
"""Unit contract for the count-only SABLE inbox wake (SABLE-m4kyf.4)."""
import importlib.util
import inspect
import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


_LOADER = SourceFileLoader(
    "sable_inbox_watcher",
    str(Path(__file__).resolve().parent / "sable-inbox-watcher"),
)
_SPEC = importlib.util.spec_from_loader("sable_inbox_watcher", _LOADER)
watcher = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = watcher
_LOADER.exec_module(watcher)


def completed(command, *, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def process_report(pane, state, *, others=()):
    record = {
        "pane": pane,
        "bead": "SABLE-test",
        "root_pid": 123,
        "state": state,
        "reason": "fixture",
        "agents": [],
        "visited_pids": [123],
    }
    return json.dumps(
        {
            "session": "scratch",
            "scope_error": None,
            "panes": [record, *others],
        }
    )


def registry(*rows):
    return "\n".join("\t".join(row) for row in rows) + "\n"


def registry_command(*, socket=None, session="scratch"):
    base = ["tmux", "-L", socket] if socket else ["tmux"]
    return base + [
        "list-panes",
        "-s",
        "-t",
        f"={session}:",
        "-F",
        "#{pane_id}\t#{@sable_role}\t#{@sable_bead}",
    ]


def test_poke_text_is_short_fixed_single_line_with_only_count_varying():
    actual = [watcher.poke_text(count) for count in (0, 1, 7)]

    assert actual == [
        "Check SABLE inbox: 0 pending.",
        "Check SABLE inbox: 1 pending.",
        "Check SABLE inbox: 7 pending.",
    ]
    assert all("\n" not in text and len(text) < 64 for text in actual)
    assert len(inspect.signature(watcher.poke_text).parameters) == 1


def test_poke_text_structurally_refuses_a_message_body():
    body = "hold\nrelease; payload must stay in enqueue"

    with pytest.raises(TypeError):
        watcher.poke_text(1, body)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        watcher.poke_text(count=1, body=body)  # type: ignore[call-arg]


def test_target_process_state_uses_the_target_per_pane_record():
    calls = []

    def run(command):
        calls.append(command)
        if "list-panes" in command:
            return completed(command, stdout=registry(("%8", "optimus", "")))
        return completed(
            command,
            stdout=process_report("%8", watcher.PROCESS_LIVE),
            returncode=0,
        )

    assert watcher.target_process_state(
        "%8", run=run, status_command="status-probe"
    ) == watcher.PROCESS_LIVE
    assert calls == [["status-probe", "--quiescence-pane", "%8", "--json"]]


@pytest.mark.parametrize(
    ("state", "returncode", "error"),
    [
        (watcher.PROCESS_EXITED, 0, watcher.WakeRefused),
        (watcher.PROCESS_CANNOT_ASSESS, 2, watcher.WakeCannotAssess),
    ],
)
def test_non_live_target_refuses_loudly_without_capture_or_injection(
    state, returncode, error
):
    calls = []

    def run(command):
        calls.append(command)
        if "list-panes" in command:
            return completed(command, stdout=registry(("%8", "optimus", "")))
        return completed(
            command, stdout=process_report("%8", state), returncode=returncode
        )

    with pytest.raises(error, match=state):
        watcher.wake_once(
            "optimus",
            "%8",
            "claude",
            run=run,
            pending_fn=lambda _recipient: [object()],
            status_command="status-probe",
            session="scratch",
        )
    assert calls == [
        registry_command(),
        ["status-probe", "--quiescence-pane", "%8", "--json"],
    ]


def test_busy_fresh_capture_refuses_loudly_and_never_injects():
    calls = []

    def run(command):
        calls.append(command)
        if "list-panes" in command:
            return completed(command, stdout=registry(("%8", "optimus", "")))
        if command[0] == "status-probe":
            return completed(
                command,
                stdout=process_report("%8", watcher.PROCESS_LIVE),
                returncode=0,
            )
        if "capture-pane" in command:
            return completed(command, stdout="✻ Working… 8s\nesc to interrupt\n❯\n")
        raise AssertionError(f"busy pane must never receive send-keys: {command}")

    with pytest.raises(watcher.WakeRefused, match="fresh capture is not idle"):
        watcher.wake_once(
            "optimus",
            "%8",
            "claude",
            run=run,
            pending_fn=lambda _recipient: [object()],
            status_command="status-probe",
            session="scratch",
        )
    assert [command[1] for command in calls] == [
        "list-panes",
        "--quiescence-pane",
        "capture-pane",
    ]


def test_live_then_fresh_idle_capture_immediately_precedes_one_submitted_poke():
    calls = []

    def run(command):
        calls.append(command)
        if "list-panes" in command:
            return completed(command, stdout=registry(("%8", "optimus", "")))
        if command[0] == "status-probe":
            return completed(
                command,
                stdout=process_report("%8", watcher.PROCESS_LIVE),
                returncode=0,
            )
        if "capture-pane" in command:
            return completed(command, stdout="completed turn\n❯\n")
        return completed(command)

    result = watcher.wake_once(
        "optimus",
        "%8",
        "claude",
        run=run,
        pending_fn=lambda _recipient: [object()] * 7,
        status_command="status-probe",
        socket="scratch",
        session="scratch",
    )

    assert result == watcher.WakeResult(7, watcher.poke_text(7))
    assert calls == [
        registry_command(socket="scratch"),
        ["status-probe", "--quiescence-pane", "%8", "--json"],
        ["tmux", "-L", "scratch", "capture-pane", "-p", "-J", "-e", "-t", "%8"],
        ["tmux", "-L", "scratch", "send-keys", "-t", "%8", "-l", watcher.poke_text(7)],
        ["tmux", "-L", "scratch", "send-keys", "-t", "%8", "Enter"],
    ]


def test_empty_queue_never_assesses_or_pokes():
    def forbidden(_command):
        raise AssertionError("empty inbox must not touch pane")

    assert watcher.wake_once(
        "optimus",
        "%8",
        "claude",
        run=forbidden,
        pending_fn=lambda _recipient: [],
        session="scratch",
    ) == watcher.WakeResult(0, None)


@pytest.mark.parametrize(
    ("recipient", "rows"),
    [
        ("optimus", (("%8", "optimus", ""),)),
        ("SABLE-work", (("%8", "worker", "SABLE-work"),)),
    ],
)
def test_registered_manager_or_worker_binding_matches(recipient, rows):
    def run(command):
        return completed(command, stdout=registry(*rows))

    watcher.bound_recipient_pane(
        recipient, "%8", "scratch", run=run, socket="sock"
    )


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ((("%8", "tarzan", ""),), "no registered pane is bound"),
        ((("%9", "optimus", ""),), "pane is not registered"),
        (
            (("%8", "optimus", ""), ("%9", "optimus", "")),
            "ambiguous pane registrations",
        ),
        ((("%8", "tarzan", ""), ("%9", "optimus", "")), "is bound to %9"),
    ],
)
def test_missing_mismatched_or_ambiguous_binding_refuses_before_probe(rows, message):
    calls = []

    def run(command):
        calls.append(command)
        if "list-panes" in command:
            return completed(command, stdout=registry(*rows))
        raise AssertionError(f"binding refusal must precede process probe: {command}")

    with pytest.raises(watcher.WakeRefused, match=message):
        watcher.wake_once(
            "optimus",
            "%8",
            "claude",
            run=run,
            pending_fn=lambda _recipient: [object()],
            status_command="must-not-run",
            session="scratch",
        )
    assert calls == [registry_command()]


def test_missing_or_duplicated_target_is_cannot_assess():
    for panes in ([], [{"pane": "%8", "state": "LIVE"}] * 2):
        def run(command, panes=panes):
            return completed(command, stdout=json.dumps({"panes": panes}))

        with pytest.raises(watcher.WakeCannotAssess, match="records for pane"):
            watcher.target_process_state("%8", run=run, status_command="probe")


def test_malformed_pane_record_is_cannot_assess():
    def run(command):
        return completed(command, stdout=json.dumps({"panes": ["not-an-object"]}))

    with pytest.raises(watcher.WakeCannotAssess, match="non-object record"):
        watcher.target_process_state("%8", run=run, status_command="probe")


def test_process_state_and_exit_code_disagreement_is_cannot_assess():
    def run(command):
        return completed(
            command,
            stdout=process_report("%8", watcher.PROCESS_LIVE),
            returncode=2,
        )

    with pytest.raises(watcher.WakeCannotAssess, match="conflicts with exit 2"):
        watcher.target_process_state("%8", run=run, status_command="probe")


def test_main_requires_explicit_host_side_session_without_pane_or_pwd_fallback(
    monkeypatch, capsys
):
    monkeypatch.delenv("SABLE_TMUX_SESSION", raising=False)
    monkeypatch.setenv("TMUX_PANE", "%caller-must-not-count")

    assert watcher.main(["optimus", "%8", "claude"]) == 2
    assert "no explicit tmux session" in capsys.readouterr().err
