#!/usr/bin/env python3
"""Focused unit tests for the shared tmux pane-state primitives.

The higher-level sable-msg, session, registry, and worker-status suites exercise
parts of this module through their callers.  This matching suite pins the
library's own boundaries: pane-text classification, polling/delivery state
transitions, tmux command construction, and fail-open pane metadata reads.
"""
import base64
import inspect
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_pane_lib as lib  # noqa: E402


def _real_capture(encoded: str) -> str:
    """Decode a byte-exact cropped `tmux capture-pane -p -J -e` frame."""
    return base64.b64decode(encoded).decode()


# Captured 2026-07-31 from real Claude Code (opus) and Codex (gpt-5.6-sol)
# TUIs on the private `sable-pane-state-fixtures` tmux socket. Cropping retains
# the composer/status/footer rows the state predicates consume; base64 retains
# SGR styling, non-breaking spaces, and trailing cells exactly.
REAL_PANE_CAPTURES = {
    "claude": {
        "idle": _real_capture(
            "ICDijr8gwqBJbnRlcnJydXB0ZWQgwrcgV2hhdCBzaG91bGQgQ2xhdWRlIGRvIGluc3RlYWQ/ICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBDdHJsK1kgdG8gcGFzdGUgZGVsZXRlZCB0ZXh0CuKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKIgArina/CoCAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIArilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIAKICDimqAgVHJhbnNjcmlwdCB3cml0ZXMgYXJlIGZhaWxpbmcgKHJlYWQtb25seSBmaWxlc3lzdGVtIOKAlCBFUk9GUykgwrcgcmVjZW50IG1lc3Nh4oCmCiAgZGRjQEtXLUxQVC0wNTA6fi9kZXYtZW52aXJvbm1lbnQvd2stcGFuZS1zdGF0ZQogIOKPteKPtSBieXBhc3MgcGVybWlzc2lvbnMgb24gKHNoaWZ0K3RhYiB0byBjeWNsZSkgwrcg4oaQIGZvciBhZ2VudHMK"
        ),
        "held": _real_capture(
            "ICDijr8gwqBJbnRlcnJydXB0ZWQgwrcgV2hhdCBzaG91bGQgQ2xhdWRlIGRvIGluc3RlYWQ/ICAgICAgICAgICAgIAoK4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSACuKdr8KgZml4dHVyZSBoZWxkIHRleHQgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCuKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgAogIOKaoCBUcmFuc2NyaXB0IHdyaXRlcyBhcmUgZmFpbGluZyAocmVhZC1vbmx5IGZpbGVzeXN0ZW0g4oCUIEVST0ZTKSDCtyByZWNlbnQgbWVzc2HigKYKICBkZGNAS1ctTFBULTA1MDp+L2Rldi1lbnZpcm9ubWVudC93ay1wYW5lLXN0YXRlCiAg4o+14o+1IGJ5cGFzcyBwZXJtaXNzaW9ucyBvbiAoc2hpZnQrdGFiIHRvIGN5Y2xlKSAgICAgICAgICAgICAgIAo="
        ),
        "midturn": _real_capture(
            "ICAgICAgICAgICAgIArinKIgRmlkZGxlLWZhZGRsaW5n4oCmICg0cyDCtyB0aGlua2luZyB3aXRoIHhoaWdoIGVmZm9ydCkgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCuKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKIgArina/CoCAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIArilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIAKICDimqAgVHJhbnNjcmlwdCB3cml0ZXMgYXJlIGZhaWxpbmcgKHJlYWQtb25seSBmaWxlc3lzdGVtIOKAlCBFUk9GUykgwrcgcmVjZW50IG1lc3Nh4oCmCiAgZGRjQEtXLUxQVC0wNTA6fi9kZXYtZW52aXJvbm1lbnQvd2stcGFuZS1zdGF0ZQogIOKPteKPtSBieXBhc3MgcGVybWlzc2lvbnMgb24gKHNoaWZ0K3RhYiB0byBjeWNsZSkgwrcg4oaQIGZvciBhZ2VudHMK"
        ),
    },
    "codex": {
        "idle": _real_capture(
            "ChtbMG3ilqAgQ29udmVyc2F0aW9uIGludGVycnVwdGVkIC0gdGVsbCB0aGUgbW9kZWwgd2hhdCB0byBkbyBkaWZmZXJlbnRseS4gU29tZXRoaW5nCndlbnQgd3Jvbmc/IEhpdCBgL2ZlZWRiYWNrYCB0byByZXBvcnQgdGhlIGlzc3VlLgoKChtbMW3igLobWzBtIBtbMm1TdW1tYXJpemUgcmVjZW50IGNvbW1pdHMKChtbMG0gIGdwdC01LjYtc29sIGhpZ2ggwrcgfi9kZXYtZW52aXJvbm1lbnQvd2stcGFuZS1zdGF0ZSDCtyBuZXZlciDCtyBDb250ZXh0IDEyJSB1c2Vk4oCmCg=="
        ),
        "held": _real_capture(
            "ChtbMG3ilqAgQ29udmVyc2F0aW9uIGludGVycnVwdGVkIC0gdGVsbCB0aGUgbW9kZWwgd2hhdCB0byBkbyBkaWZmZXJlbnRseS4gU29tZXRoaW5nCndlbnQgd3Jvbmc/IEhpdCBgL2ZlZWRiYWNrYCB0byByZXBvcnQgdGhlIGlzc3VlLgoKChtbMW3igLobWzBtIGZpeHR1cmUgaGVsZCB0ZXh0ICAgICAgIAoKICBncHQtNS42LXNvbCBoaWdoIMK3IH4vZGV2LWVudmlyb25tZW50L3drLXBhbmUtc3RhdGUgwrcgbmV2ZXIgwrcgQ29udGV4dCAxMiUgdXNlZOKApgo="
        ),
        "midturn": _real_capture(
            "G1sxOzJt4oC6IBtbMG1FeHBsYWluIHBhbmUgc3RhdGUgZGV0ZWN0aW9uIHdpdGggc2V2ZXJhbCBleGFtcGxlcy4KCgrigKIgG1sybVdvcmtpbhtbMG1nIBtbMm0oM3Mg4oCiIGVzYyB0byBpbnRlcnJ1cHQpCgoKG1swOzFt4oC6G1swbSAbWzJtU3VtbWFyaXplIHJlY2VudCBjb21taXRzCgobWzBtICBncHQtNS42LXNvbCBoaWdoIMK3IH4vZGV2LWVudmlyb25tZW50L3drLXBhbmUtc3RhdGUgwrcgbmV2ZXIgwrcgQ29udGV4dCAxMiUgdXNlZOKApgo="
        ),
    },
}

REAL_CLAUDE_COMPLETED = _real_capture(
    "4p2vIFJlcGx5IG9ubHkgT0suCgril48gT0sKCuKcuyBDaHVybmVkIGZvciAxcwogICAgICAgICAgICAgIArina8gRXhwbGFpbiBwYW5lIHN0YXRlIGRldGVjdGlvbiB3aXRoIHNldmVyYWwgZXhhbXBsZXMuCgogIFJlYWQgNSBmaWxlcwo="
)


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_prompt_glyphs_are_provider_specific_and_normalized():
    assert lib.prompt_glyphs("CLAUDE") == ("❯", ">")
    assert lib.prompt_glyphs("CoDeX") == ("›", ">")


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_uniform_pane_state_predicates_on_real_provider_frames(provider):
    captures = REAL_PANE_CAPTURES[provider]

    assert lib.composer_is_empty(captures["idle"], provider)
    assert lib.pane_idle(captures["idle"], provider)
    assert not lib.pane_working(captures["idle"], provider)

    assert not lib.composer_is_empty(captures["held"], provider)
    assert not lib.pane_idle(captures["held"], provider)
    assert not lib.pane_working(captures["held"], provider)

    assert lib.composer_is_empty(captures["midturn"], provider)
    assert not lib.pane_idle(captures["midturn"], provider)
    assert lib.pane_working(captures["midturn"], provider)


def test_public_pane_state_predicates_share_one_interface():
    assert lib.composer_is_empty.__module__ == "sable_pane_lib"
    for name in (
        "pane_idle",
        "pane_working",
        "deliberate_hold",
        "composer_is_empty",
    ):
        predicate = getattr(lib, name)
        assert list(inspect.signature(predicate).parameters) == [
            "capture",
            "provider",
        ]


def test_stray_prompt_line_cannot_hide_real_codex_held_text():
    poisoned = ">\n" + REAL_PANE_CAPTURES["codex"]["held"]

    assert not lib.composer_is_empty(poisoned, "codex")
    assert not lib.pane_idle(poisoned, "codex")


def test_completed_claude_turn_is_not_still_working():
    assert "✻ Churned for 1s" in REAL_CLAUDE_COMPLETED
    assert not lib.pane_working(REAL_CLAUDE_COMPLETED, "claude")


def test_completed_claude_turn_does_not_block_busy_leg_rescue_enter():
    snippet = "parked dispatch"
    parked = REAL_CLAUDE_COMPLETED + f"\n────\n❯ {snippet}\n────\n"
    submitted = REAL_CLAUDE_COMPLETED + f"\n❯ {snippet}\n● accepted\n❯ \n"
    captures = iter([
        REAL_PANE_CAPTURES["claude"]["midturn"],
        parked,
        submitted,
    ])
    commands = []

    assert lib.deliver_text(
        ["tmux"],
        "%8",
        snippet,
        snippet,
        tries=1,
        interval=0,
        run=lambda cmd: commands.append(cmd) or True,
        capture=lambda: next(captures),
        sleep=lambda _interval: None,
        provider="claude",
    )
    assert commands == [
        ["tmux", "send-keys", "-t", "%8", "-l", snippet],
        ["tmux", "send-keys", "-t", "%8", "Enter"],
        ["tmux", "send-keys", "-t", "%8", "Enter"],
    ]


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
    # -e joined -J here in SABLE-6c391: styling is what distinguishes Codex's
    # ghost suggestion from real held input, and tmux drops it without -e.
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
                "-e",
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


def test_deliver_text_waits_before_sending_enter():
    text = "submit after the paste-burst window"
    events = []
    typed = False

    def run(cmd):
        nonlocal typed
        events.append(("run", cmd))
        if "-l" in cmd:
            typed = True
        return True

    def capture():
        if not typed:
            return "● prior output\n❯ "
        return f"❯ {text}\n● accepted\n❯ "

    assert lib.deliver_text(
        ["tmux"],
        "%8",
        text,
        text,
        tries=1,
        interval=0,
        run=run,
        capture=capture,
        sleep=lambda delay: events.append(("sleep", delay)),
    )

    literal = ("run", ["tmux", "send-keys", "-t", "%8", "-l", text])
    enter = ("run", ["tmux", "send-keys", "-t", "%8", "Enter"])
    literal_index = events.index(literal)
    enter_index = events.index(enter)
    waits_between = [
        delay
        for kind, delay in events[literal_index + 1:enter_index]
        if kind == "sleep"
    ]
    assert waits_between and max(waits_between) >= lib.SUBMIT_GAP_SECONDS

    # Negative control: the fix inserts time, not another keystroke.
    assert sum(kind == "run" and command[1] == "send-keys"
               for kind, command in events) == 2


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
            "=sable-repo:",
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


# --- Codex ghost-placeholder composer (SABLE-6c391) --------------------------
# Codex renders a rotating SUGGESTION inside its composer while idle, so the
# composer line is never the bare glyph and pane_ready returned False forever
# for a perfectly healthy Codex pane — blocking every Codex spawn and send.
#
# The fixtures below are the REAL bytes from `tmux capture-pane -p -e` against
# a live Codex pane (v0.146.0), not hand-written approximations. The whole fix
# rests on the styling difference between them:
#   ghost placeholder -> remainder wrapped in SGR 2 (dim/faint)
#   real typed text   -> remainder carries no styling at all
# That distinction is load-bearing: treating any glyph-prefixed line as ready
# would type OVER an operator's genuinely-held unsubmitted text (SABLE-r3fg0).

CODEX_GHOST_LINE = "\x1b[1m›\x1b[0m \x1b[2mSummarize recent commits\x1b[0m"
# The SAME ghost, captured through capture_pane's own `-J -e` against a live
# pane rather than by hand. TWO differences from the line above, both of which
# broke a first draft of the fix and neither of which was guessable:
#   * the glyph's SGR is "0;1" here, not "1"
#   * the dim span is NEVER CLOSED — no trailing reset, it just runs to EOL
# A ghost matcher that requires a closing reset silently fails to strip this,
# which is indistinguishable from the original bug.
CODEX_GHOST_LINE_UNTERMINATED = (
    "\x1b[0;1m›\x1b[0m \x1b[2mFind and fix a bug in @filename"
)
CODEX_TYPED_LINE = "\x1b[1m›\x1b[0m PROBE_TEXT_DO_NOT_SUBMIT"
CODEX_STATUS = (
    "  gpt-5.6-sol high · ~/dev-environment/SABLE · never · Context 0% used"
)


def test_codex_ghost_placeholder_reads_as_an_empty_composer():
    """An idle Codex pane is READY even though its composer shows suggestion
    text, because that text is ghost styling rather than held input."""
    capture = f"• prior result\n{CODEX_GHOST_LINE}\n{CODEX_STATUS}"

    assert lib.pane_ready(capture, "codex")
    assert not lib.pane_busy(capture, "codex")
    assert lib.pane_idle(capture, "codex")


def test_codex_unterminated_ghost_span_still_reads_as_empty():
    """REAL capture regression: Codex leaves the dim span open to end-of-line,
    so the ghost matcher must not require a closing reset."""
    capture = (
        f"• prior result\n{CODEX_GHOST_LINE_UNTERMINATED}\n{CODEX_STATUS}"
    )

    assert lib.pane_ready(capture, "codex")
    assert lib.pane_idle(capture, "codex")


def test_codex_real_unsubmitted_text_is_not_ready():
    """The deliberate-hold case: unstyled text after the glyph is something a
    human actually typed, so the pane must NOT be treated as ready."""
    capture = f"• prior result\n{CODEX_TYPED_LINE}\n{CODEX_STATUS}"

    assert not lib.pane_ready(capture, "codex")
    assert not lib.pane_idle(capture, "codex")


def test_codex_midturn_pane_is_busy_and_not_idle():
    """Codex DOES render the 'esc to interrupt' affordance mid-turn (verified
    live). Pins it so the shared busy marker is not 'simplified' away as
    Claude-only — SABLE-0ro7r was filed on exactly that false inference."""
    capture = (
        f"• Working (6s • esc to interrupt)\n{CODEX_GHOST_LINE}\n{CODEX_STATUS}"
    )

    assert lib.pane_busy(capture, "codex")
    assert not lib.pane_idle(capture, "codex")


def test_styled_capture_does_not_change_claude_semantics():
    """REGRESSION (SABLE-6c391): stripping SGR so styled captures parse must
    leave the Claude path byte-identical, and must NOT extend ghost tolerance
    to Claude — Claude suppresses suggestions at the source via
    CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=0 (SABLE-ndaup)."""
    styled_bare = "● prior result\n\x1b[1m❯\x1b[0m \n  ddc@host:~/repo"
    assert lib.pane_ready(styled_bare, "claude")

    plain_bare = "● prior result\n❯ \n  ddc@host:~/repo"
    assert lib.pane_ready(plain_bare, "claude")

    # A dim span on the Claude path is still held text, not a ghost to skip.
    claude_dim = "● prior\n\x1b[1m❯\x1b[0m \x1b[2mleftover text\x1b[0m"
    assert not lib.pane_ready(claude_dim, "claude")


# --- Codex queued-message footer (SABLE-yuwrs) ------------------------------
# Verbatim from a live Codex pane holding a queued lincoln->chuck message. The
# Claude footer ("press up to edit queued messages") never appears in a Codex
# pane, so queued sends were invisible and sable-msg reported UNDELIVERED on a
# message that had in fact landed — filing a spurious durable inbox bead and
# handing the recipient the same instruction twice.
CODEX_QUEUED_FOOTER = (
    "• Messages to be submitted after next tool call "
    "(press esc to interrupt and send immediately)"
)


def test_codex_queued_footer_counts_as_delivery_evidence():
    snippet = "hold all promotions"
    capture = (
        f"• Working (14s • esc to interrupt)\n"
        f"{CODEX_QUEUED_FOOTER}\n"
        f"  ↳ ⟦SABLE-MSG⟧ from=lincoln to=chuck :: {snippet}\n"
        f"› \n"
    )

    assert lib.pane_has_queued_message(capture, snippet, "codex")
    assert lib.submitted_own_turn(capture, snippet, "codex")


def test_codex_pane_without_the_footer_is_not_evidence():
    """Guards against a vacuous fix: the snippet being merely PRESENT must not
    count. Without the footer this is an unsubmitted line sitting in the box."""
    snippet = "hold all promotions"
    capture = f"• Working (14s • esc to interrupt)\n› {snippet}\n"

    assert not lib.pane_has_queued_message(capture, snippet, "codex")


def test_claude_queued_footer_is_unchanged_by_the_codex_addition():
    """REGRESSION: the Claude marker must keep working, and the two providers'
    footers must not be interchangeable — a Codex footer in a Claude pane is
    not evidence, and vice versa."""
    snippet = "status please"
    claude_cap = f"❯ {snippet}\nPress up to edit queued messages\n  cwd"
    assert lib.pane_has_queued_message(claude_cap, snippet, "claude")

    codex_footer_in_claude = f"❯ {snippet}\n{CODEX_QUEUED_FOOTER}\n  cwd"
    assert not lib.pane_has_queued_message(codex_footer_in_claude, snippet, "claude")

    claude_footer_in_codex = f"› {snippet}\nPress up to edit queued messages\n"
    assert not lib.pane_has_queued_message(claude_footer_in_codex, snippet, "codex")


def test_git_common_dir_is_shared_by_a_linked_worktree(tmp_path):
    """SABLE-82k8m: the Codex sandbox grant for git writes must name the git
    COMMON dir, not <cwd>/.git. A worker runs in a LINKED WORKTREE where .git
    is a FILE and the real git dir lives under the main repo, so granting the
    worktree's own path would leave every worker push blocked while managers
    worked fine — a failure that only appears at the push."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-q", "--allow-empty",
                    "-m", "root"], check=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t",
                        "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q",
                    str(wt), "-b", "probe"], check=True)

    from_main = lib.git_common_dir(str(main))
    from_worktree = lib.git_common_dir(str(wt))

    assert from_main == from_worktree, "worktree must resolve to the SHARED git dir"
    assert from_main == os.path.realpath(str(main / ".git"))
    # the worktree's own .git is a FILE — granting it would not cover writes
    assert (wt / ".git").is_file()


def test_git_common_dir_outside_a_repo_is_none(tmp_path):
    assert lib.git_common_dir(str(tmp_path)) is None


def test_capture_pane_requests_styled_output():
    """capture_pane must pass -e; without it tmux strips the SGR the codex
    composer check depends on, and the ghost/held distinction is unrecoverable
    no matter how the predicate is written."""
    seen = {}

    def fake_run(cmd, capture_output, text):
        seen["cmd"] = cmd
        return SimpleNamespace(stdout="pane text\n")

    import sable_pane_lib

    original = sable_pane_lib.subprocess.run
    sable_pane_lib.subprocess.run = fake_run
    try:
        lib.capture_pane(["tmux", "-L", "fleet"], "%7")
    finally:
        sable_pane_lib.subprocess.run = original

    assert "-e" in seen["cmd"]
    assert "-J" in seen["cmd"]
