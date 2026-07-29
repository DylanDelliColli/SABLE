#!/usr/bin/env python3
"""Focused unit tests for the shared tmux pane-state primitives.

The higher-level sable-msg, session, registry, and worker-status suites exercise
parts of this module through their callers.  This matching suite pins the
library's own boundaries: pane-text classification, polling/delivery state
transitions, tmux command construction, and fail-open pane metadata reads.
"""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_pane_lib as lib  # noqa: E402


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_prompt_glyphs_are_provider_specific_and_normalized():
    assert lib.prompt_glyphs("CLAUDE") == ("❯", ">")
    assert lib.prompt_glyphs("CoDeX") == ("›", ">")


def test_pane_state_distinguishes_ready_busy_idle_and_working():
    idle = "● prior result\n\x00❯\x7f \n  ddc@host:~/repo"
    busy = idle + "\n✻ Thinking… (12s · esc  to\n interrupt)"
    spinner_only = "⣾ Working… 4m 36s\n  output continues"

    assert lib.pane_ready(idle)
    assert not lib.pane_busy(idle)
    assert lib.pane_idle(idle)

    assert lib.pane_ready(busy)
    assert lib.pane_busy(busy)
    assert not lib.pane_idle(busy)
    assert lib.pane_working(busy)

    assert not lib.pane_busy(spinner_only)
    assert lib.pane_working(spinner_only)
    assert not lib.pane_working("● completed item 8s ago")


@pytest.mark.parametrize(
    ("capture", "key"),
    [
        ("Bypass Permissions Mode\n1. No, exit\n2. Yes, I accept", "2"),
        ("Do you trust this folder?\n1. Yes\n2. No", "1"),
        ("Trust this folder\n1. Yes, I trust this folder", "1"),
        ("ordinary boot splash", None),
    ],
)
def test_accept_startup_gate_only_accepts_known_gates(capture, key):
    assert lib.accept_startup_gate(capture) == key


def test_dialog_and_overlay_classifiers_require_interactive_posture():
    assert lib.dialog_posture("1. No, exit\n❯ 2. Yes, I accept")
    assert lib.dialog_posture("Choose a model\nEnter to select")
    assert not lib.dialog_posture("1. an ordinary numbered transcript line")

    live_overlay = "old output\n❯\nUsage details\nEsc to close"
    assert lib.overlay_evidence(live_overlay) == "Esc to close"
    assert lib.overlay_posture(live_overlay)

    # The same words in transcript history are not a live overlay once a new
    # composer is drawn beneath them.
    relayed_mention = "The other pane says Enter to select\n› "
    assert lib.overlay_evidence(relayed_mention, provider="codex") is None
    assert not lib.overlay_posture("✻ Thinking… esc to interrupt")


@pytest.mark.parametrize(
    ("provider", "capture", "expected"),
    [
        (
            "claude",
            "You have hit your session limit - resets 2pm",
            "2pm",
        ),
        (
            "codex",
            "Usage limit reached; try again tomorrow at 9am",
            "tomorrow at 9am",
        ),
        ("claude", "ordinary completed turn\n❯ ", None),
    ],
)
def test_session_limit_reset_extracts_provider_banner(provider, capture, expected):
    assert lib.session_limit_reset(capture, provider=provider) == expected


def test_dispatch_landed_fails_closed_until_text_leaves_composer():
    snippet = "ship the release"

    assert not lib.dispatch_landed("booting\nship the release", snippet)
    assert not lib.dispatch_landed("❯ ship the\n release\n  cwd", snippet)
    assert lib.dispatch_landed(
        "❯ ship the release\n● accepted\n❯ \n  cwd",
        snippet,
    )

    # During the redraw immediately after Enter, the submitted echo can still
    # be the last prompt line; the busy marker below it proves submission.
    assert lib.dispatch_landed(
        "❯ ship the release\n✻ Thinking… (1s · esc to interrupt)",
        snippet,
    )


def test_queued_footer_is_independent_delivery_evidence():
    snippet = "status please"
    queued = "❯ status please\nPress up to edit queued messages\n  cwd"

    assert not lib.dispatch_landed(queued, snippet)
    assert lib.pane_has_queued_message(queued, snippet)
    assert lib.submitted_own_turn(queued, snippet)
    assert not lib.pane_has_queued_message(
        "❯ another request\nPress up to edit queued messages",
        snippet,
    )


def test_capture_pane_joins_wrapped_lines_and_returns_stdout(monkeypatch):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "pane text\n", "")

    monkeypatch.setattr(lib.subprocess, "run", fake_run)

    assert lib.capture_pane(["tmux", "-L", "fleet"], "%7") == "pane text\n"
    assert seen == [
        (
            [
                "tmux",
                "-L",
                "fleet",
                "capture-pane",
                "-p",
                "-J",
                "-t",
                "%7",
            ],
            {"capture_output": True, "text": True},
        )
    ]


def test_wait_for_ready_accepts_gate_then_observes_prompt():
    captures = iter(
        [
            "Bypass Permissions Mode\n1. No, exit\n2. Yes, I accept",
            "boot complete\n❯ ",
        ]
    )
    commands = []
    sleeps = []

    assert lib.wait_for_ready(
        ["tmux"],
        "%2",
        timeout=2,
        interval=0.25,
        capture=lambda: next(captures),
        sleep=sleeps.append,
        run=lambda cmd: commands.append(cmd) or True,
    )
    assert commands == [
        ["tmux", "send-keys", "-t", "%2", "2"],
        ["tmux", "send-keys", "-t", "%2", "Enter"],
    ]
    assert sleeps == [0.25]


def test_wait_for_idle_does_not_confuse_busy_composer_with_idle():
    captures = iter(
        [
            "✻ Thinking… (3s · esc to interrupt)\n❯ ",
            "● complete\n❯ ",
        ]
    )
    sleeps = []

    assert lib.wait_for_idle(
        ["tmux"],
        "%3",
        timeout=1,
        interval=0.1,
        capture=lambda: next(captures),
        sleep=sleeps.append,
        run=lambda cmd: pytest.fail(f"unexpected gate command: {cmd}"),
    )
    assert sleeps == [0.1]


def test_wait_helpers_timeout_cleanly_without_a_prompt():
    captures = []

    assert not lib.wait_for_ready(
        ["tmux"],
        "%4",
        timeout=0.3,
        interval=0.1,
        capture=lambda: captures.append("poll") or "still booting",
        sleep=lambda _interval: None,
    )
    assert len(captures) == 3


def test_deliver_text_idle_path_types_once_and_confirms_submission():
    text = "perform the handoff"
    state = {"typed": False}
    commands = []

    def run(cmd):
        commands.append(cmd)
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            return "● prior output\n❯ "
        return f"❯ {text}\n● accepted\n❯ "

    assert lib.deliver_text(
        ["tmux"],
        "%8",
        text,
        text,
        tries=2,
        interval=0,
        run=run,
        capture=capture,
        sleep=lambda _interval: None,
    )
    assert commands == [
        ["tmux", "send-keys", "-t", "%8", "-l", text],
        ["tmux", "send-keys", "-t", "%8", "Enter"],
    ]


def test_deliver_text_returns_false_when_literal_send_fails():
    calls = []

    def run(cmd):
        calls.append(cmd)
        return False

    assert not lib.deliver_text(
        ["tmux"],
        "%9",
        "message",
        "message",
        run=run,
        capture=lambda: "❯ ",
        sleep=lambda _interval: None,
    )
    assert calls == [["tmux", "send-keys", "-t", "%9", "-l", "message"]]


def test_tmux_session_metadata_helpers_build_exact_targets():
    calls = []

    def run(cmd):
        calls.append(cmd)
        if cmd[1] == "has-session":
            return _proc(0)
        return _proc(0, "/repo/root\n")

    assert lib.tmux_base() == ["tmux"]
    assert lib.tmux_base("fleet") == ["tmux", "-L", "fleet"]
    assert lib.session_exists(["tmux"], "sable-repo", run=run)
    assert lib.session_repo(["tmux"], "sable-repo", run=run) == "/repo/root"
    assert calls == [
        ["tmux", "has-session", "-t", "=sable-repo"],
        [
            "tmux",
            "show-options",
            "-v",
            "-t",
            "=sable-repo",
            "@sable_repo",
        ],
    ]


def test_pane_tag_helpers_read_values_and_fail_open():
    values = {
        "@sable_role": "optimus\n",
        "@sable_provider": "CoDeX\n",
        "@sable_bead": "SABLE-123\n",
    }

    def run(cmd):
        return _proc(0, values[cmd[-1]])

    base = ["tmux"]
    assert lib.pane_role_tag(base, "%1", run=run) == "optimus"
    assert lib.pane_provider_tag(base, "%1", run=run) == "codex"
    assert lib.pane_bead_tag(base, "%1", run=run) == "SABLE-123"
    assert lib.pane_bead_tag(base, "", run=run) is None

    def unavailable(_cmd):
        raise OSError("tmux unavailable")

    assert lib.pane_role_tag(base, "%1", run=unavailable) is None
    assert lib.pane_provider_tag(base, "%1", run=unavailable) == "claude"
    assert lib.pane_bead_tag(base, "%1", run=unavailable) is None


def test_pane_process_identity_reads_authoritative_process_environment(tmp_path):
    proc_dir = tmp_path / "4242"
    proc_dir.mkdir()
    (proc_dir / "environ").write_bytes(
        b"CLAUDE_AGENT_NAME=legacy\x00SABLE_AGENT_NAME=tarzan\x00"
    )

    def run(cmd):
        assert cmd[-1] == "#{pane_pid}"
        return _proc(0, "4242\n")

    assert lib.pane_pid(["tmux"], "%6", run=run) == "4242"
    assert (
        lib.pane_process_identity(
            ["tmux"],
            "%6",
            run=run,
            proc_root=str(tmp_path),
        )
        == "tarzan"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, lib.WORKER_CAP_DEFAULT),
        ("", lib.WORKER_CAP_DEFAULT),
        ("0", 0),
        ("3", 3),
        ("-1", lib.WORKER_CAP_DEFAULT),
        ("many", lib.WORKER_CAP_DEFAULT),
    ],
)
def test_worker_cap_is_bounded_and_fail_closed(raw, expected):
    env = {} if raw is None else {"SABLE_MAX_WORKERS": raw}
    assert lib.worker_cap(env) == expected


def test_kick_message_selects_manager_integrator_and_bounded_lifecycles():
    manager = lib.kick_message("optimus")
    integrator = lib.kick_message("chuck")
    producer = lib.kick_message("victor", "/tmp/report.md")

    assert lib.KICK_TAG in manager
    assert "sable-spawn-worker" in manager
    assert "merge request" in integrator
    assert "BOUNDED PRODUCER" in producer
    assert "/tmp/report.md" in producer
    assert "Never loop back" in producer
