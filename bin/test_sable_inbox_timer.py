#!/usr/bin/env python3
"""Contract tests for the host-side SABLE inbox drain cadence (SABLE-albyd)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


_LOADER = SourceFileLoader(
    "sable_inbox_timer",
    str(Path(__file__).resolve().parent / "sable-inbox-timer"),
)
_SPEC = importlib.util.spec_from_loader("sable_inbox_timer", _LOADER)
timer = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(timer)


def _cp(argv, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, rc, stdout, stderr)


def _visible_status(argv, state="LIVE"):
    pane = argv[argv.index("--quiescence-pane") + 1]
    rc = 2 if state == "CANNOT-ASSESS" else 0
    return _cp(
        argv,
        rc=rc,
        stdout=json.dumps({
            "session": "sable-a",
            "scope_error": None,
            "panes": [{"pane": pane, "state": state}],
        }),
    )


def test_registry_includes_manager_cockpit_and_worker_identities():
    rows = timer.parse_registry(
        "%1\tsable-a\toptimus\t\tcodex\n"
        "%2\tsable-a\tlincoln\t\tclaude\n"
        "%3\tsable-a\tworker\tSABLE-work\tcodex\n"
        "%4\tsable-a\t\t\t\n"
    )

    assert [(row.identity, row.pane, row.provider) for row in rows] == [
        ("optimus", "%1", "codex"),
        ("lincoln", "%2", "claude"),
        ("SABLE-work", "%3", "codex"),
    ]


def test_registry_rejects_malformed_rows_and_registered_panes_without_provider():
    with pytest.raises(timer.TimerCannotAssess, match="invalid pane registry row"):
        timer.parse_registry("%1\tsable-a\toptimus\n")
    with pytest.raises(timer.TimerCannotAssess, match="provider"):
        timer.parse_registry("%1\tsable-a\toptimus\t\t\n")


def test_resolve_socket_ignores_pane_and_cwd_context():
    env = {
        "TMUX_PANE": "%wrong",
        "PWD": "/wrong/repo",
        "SABLE_INBOX_TMUX_SOCKET": "configured",
        "SABLE_TMUX_SOCKET": "pane-shaped",
    }
    assert timer.resolve_socket("explicit", env) == "explicit"
    assert timer.resolve_socket(None, env) == "configured"
    assert timer.resolve_socket(None, {"TMUX_PANE": "%wrong", "PWD": "/wrong"}) is None


def test_tick_delegates_readiness_to_watcher_and_heartbeats_safe_deferral():
    calls = []
    heartbeats = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "list-panes" in argv:
            return _cp(
                argv,
                stdout=(
                    "%1\tsable-a\toptimus\t\tcodex\n"
                    "%2\tsable-a\tlincoln\t\tclaude\n"
                    "%3\tsable-a\tworker\tSABLE-work\tcodex\n"
                ),
            )
        if "--quiescence-pane" in argv:
            return _visible_status(argv)
        if argv[0] == "/watcher":
            return _cp(argv, rc=1, stderr="REFUSED: fresh capture is not idle")
        raise AssertionError(argv)

    result = timer.sweep_once(
        socket="sock",
        interval_seconds=30.0,
        status_command="/status",
        watcher_command="/watcher",
        run=run,
        recipients_fn=lambda: ["optimus", "SABLE-work"],
        heartbeat_fn=lambda **kwargs: heartbeats.append(kwargs),
    )

    assert result.returncode == 0
    assert result.deferred == ("optimus", "SABLE-work")
    assert heartbeats == [{"interval_seconds": 30.0}]
    watcher_calls = [call for call in calls if call[0] == "/watcher"]
    assert [call[1:4] for call in watcher_calls] == [
        ["optimus", "%1", "codex"],
        ["SABLE-work", "%3", "codex"],
    ]
    assert all("--session" in call and "--status-command" in call for call in watcher_calls)
    assert not any("capture-pane" in call or "send-keys" in call for call in calls)


@pytest.mark.parametrize(
    ("registry", "reason"),
    [
        ("%1\ts\tlincoln\t\tclaude\n", "no registered pane"),
        (
            "%1\ts\toptimus\t\tcodex\n%2\ts\toptimus\t\tclaude\n",
            "ambiguous",
        ),
    ],
)
def test_binding_failure_pokes_nothing_and_does_not_heartbeat(registry, reason):
    calls = []
    heartbeats = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "list-panes" in argv:
            return _cp(argv, stdout=registry)
        if "--quiescence-pane" in argv:
            return _visible_status(argv)
        raise AssertionError("watcher must not run for a bad binding")

    result = timer.sweep_once(
        socket="sock",
        interval_seconds=60.0,
        status_command="/status",
        watcher_command="/watcher",
        run=run,
        recipients_fn=lambda: ["optimus"],
        heartbeat_fn=lambda **kwargs: heartbeats.append(kwargs),
    )

    assert result.returncode == 2
    assert reason in " ".join(result.errors)
    assert heartbeats == []
    assert not any(call[0] == "/watcher" for call in calls)


def test_cannot_assess_pid_visibility_is_loud_before_any_watcher_or_heartbeat():
    calls = []
    heartbeats = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "list-panes" in argv:
            return _cp(argv, stdout="%1\ts\toptimus\t\tcodex\n")
        if "--quiescence-pane" in argv:
            return _visible_status(argv, "CANNOT-ASSESS")
        raise AssertionError("watcher must not run without host PID visibility")

    result = timer.sweep_once(
        socket="sock",
        interval_seconds=60.0,
        status_command="/status",
        watcher_command="/watcher",
        run=run,
        recipients_fn=lambda: ["optimus"],
        heartbeat_fn=lambda **kwargs: heartbeats.append(kwargs),
    )

    assert result.returncode == 2
    assert "host PID visibility" in " ".join(result.errors)
    assert heartbeats == []


def test_watcher_cannot_assess_other_recipient_does_not_skip_but_blocks_heartbeat():
    called_recipients = []
    heartbeats = []

    def run(argv, **kwargs):
        if "list-panes" in argv:
            return _cp(
                argv,
                stdout=(
                    "%1\ts\toptimus\t\tcodex\n"
                    "%2\ts\ttarzan\t\tcodex\n"
                ),
            )
        if "--quiescence-pane" in argv:
            return _visible_status(argv)
        if argv[0] == "/watcher":
            called_recipients.append(argv[1])
            return _cp(argv, rc=2 if argv[1] == "optimus" else 0)
        raise AssertionError(argv)

    result = timer.sweep_once(
        socket="sock",
        interval_seconds=60.0,
        status_command="/status",
        watcher_command="/watcher",
        run=run,
        recipients_fn=lambda: ["optimus", "tarzan"],
        heartbeat_fn=lambda **kwargs: heartbeats.append(kwargs),
    )

    assert called_recipients == ["optimus", "tarzan"]
    assert result.returncode == 2
    assert heartbeats == []


def test_tick_with_no_pending_messages_still_proves_drainer_health():
    heartbeats = []

    def run(argv, **kwargs):
        if "list-panes" in argv:
            return _cp(argv, stdout="%1\ts\tlincoln\t\tclaude\n")
        return _visible_status(argv)

    result = timer.sweep_once(
        socket=None,
        interval_seconds=60.0,
        status_command="/status",
        watcher_command="/watcher",
        run=run,
        recipients_fn=lambda: [],
        heartbeat_fn=lambda **kwargs: heartbeats.append(kwargs),
    )
    assert result.returncode == 0
    assert heartbeats == [{"interval_seconds": 60.0}]


@pytest.mark.parametrize(
    ("active", "unit_text", "cron_text", "socket", "expected"),
    [
        (False, "", "", None, 3),
        (
            True,
            "ExecStart=%h/.local/bin/sable-inbox-timer --once --socket fleet-a\n",
            "",
            "fleet-a",
            0,
        ),
        (
            True,
            "ExecStart=%h/.local/bin/sable-inbox-timer --once --socket fleet-a\n",
            "",
            "fleet-b",
            3,
        ),
        (
            False,
            "",
            "* * * * * sable-inbox-timer --once\n",
            None,
            0,
        ),
    ],
)
def test_schedule_check_asks_live_scheduler_and_pins_socket(
    monkeypatch, active, unit_text, cron_text, socket, expected,
):
    monkeypatch.setattr(
        timer,
        "read_live_schedule",
        lambda: (active, unit_text, cron_text),
    )
    assert timer.check_schedule(socket) == expected


def _write_staged_units(units_dir: Path, socket: str | None) -> None:
    socket_arg = f" --socket {socket}" if socket else ""
    (units_dir / timer.SERVICE_UNIT).write_text(
        "ExecStart=%h/.local/bin/sable-inbox-timer --once" + socket_arg + "\n"
    )
    (units_dir / timer.TIMER_UNIT).write_text("Unit=sable-inbox-timer.service\n")


def test_install_schedule_refuses_socket_mismatch_before_copy_or_systemctl(
    tmp_path, monkeypatch, capsys,
):
    units_dir = tmp_path / "staged"
    units_dir.mkdir()
    _write_staged_units(units_dir, "fleet-a")
    destination = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(destination))
    calls = []
    monkeypatch.setattr(timer.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(
        timer,
        "_run_text",
        lambda argv: calls.append(argv) or (0, ""),
    )

    assert timer.install_schedule(units_dir, "fleet-b") == 2
    assert calls == []
    assert not destination.exists()
    assert "does not match staged socket" in capsys.readouterr().err


def test_install_schedule_adopts_staged_socket_and_verifies_live_state(
    tmp_path, monkeypatch,
):
    units_dir = tmp_path / "staged"
    units_dir.mkdir()
    _write_staged_units(units_dir, "fleet-a")
    destination = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(destination))
    calls = []
    checked = []
    monkeypatch.setattr(timer.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(
        timer,
        "_run_text",
        lambda argv: calls.append(argv) or (0, ""),
    )
    monkeypatch.setattr(
        timer,
        "check_schedule",
        lambda socket: checked.append(socket) or 0,
    )

    assert timer.install_schedule(units_dir, None) == 0
    assert checked == ["fleet-a"]
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", timer.TIMER_UNIT],
    ]
    assert (destination / "systemd/user" / timer.SERVICE_UNIT).is_file()
    assert (destination / "systemd/user" / timer.TIMER_UNIT).is_file()
