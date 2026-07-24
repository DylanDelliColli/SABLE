#!/usr/bin/env python3
"""Unit tests for bin/sable-msg (loaded by path — the CLI has no .py extension).

Covers the Fresh-Agent-Test spec items for SABLE-bldh.1 (header formatting, arg
parsing, registry lookup), SABLE-bq93 (verified delivery: --interrupt waits for
pane readiness and submission is retried until the framed header is confirmed
in the pane, not just assumed from a zero exit code), and SABLE-6izz
(bead-addressed worker delivery via --bead, and the pinned guarantee that
manager-name lookups never fall through to a worker pane's bead tag).
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

# Load the no-extension CLI as a module (needs an explicit source loader since
# there is no .py suffix for importlib to infer one from).
_LOADER = SourceFileLoader("sable_msg", str(Path(__file__).resolve().parent / "sable-msg"))
_SPEC = importlib.util.spec_from_loader("sable_msg", _LOADER)
sable_msg = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sable_msg)

# sable-msg inserts its own dir on sys.path at import, so the shared helper
# module it imports from is now importable directly for predicate-level tests.
import sable_pane_lib  # noqa: E402


# --- header / message formatting -------------------------------------------

def test_format_message_basic():
    msg = sable_msg.format_message("optimus", "lincoln", "API epic is urgent", 0.0)
    assert msg == ("⟦SABLE-MSG⟧ from=optimus to=lincoln :: API epic is urgent "
                   "[composed=1970-01-01T00:00:00Z]")


def test_format_message_collapses_newlines_and_runs():
    msg = sable_msg.format_message("lincoln", "optimus", "drop auth\n\n  do API   now", 0.0)
    # newlines/extra spaces collapse to single spaces -> single-line, single turn
    assert msg == ("⟦SABLE-MSG⟧ from=lincoln to=optimus :: drop auth do API now "
                   "[composed=1970-01-01T00:00:00Z]")
    assert "\n" not in msg


def test_header_glyph_present():
    assert sable_msg.HEADER == "⟦SABLE-MSG⟧"


# --- composition timestamp (SABLE-xwy0b) ------------------------------------

def test_compose_timestamp_is_utc_iso8601():
    assert sable_msg.compose_timestamp(0.0) == "1970-01-01T00:00:00Z"
    assert sable_msg.compose_timestamp(1_700_000_000.0) == "2023-11-14T22:13:20Z"


def test_format_message_carries_a_composition_timestamp_distinguishable_from_arrival():
    # Fresh-Agent-Test spec item: every rendered message carries a composition
    # timestamp. It is embedded in the HEADER (what's actually typed into the
    # recipient's pane), not merely logged to the sender's stderr — the
    # recipient never sees the sender's stderr, so anything less than this
    # would not be "legible" to the recipient at all.
    msg = sable_msg.format_message("lincoln", "optimus", "cap in force", 1_700_000_000.0)
    assert "composed=2023-11-14T22:13:20Z" in msg
    # distinguishable from arrival time: composed= is the ONLY timestamp in
    # the header, fixed at send time -- nothing here is filled in on receipt.
    assert msg.count("composed=") == 1


def test_message_identity_is_format_message_without_the_composed_suffix():
    # message_identity is what gets typed's IDENTITY for matching purposes
    # (SABLE-xwy0b): the same (frm, to, body) must always produce the same
    # identity regardless of when it was composed, and it must be an exact
    # PREFIX of format_message's output so sable_pane_lib's substring-based
    # matching (dispatch_landed / _already_pending) still finds it.
    identity = sable_msg.message_identity("lincoln", "optimus", "cap in force")
    assert identity == "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    full_a = sable_msg.format_message("lincoln", "optimus", "cap in force", 1000.0)
    full_b = sable_msg.format_message("lincoln", "optimus", "cap in force", 2000.0)
    assert full_a.startswith(identity)
    assert full_b.startswith(identity)
    assert full_a != full_b, "different composed_at must still change the full message"


# --- registry parsing (tmux list-panes output) ------------------------------

def test_parse_panes_basic():
    out = "%1 lincoln\n%2 optimus\n%3 tarzan\n"
    assert sable_msg.parse_panes(out) == {
        "lincoln": "%1",
        "optimus": "%2",
        "tarzan": "%3",
    }


def test_parse_panes_skips_roleless_and_blank():
    # panes with no @sable_role set emit just the pane id (no second field)
    out = "%1 lincoln\n%2 \n%3\n\n%4 optimus\n"
    assert sable_msg.parse_panes(out) == {"lincoln": "%1", "optimus": "%4"}


def test_parse_panes_first_wins_on_duplicate_role():
    out = "%1 optimus\n%2 optimus\n"
    assert sable_msg.parse_panes(out)["optimus"] == "%1"


@pytest.fixture(autouse=True)
def _pin_session(monkeypatch, tmp_path):
    """Keep every test hermetic: main() resolves the target session per-repo
    (SABLE-e1e3.3), which would consult the real tmux server — the env
    override short-circuits that. The recipient identity cross-check (SABLE-to8m)
    would likewise shell to the real tmux server + /proc, so it is stubbed to
    None (no poisoning) by default; the cross-check's own test overrides it.

    SABLE_MSG_STATE_DIR (SABLE-xwy0b) is pinned to a fresh tmp_path per test:
    the supersession generation counter is a real on-disk file keyed by
    (frm, to), and most tests here reuse the same lincoln/optimus pair —
    without per-test isolation every test's registration would pile onto ONE
    shared real-filesystem counter across the whole suite (and across
    repeated local runs), which is exactly the kind of cross-test state
    leakage that makes failures nondeterministic."""
    monkeypatch.setenv("SABLE_TMUX_SESSION", "s")
    monkeypatch.setenv("SABLE_MSG_STATE_DIR", str(tmp_path / "sable-msg-freshness"))
    monkeypatch.setattr(sable_msg, "recipient_identity", lambda pane, socket=None: None)


def test_lookup_pane_found_and_missing():
    fake_out = "%1 lincoln\n%2 optimus\n"
    runner = lambda args: fake_out
    assert sable_msg.lookup_pane("optimus", runner) == "%2"
    assert sable_msg.lookup_pane("ghost", runner) is None


def test_lookup_pane_scopes_to_session_when_given():
    seen = []
    runner = lambda args: seen.append(args) or "%1 optimus\n"
    assert sable_msg.lookup_pane("optimus", runner, session="sable-alpha") == "%1"
    cmd = seen[0]
    assert ["-s", "-t", "sable-alpha"] == cmd[cmd.index("-s"):cmd.index("-s") + 3]
    assert "-a" not in cmd


def test_lookup_worker_by_bead_scopes_to_session_when_given():
    seen = []
    runner = lambda args: seen.append(args) or "%7 worker SABLE-x1\n"
    assert sable_msg.lookup_worker_by_bead("SABLE-x1", runner,
                                           session="sable-alpha") == "%7"
    cmd = seen[0]
    assert "-a" not in cmd and "sable-alpha" in cmd


def test_lookup_pane_missing_session_returns_none():
    def runner(args):
        raise subprocess.CalledProcessError(1, args)
    assert sable_msg.lookup_pane("optimus", runner, session="sable-gone") is None


# --- tmux base / socket isolation -------------------------------------------

def test_tmux_base_default():
    assert sable_msg.tmux_base(None) == ["tmux"]


def test_tmux_base_socket():
    assert sable_msg.tmux_base("sable-test") == ["tmux", "-L", "sable-test"]


# --- arg parsing ------------------------------------------------------------

def test_parse_args_requires_role_and_body():
    with pytest.raises(SystemExit):
        sable_msg.parse_args([])
    with pytest.raises(SystemExit):
        sable_msg.parse_args(["optimus"])  # body missing


def test_parse_args_from_default_and_interrupt():
    ns = sable_msg.parse_args(["optimus", "hi there", "--from", "lincoln"])
    assert ns.to_role == "optimus"
    assert ns.body == "hi there"
    assert ns.frm == "lincoln"
    assert ns.interrupt is False
    assert ns.bead is False
    ns2 = sable_msg.parse_args(["lincoln", "stop", "--interrupt"])
    assert ns2.interrupt is True


def test_parse_args_bead_flag():
    ns = sable_msg.parse_args(["market-brief-package-73t4", "hold the tree claim", "--bead"])
    assert ns.to_role == "market-brief-package-73t4"
    assert ns.bead is True


# --- default sender label: worker sends must not wear the lane manager's name
# (SABLE-qqcd) -----------------------------------------------------------------
# sable-spawn-worker:696 stamps a worker pane's CLAUDE_AGENT_NAME with the
# owning LANE MANAGER's name (deliberately, for push-attribution, SABLE-bldh.13)
# and SABLE_WORKER_PANE=1 ALWAYS (sable-spawn-worker:697, the SABLE-38zi
# disambiguator). Before this fix sable-msg's --from default consulted only
# CLAUDE_AGENT_NAME, so a worker's own send was framed from=<manager> —
# indistinguishable from a real directive from that manager.

def test_resolve_from_worker_pane_labels_as_worker_bead(monkeypatch):
    monkeypatch.setenv("SABLE_WORKER_PANE", "1")
    monkeypatch.setenv("CLAUDE_AGENT_NAME", "tarzan")
    monkeypatch.setenv("SABLE_BEAD", "SABLE-x1")
    assert sable_msg.resolve_from() == "worker:SABLE-x1"


def test_resolve_from_worker_pane_without_sable_bead_env_uses_own_pane_tag(monkeypatch):
    # No $SABLE_BEAD -> falls back to the sending pane's own @sable_bead tag
    # (the tag sable-spawn-worker's worker_pane_tags stamps on every worker pane).
    monkeypatch.setenv("SABLE_WORKER_PANE", "1")
    monkeypatch.setenv("CLAUDE_AGENT_NAME", "tarzan")
    monkeypatch.delenv("SABLE_BEAD", raising=False)
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(
        sable_msg, "pane_bead_tag",
        lambda base, pane, run=None: "SABLE-y2" if pane == "%7" else None,
    )
    assert sable_msg.resolve_from() == "worker:SABLE-y2"


def test_resolve_from_worker_pane_unresolvable_bead_falls_back_to_plain_worker(monkeypatch):
    monkeypatch.setenv("SABLE_WORKER_PANE", "1")
    monkeypatch.delenv("SABLE_BEAD", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert sable_msg.resolve_from() == "worker"


def test_resolve_from_manager_pane_keeps_lane_name_regression_guard(monkeypatch):
    # Without SABLE_WORKER_PANE (a real manager pane), CLAUDE_AGENT_NAME must
    # still resolve to the lane name -- this fix must not relabel managers.
    monkeypatch.delenv("SABLE_WORKER_PANE", raising=False)
    monkeypatch.setenv("CLAUDE_AGENT_NAME", "tarzan")
    assert sable_msg.resolve_from() == "tarzan"


def test_resolve_from_operator_default_when_nothing_set(monkeypatch):
    monkeypatch.delenv("SABLE_WORKER_PANE", raising=False)
    monkeypatch.delenv("CLAUDE_AGENT_NAME", raising=False)
    assert sable_msg.resolve_from() == "operator"


def test_main_worker_pane_default_from_labels_as_worker(monkeypatch, capsys):
    monkeypatch.setenv("SABLE_WORKER_PANE", "1")
    monkeypatch.setenv("CLAUDE_AGENT_NAME", "tarzan")
    monkeypatch.setenv("SABLE_BEAD", "SABLE-i8kv")
    monkeypatch.setattr(sable_msg, "lookup_pane",
                        lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: True)
    rc = sable_msg.main(["tarzan", "status update"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "worker:SABLE-i8kv -> tarzan" in err


def test_main_explicit_from_overrides_worker_default(monkeypatch, capsys):
    # An explicit --from must still win over the worker-pane default.
    monkeypatch.setenv("SABLE_WORKER_PANE", "1")
    monkeypatch.setenv("CLAUDE_AGENT_NAME", "tarzan")
    monkeypatch.setattr(sable_msg, "lookup_pane",
                        lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: True)
    rc = sable_msg.main(["optimus", "status update", "--from", "lincoln"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "lincoln -> optimus" in err


# --- main: missing role is a hard error -------------------------------------

def test_main_missing_role_errors(monkeypatch, capsys):
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None, session=None: None)
    rc = sable_msg.main(["ghost", "hello", "--from", "lincoln"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "ghost" in err


# --- main: delivery is verified, not assumed (SABLE-bq93) -------------------

def test_main_happy_path_reports_delivered(monkeypatch, capsys):
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: True)
    rc = sable_msg.main(["optimus", "ship it", "--from", "lincoln"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "delivered" in err
    assert "optimus" in err


def test_main_refuses_poisoned_role_tag(monkeypatch, capsys):
    # SABLE-to8m: the pane resolved for role 'lincoln' has an authoritative
    # process identity of 'optimus' — a poisoned/stale @sable_role tag. Delivery
    # must be REFUSED (never routed into the wrong pane), before deliver_message
    # is ever called.
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None, session=None: "%9")
    monkeypatch.setattr(sable_msg, "recipient_identity", lambda pane, socket=None: "optimus")
    monkeypatch.setattr(sable_msg, "deliver_message",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deliver")))
    rc = sable_msg.main(["lincoln", "escalation", "--from", "optimus"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "poisoned" in err.lower()
    assert "optimus" in err          # names the real identity
    assert "sable-relink" in err     # points at the recovery path


def test_main_delivers_when_identity_agrees(monkeypatch, capsys):
    # The cross-check must not block a legitimate send: identity == requested role.
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "recipient_identity", lambda pane, socket=None: "optimus")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: True)
    assert sable_msg.main(["optimus", "ship it", "--from", "lincoln"]) == 0
    assert "delivered" in capsys.readouterr().err


def test_main_reports_undelivered_and_exits_nonzero(monkeypatch, capsys):
    # This is the exact SABLE-bq93 false-positive: send-keys "succeeding" must
    # no longer be enough to print `delivered` — verification failing must
    # surface as a hard, non-zero-exit failure with a durable-fallback hint
    # (bd unavailable here, so the manual hint is the fallback's fallback).
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: False)
    monkeypatch.setattr(sable_msg, "file_fallback_bead", lambda *a, **k: None)
    rc = sable_msg.main(["optimus", "cap in force", "--from", "lincoln", "--interrupt"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "undelivered" in err
    assert "optimus" in err
    assert "for-optimus" in err  # durable inbox-bead fallback hint


def test_main_undelivered_auto_files_durable_fallback_bead(monkeypatch, capsys):
    # SABLE-1umr acceptance: failed verification FILES the durable inbox bead
    # (not just advice) and reports its id — delivery degrades to the bead
    # substrate instead of silently degrading to nothing.
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: False)
    calls = []
    monkeypatch.setattr(sable_msg, "file_fallback_bead",
                        lambda frm, to, msg, runner=None: calls.append((frm, to, msg)) or "SABLE-fb42")
    rc = sable_msg.main(["optimus", "cap in force", "--from", "lincoln"])
    assert rc != 0
    assert calls and calls[0][0] == "lincoln" and calls[0][1] == "optimus"
    err = capsys.readouterr().err
    assert "SABLE-fb42" in err


def test_main_undelivered_bead_addressed_does_not_auto_file(monkeypatch, capsys):
    # Worker lanes are owned by their dispatching manager (who sees the nonzero
    # exit live); a for-<bead-id> inbox label would be meaningless. No auto-file.
    monkeypatch.setattr(sable_msg, "lookup_worker_by_bead",
                        lambda bead, run=None, socket=None, session=None: "%9")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: False)
    monkeypatch.setattr(sable_msg, "file_fallback_bead",
                        lambda *a, **k: pytest.fail("must not auto-file for --bead"))
    rc = sable_msg.main(["market-brief-package-73t4", "hold", "--from", "optimus", "--bead"])
    assert rc != 0


def test_file_fallback_bead_creates_for_role_inbox_bead():
    seen = []

    class R:
        returncode = 0
        stdout = "Created issue: SABLE-ab12\n"
        stderr = ""

    def runner(args):
        seen.append(args)
        return R()

    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    bead_id = sable_msg.file_fallback_bead("lincoln", "optimus", message, runner=runner)
    assert bead_id == "SABLE-ab12"
    argv = seen[0]
    assert argv[:2] == ["bd", "create"]
    joined = " ".join(argv)
    assert "for-optimus" in joined
    assert message in joined
    assert "coord" in joined


def test_file_fallback_bead_is_not_filed_p1():
    """costing-comparison-573: an undelivered-message bead is an INBOX item, not
    a work item. Filing it P1 sorted it ABOVE genuine P1 engineering work in
    `bd ready` — one measured pool held 26 items of which 11 were these, all
    outranking the real work. The fallback itself is correct and must keep
    existing: it is what made this session's tmux delivery failures lossless.
    Only its priority is wrong."""
    seen = []

    class R:
        returncode = 0
        stdout = "Created issue: SABLE-ab12\n"
        stderr = ""

    def runner(args):
        seen.append(args)
        return R()

    sable_msg.file_fallback_bead("lincoln", "optimus", "body", runner=runner)
    argv = seen[0]
    assert "--priority=1" not in argv, (
        "fallback inbox beads must not be P1 — they bury real engineering work "
        "in bd ready (costing-comparison-573)"
    )
    assert "--priority=3" in argv
    # The for-ROLE label is how the recipient actually reads their inbox, so it
    # must survive any change to pool membership.
    assert "--labels=for-optimus,coord" in argv


def test_fallback_bead_title_is_composed_from_the_framed_message_3mrv3():
    """SABLE-3mrv3 regression. A recogniser for "did the fallback pollute the
    live DB?" needs the bead's title. The first attempt hand-typed it as
    '<prefix> <role>: <body>' — but every real call site passes the FRAMED
    message, so the framing sits BETWEEN the role and the body and the guess
    matched nothing. `bd count --title-contains <guess>` then returned 0
    unconditionally: an assertion that could not fail, which is worse than the
    global counter it replaced because it looks like coverage.

    Pin both directions: the composed title is what file_fallback_bead actually
    uses, and the naive hand-typed form is NOT a substring of it."""
    body = "sandbox fallback probe"
    framed = sable_msg.format_message("lincoln", "chuck", body, 0.0)
    title = sable_msg.fallback_bead_title("chuck", framed)

    assert title == f"SABLE-MSG undelivered to chuck: {framed[:80]}"
    assert sable_msg.HEADER in title, "the framing must be present in the title"
    assert body in title

    naive = f"SABLE-MSG undelivered to chuck: {body}"
    assert naive not in title, (
        "the hand-typed '<prefix> <role>: <body>' form must NOT match — if it "
        "ever does, the SABLE-3mrv3 trap has silently reopened"
    )


def test_file_fallback_bead_uses_the_shared_title_and_label_helpers_3mrv3():
    """The seam is only useful if the filer and the recogniser cannot drift:
    file_fallback_bead must emit exactly what fallback_bead_title /
    fallback_bead_labels produce, so a test deriving the title from those
    helpers is guaranteed to match a real fallback bead."""
    seen = []

    class R:
        returncode = 0
        stdout = "Created issue: SABLE-ab12\n"
        stderr = ""

    def runner(args):
        seen.append(args)
        return R()

    framed = sable_msg.format_message("lincoln", "chuck", "sandbox fallback probe", 0.0)
    sable_msg.file_fallback_bead("lincoln", "chuck", framed, runner=runner)
    argv = seen[0]
    assert f"--title={sable_msg.fallback_bead_title('chuck', framed)}" in argv
    assert f"--labels={sable_msg.fallback_bead_labels('chuck')}" in argv


def test_file_fallback_bead_returns_none_when_bd_unavailable():
    class R:
        returncode = 1
        stdout = ""
        stderr = "bd: not a beads workspace"

    assert sable_msg.file_fallback_bead("lincoln", "optimus", "msg",
                                        runner=lambda a: R()) is None


# --- seat sightings (SABLE-441vl) -------------------------------------------
#
# CAPTURE IS MANDATORY, PRIORITY IS ADVISORY (cockpit ruling, 2026-07-22
# 16:23, recorded as a comment on SABLE-441vl — NOT in its description, which
# is why an earlier pass here built the wrong thing: a DENY hook on the
# premise the seat cannot create beads at all. That premise is measured
# FALSE). `--file-sighting` is a plain `bd create`, never refused or
# deferred; hooks/multi-manager/seat-sighting-gate.sh annotates the result
# AFTER it lands, for any chuck-identity `bd create` whether or not it went
# through this wrapper.

def test_file_sighting_bead_is_a_single_plain_create():
    """One bd call, not two: capture is mandatory, so there is nothing to
    defer and nothing to promote later."""
    seen = []

    class R:
        returncode = 0
        stdout = "Created issue: SABLE-ab12\n"
        stderr = ""

    def runner(args):
        seen.append(args)
        return R()

    bead_id = sable_msg.file_sighting_bead(
        "chuck", "found a defect while verifying wk-foo", runner=runner)
    assert bead_id == "SABLE-ab12"
    assert len(seen) == 1
    argv = seen[0]
    assert argv[:2] == ["bd", "create"]
    joined = " ".join(argv)
    assert "SEAT-FILED:" in joined
    assert "found a defect while verifying wk-foo" in joined
    assert not any(a.startswith("--status") for a in argv), \
        "capture is mandatory — a sighting must never be deferred"


def test_file_sighting_bead_returns_none_when_bd_unavailable():
    class R:
        returncode = 1
        stdout = ""
        stderr = "bd: not a beads workspace"

    assert sable_msg.file_sighting_bead("chuck", "text", runner=lambda a: R()) is None


def test_sighting_bead_title_truncates_and_prefixes():
    title = sable_msg.sighting_bead_title("x" * 200)
    assert title.startswith(f"{sable_msg.SIGHTING_TITLE_PREFIX}: ")
    assert title == f"{sable_msg.SIGHTING_TITLE_PREFIX}: {'x' * 80}"


def test_main_file_sighting_bypasses_pane_lookup_entirely(monkeypatch, capsys):
    """--file-sighting is a bd write, not a message: it must never touch
    tmux/session resolution at all."""
    monkeypatch.setattr(sable_msg, "resolve_session", lambda *a, **k: pytest.fail(
        "a sighting must not resolve a tmux session"))
    monkeypatch.setattr(sable_msg, "file_sighting_bead", lambda frm, text, **k: "SABLE-xyz")
    rc = sable_msg.main(["--file-sighting", "--from", "chuck", "an observation"])
    assert rc == 0
    assert "SABLE-xyz" in capsys.readouterr().err


def test_main_file_sighting_reports_failure_and_a_manual_fallback(monkeypatch, capsys):
    monkeypatch.setattr(sable_msg, "resolve_session", lambda *a, **k: pytest.fail(
        "a sighting must not resolve a tmux session"))
    monkeypatch.setattr(sable_msg, "file_sighting_bead", lambda frm, text, **k: None)
    rc = sable_msg.main(["--file-sighting", "--from", "chuck", "an observation"])
    assert rc == 1
    assert "could not file" in capsys.readouterr().err


def test_parse_args_file_sighting_treats_the_single_positional_as_body():
    """--file-sighting takes no to_role — argparse fills the first optional
    positional (to_role) greedily, so parse_args must re-route it into body."""
    ns = sable_msg.parse_args(["--file-sighting", "an observation body"])
    assert ns.to_role is None
    assert ns.body == "an observation body"


def test_parse_args_still_requires_to_role_without_file_sighting():
    with pytest.raises(SystemExit):
        sable_msg.parse_args(["--body-file", "-"])


SEAT_GATE_HOOK = Path(__file__).resolve().parent.parent / "hooks" / "multi-manager" / "seat-sighting-gate.sh"


def _run_seat_gate(command, agent_name, agent_role="manager", stdout="", extra_env=None):
    env = dict(os.environ)
    if agent_name:
        env["CLAUDE_AGENT_NAME"] = agent_name
        env["CLAUDE_AGENT_ROLE"] = agent_role or ""
    else:
        env.pop("CLAUDE_AGENT_NAME", None)
        env.pop("CLAUDE_AGENT_ROLE", None)
    env.update(extra_env or {})
    hook_input = json.dumps({
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": ""},
    })
    return subprocess.run(["bash", str(SEAT_GATE_HOOK)], input=hook_input, text=True,
                          capture_output=True, env=env, timeout=10)


def test_seat_gate_never_denies_a_normal_work_bd_create():
    """THE property SABLE-441vl is accepted or rejected on, corrected against
    the actual cockpit ruling: capture is MANDATORY, so a plain work `bd
    create` from the seat's own identity must NEVER be refused. (An earlier,
    wrong pass here asserted the opposite — that this must be denied unless
    labeled a sighting. The ruling that reopened this bead exists precisely
    because that premise was measured false.)"""
    if not SEAT_GATE_HOOK.is_file():
        pytest.skip(f"seat-sighting-gate.sh not found at {SEAT_GATE_HOOK}")
    result = _run_seat_gate(
        'bd create --title="fix the thing" --description="foo bar" --type=task',
        agent_name="chuck", stdout="Created issue: SABLE-ab12 — fix the thing\n")
    assert result.returncode == 0
    assert "deny" not in result.stdout


def test_seat_gate_annotates_the_created_bead_afterward(monkeypatch, tmp_path):
    """The hook's actual job: after a successful chuck-identity `bd create`,
    it runs a follow-up `bd update <id> --add-label seat-filed
    --set-metadata priority_provisional=true`. Verified end to end against a
    real, throwaway bd DB (real bd or self-skip — no mocks)."""
    if not SEAT_GATE_HOOK.is_file():
        pytest.skip(f"seat-sighting-gate.sh not found at {SEAT_GATE_HOOK}")
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    beads_root = tmp_path / "beads"
    beads_root.mkdir()
    init = subprocess.run(["bd", "init", "--prefix=sga"], cwd=str(beads_root),
                          env={**os.environ, "BD_NON_INTERACTIVE": "1"},
                          text=True, capture_output=True, timeout=60)
    assert init.returncode == 0, init.stdout + init.stderr
    beads_db = str(beads_root / ".beads")

    created = subprocess.run(
        ["bd", "create", "--title=found a defect", "--description=text [no-test]",
         "--type=task"],
        env={**os.environ, "BEADS_DB": beads_db}, text=True, capture_output=True, timeout=30)
    assert created.returncode == 0, created.stdout + created.stderr
    bead_id = re.search(r"Created issue:\s*(\S+)", created.stdout).group(1)

    result = _run_seat_gate(
        'bd create --title="found a defect" --description="text [no-test]" --type=task',
        agent_name="chuck", stdout=created.stdout, extra_env={"BEADS_DB": beads_db})
    assert result.returncode == 0
    # The hook's own `bd update` call must reach the SAME isolated DB.
    show = subprocess.run(["bd", "show", bead_id, "--json"],
                          env={**os.environ, "BEADS_DB": beads_db}, text=True,
                          capture_output=True, timeout=30)
    data = json.loads(show.stdout)
    data = data[0] if isinstance(data, list) else data
    assert "seat-filed" in (data.get("labels") or [])
    assert (data.get("metadata") or {}).get("priority_provisional") in (True, "true", "True")


def test_seat_gate_ignores_non_seat_identities():
    """Every other manager's `bd create` — the ordinary work-filing path —
    must pass through untouched. This hook has exactly one job."""
    if not SEAT_GATE_HOOK.is_file():
        pytest.skip(f"seat-sighting-gate.sh not found at {SEAT_GATE_HOOK}")
    result = _run_seat_gate(
        'bd create --title="fix the thing" --description="foo bar" --type=task',
        agent_name="optimus", stdout="Created issue: SABLE-ab12 — fix the thing\n")
    assert result.returncode == 0


def test_seat_gate_ignores_commands_that_are_not_bd_create():
    if not SEAT_GATE_HOOK.is_file():
        pytest.skip(f"seat-sighting-gate.sh not found at {SEAT_GATE_HOOK}")
    result = _run_seat_gate("bd show SABLE-x", agent_name="chuck")
    assert result.stdout.strip() == ""
    assert result.returncode == 0


# --- deliver_message: stub-tmux retry + verification (SABLE-bq93) -----------

def test_deliver_message_retries_until_header_lands_outside_input_box():
    # Simulates the exact failure mode: the pane is still booting for the first
    # two readiness polls (no empty prompt line -> wait_for_ready keeps waiting),
    # then becomes ready but the typed message sits unsubmitted in the input box
    # for two more checks (the dropped-Enter race) before it finally lands.
    screens = iter([
        "╭─ Claude Code ─╮\n│ booting… │\n╰──────────╯",             # not ready
        "╭─ Claude Code ─╮\n│ booting… │\n╰──────────╯",             # not ready
        "❯ \n  ddc@host:~/wt",                                       # ready+idle (wait_for_idle returns)
        "❯ \n  ddc@host:~/wt",                                       # idle at send (deliver_text t0, SABLE-d21h)
        "❯ ⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force\n  ddc@host:~/wt",  # still in box
        "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force\n"
        "● thinking…\n❯ \n  ddc@host:~/wt",                          # landed
    ])
    sent = []

    def run(cmd):
        sent.append(cmd)
        return True

    def capture():
        return next(screens)

    sleeps = []
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    landed = sable_msg.deliver_message(
        "%2", message, interrupt=True, run=run, capture=capture,
        sleep=sleeps.append, ready_timeout=10, interval=0.01, tries=5,
    )
    assert landed is True
    assert sent[0] == ["tmux", "send-keys", "-t", "%2", "Escape"]
    assert any(c[-3:] == ["send-keys", "-t", "%2"] or c[-2:] == ["-l", message] for c in sent
              if "-l" in c)
    # it genuinely retried (multiple polls/resends), not a single blind send
    assert len(sleeps) >= 2


def test_deliver_message_gives_up_when_never_confirmed_landed():
    # Idle at send time, but the message NEVER leaves the input box (the
    # dropped-Enter race that never wins, or the pane dies mid-turn) ->
    # deliver_message must report failure, not delivery. Stateful fake: an empty
    # idle composer BEFORE we type (so idle_at_send is True and it is NOT the
    # SABLE-d21h guard that fails this), then the message stuck in the box
    # forever thereafter.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    state = {"typed": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            return "❯ \n  ddc@host:~/wt"             # idle at t0
        return f"❯ {message}\n  ddc@host:~/wt"        # stuck in the box thereafter

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run, capture=capture,
        sleep=lambda s: None, tries=3, interval=0.01,
    )
    assert landed is False


# --- visible-versus-submitted: no composer box => not landed (SABLE-wvk9) ----

def test_dispatch_landed_false_when_no_composer_box_even_if_text_visible():
    # The silent-swallow signature: the typed text is VISIBLE in the capture but
    # no composer glyph (❯/>) is locatable — a busy pane whose prompt line was
    # obscured by a spinner/reflow, or a booting/gated pane. We cannot prove the
    # text left the input box, so it must NOT count as a submitted turn. The old
    # `box_start is None -> return True` reported these as delivered while the
    # message sat unsubmitted as pending input (two stranded handoffs).
    snippet = "⟦SABLE-MSG⟧ from=optimus to=chuck :: PR ready from optimus"
    # Uses the two REAL swallow fixtures from the bead design field as the
    # visible-but-boxless payloads.
    for boxless in (
        f"● merging PR…\n✻ Thinking… (8s · esc to interrupt)\n{snippet}",
        "some transcript\n⟦SABLE-MSG⟧ from=lincoln to=worker :: fix SABLE-poka now",
        "prior output\n⟦SABLE-MSG⟧ from=lincoln to=worker :: stand down",
    ):
        # snippet for the fixture rows is the tail after '::'
        want = boxless.split("::", 1)[-1].strip() if "::" in boxless else snippet
        assert sable_pane_lib.dispatch_landed(boxless, want) is False


# --- submitted-echo redraw race: report-NOT-landed-when-it-DID (SABLE-uh4b) ---
# The INVERSE of wvk9. Once a message SUBMITS, Claude Code echoes it into the
# transcript as its own prompt-glyph line ("❯ <msg>", glyph + REGULAR space) and
# starts the turn; in the brief redraw window right after Enter the empty
# composer has not repainted yet, so that echo is momentarily the LAST glyph
# line while the turn already runs beneath it (busy marker below). box_start
# alone mistook the echo for the still-unsubmitted composer and false-negatived
# the landing — filing a duplicate durable fallback bead for a message that
# actually submitted, which blocked a P0 worker release.

def test_dispatch_landed_true_for_submitted_echo_above_busy_marker_uh4b():
    msg = ("⟦SABLE-MSG⟧ from=lincoln to=SABLE-z776 :: GO push your worktree "
           "branch now recovery landed and chuck drained the merge queue")
    # redraw window: submitted echo (regular space) is the last glyph line, the
    # turn is already running BELOW it, composer not yet repainted.
    redraw = ("● prior turn output\n"
              f"❯ {msg}\n"
              "✻ Thinking… (1s · ↓ 8 tokens · esc to interrupt)")
    assert sable_pane_lib.dispatch_landed(redraw, msg) is True
    # steady state a moment later (empty composer repainted at the bottom of the
    # box-drawing frame) must stay landed too — the busy marker now sits ABOVE
    # the composer, so the normal box_start path returns True.
    border = "─" * 120
    steady = ("● prior turn output\n"
              f"❯ {msg}\n✻ Thinking… (esc to interrupt)\n"
              f"{border}\n❯\xa0\n{border}\n  ddc@host:~/wk-idle")
    assert sable_pane_lib.dispatch_landed(steady, msg) is True


def test_dispatch_landed_false_for_idle_message_in_box_frame_uh4b_guard():
    # The false-POSITIVE guard on the uh4b allowance: a message sitting UNSENT in
    # the box-drawing composer while the pane is IDLE (no busy marker anywhere
    # below the prompt) must still count as NOT landed. The redraw-race allowance
    # keys strictly on a running turn beneath the echo, so it can never resurrect
    # the wvk9/d21h silent-swallow (report-landed-when-it-did-not).
    msg = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    border = "─" * 120
    stuck = f"● prior turn output\n{border}\n❯\xa0{msg}\n{border}\n  ddc@host:~/wk-idle"
    assert sable_pane_lib.dispatch_landed(stuck, msg) is False


def test_deliver_message_idle_box_frame_lands_via_redraw_race_uh4b():
    # SABLE-uh4b end-to-end at deliver_message, against a REAL-shaped pane: an
    # empty composer inside a box-drawing frame (── borders, "❯\xa0" prompt), a
    # WIDE message, IDLE at send. After type+Enter the message submits and every
    # post-send capture lands in the redraw window (echo "❯ <msg>" + running turn,
    # composer not repainted). deliver_message must report LANDED. Before the fix
    # this false-negatived on every one of the 8 polls (the exact z776 symptom:
    # 'undelivered after 8 attempts' while the message had actually submitted).
    border = "─" * 128
    nbsp = "\xa0"
    cwd = "  ddc@KW-LPT-050:~/dev-environment/wk-idle-pane-landed"
    mode = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
    msg = ("⟦SABLE-MSG⟧ from=lincoln to=SABLE-z776 :: GO push your worktree "
           "branch now that recovery has landed and chuck has drained the merge "
           "queue so your self-push applies cleanly")
    state = {"typed": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            # idle: empty composer inside the box-drawing frame
            return "\n".join(["● prior turn output", "● done", border,
                              "❯" + nbsp + " " * 40, border, cwd, mode])
        # after submit: redraw window persists — transcript echo (regular space)
        # + running turn, the empty composer has NOT repainted yet.
        return "\n".join(["● prior turn output",
                          "❯ " + msg,
                          "✢ Sautéing… (3s · ↓ 40 tokens · esc to interrupt)"])

    landed = sable_msg.deliver_message(
        "%2", msg, interrupt=True, run=run, capture=capture,
        sleep=lambda s: None, ready_timeout=5, interval=0.001, tries=8,
    )
    assert landed is True


def test_main_landed_box_frame_send_does_not_double_file_fallback_bead_uh4b(monkeypatch):
    # SABLE-uh4b second half: a send the pane ACCEPTS must NOT be double-counted
    # into a durable fallback bead (message delivered + a redundant for-<role>
    # bead — the mirror of the not-delivered-but-reported-success loss). Drive the
    # REAL main -> deliver_message -> deliver_text -> dispatch_landed composition
    # against a box-drawing-frame fake that lands via the redraw race; assert rc 0,
    # "delivered", and that file_fallback_bead was NEVER called.
    monkeypatch.setenv("SABLE_MSG_POLL_INTERVAL", "0")
    monkeypatch.setenv("SABLE_MSG_READY_TIMEOUT", "1")
    monkeypatch.setenv("SABLE_MSG_SUBMIT_TRIES", "4")
    monkeypatch.setattr(sable_msg, "lookup_pane",
                        lambda role, run=None, socket=None, session=None: "%2")
    # Pin _now() so main()'s OWN composed_at matches this test's independently
    # built `framed` string exactly (SABLE-xwy0b) — the fake pane echoes
    # `framed` back, and dispatch_landed needs the REAL delivered message
    # (main()'s) to be a literal substring of that echo.
    monkeypatch.setattr(sable_msg, "_now", lambda: 1_700_000_000.0)

    border = "─" * 128
    cwd = "  ddc@KW-LPT-050:~/dev-environment/wk-idle-pane-landed"
    mode = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
    framed = sable_msg.format_message("lincoln", "optimus",
                                      "GO push your worktree branch now recovery landed",
                                      1_700_000_000.0)
    state = {"typed": False}

    class FakeProc:
        returncode = 0

    def fake_run(cmd, **kw):
        if "-l" in cmd:
            state["typed"] = True
        return FakeProc()

    def fake_capture(base, pane):
        if not state["typed"]:
            return "\n".join(["● prior", border, "❯\xa0" + " " * 20, border, cwd, mode])
        return "\n".join(["● prior", "❯ " + framed,
                          "✻ Thinking… (0s · esc to interrupt)"])

    monkeypatch.setattr(sable_msg.subprocess, "run", fake_run)
    monkeypatch.setattr(sable_msg, "_capture_pane", fake_capture)
    filed = []
    monkeypatch.setattr(sable_msg, "file_fallback_bead",
                        lambda *a, **k: filed.append(a) or "SABLE-should-not-file")

    rc = sable_msg.main(["optimus", "GO push your worktree branch now recovery landed",
                         "--from", "lincoln"])
    assert rc == 0
    assert filed == [], "a landed send must not also file a durable fallback bead"


def test_deliver_message_boxless_visible_text_degrades_to_failure():
    # End-to-end through deliver_message: the pane only ever shows the text with
    # no composer box (never a submitted turn) -> report non-delivery so the
    # caller routes to the durable fallback bead, not a phantom 'delivered'.
    message = "⟦SABLE-MSG⟧ from=optimus to=chuck :: PR ready from optimus"
    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False,
        run=lambda cmd: True,
        # visible in a busy pane, but no ❯/> composer line anywhere
        capture=lambda: f"✻ Thinking… (esc to interrupt)\n{message}",
        sleep=lambda s: None, tries=3, interval=0.01,
    )
    assert landed is False


# --- queued-while-busy: pre-send idle tracking (SABLE-d21h) ------------------
# dispatch_landed alone (visible AND not-in-box) cannot tell "our message
# started this turn" from "our message QUEUED behind a DIFFERENT running turn":
# Claude Code hoists a queued line ABOVE the composer and clears the input box,
# so both look identical in a single capture. A queued line can then be dropped
# on the running turn's compaction/redraw or a pane reap — the swallow that
# stranded two handoffs. The fix captures pane_idle at t0 (before the send) and
# only counts an idle->our-turn transition; busy-at-t0 fails closed so the
# caller routes to the durable fallback bead.

def test_deliver_message_queued_while_busy_is_not_landed():
    # THE bead repro: the pane is BUSY running a DIFFERENT turn at send time
    # (its status line shows 'esc to interrupt'). After we type+Enter, our line
    # is hoisted ABOVE an empty composer while the OTHER turn keeps running —
    # visible AND not-in-box — yet it was only QUEUED, never accepted as its own
    # submitted turn. Pre-send state was busy, so deliver_message must report NOT
    # landed (sable-msg then files the durable fallback).
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other_turn = "● Running the auth refactor…\n✻ Thinking… (12s · esc to interrupt)"
    state = {"typed": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            # t0: someone else's turn is running (busy) — no empty composer.
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"
        # after we type+Enter: our line hoisted above the (still-empty) composer
        # while the OTHER turn keeps running -> visible + not-in-box, but queued.
        return f"{message}\n{other_turn}\n❯ \n  ddc@host:~/wt"

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run, capture=capture,
        sleep=lambda s: None, tries=4, interval=0.01,
    )
    assert landed is False

    # Proof the pre-send idle guard is load-bearing: on the hoisted capture, the
    # old visible-vs-submitted signal (dispatch_landed) would have FALSE-POSITIVED.
    hoisted = f"{message}\n{other_turn}\n❯ \n  ddc@host:~/wt"
    assert sable_pane_lib.dispatch_landed(hoisted, message) is True


def test_deliver_message_idle_recipient_transitions_to_our_turn_and_lands():
    # The PRESERVED happy path: the recipient is IDLE at send time (empty
    # composer, no running turn). After we type+Enter it goes busy processing
    # OUR message, which is visible above the composer -> a genuine idle->our-turn
    # transition -> landed. The guard must not regress this normal case.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: status?"
    state = {"typed": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            return "● earlier turn output\n● done\n❯ \n  ddc@host:~/wt"  # idle at t0
        # now busy processing OUR message, which sits above the composer:
        return f"{message}\n✻ Thinking… (2s · esc to interrupt)\n❯ \n  ddc@host:~/wt"

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run, capture=capture,
        sleep=lambda s: None, tries=4, interval=0.01,
    )
    assert landed is True


def test_deliver_text_fresh_pane_dispatch_still_lands():
    # The sable-spawn-worker path, at the shared helper it actually calls
    # (deliver_text). A FRESH worker pane is idle at the empty composer
    # (wait_for_ready already confirmed the prompt), then LEGITIMATELY goes busy
    # after we submit the dispatch. Idle at t0 -> the dispatch must still land;
    # SABLE-d21h must NOT false-negative worker dispatch.
    dispatch = "Read /home/ddc/.claude/sable/dispatch/SABLE-xyz.md in full"
    snippet = "SABLE-xyz"
    state = {"typed": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            return "❯ \n  ddc@host:~/wk-xyz"           # fresh + idle, empty composer
        return f"{dispatch}\n✻ Working… (esc to interrupt)\n❯ \n  ddc@host:~/wk-xyz"

    landed = sable_pane_lib.deliver_text(
        ["tmux"], "%9", dispatch, snippet,
        tries=4, interval=0.01, run=run, capture=capture, sleep=lambda s: None,
    )
    assert landed is True


# --- delayed confirmation of a busy-at-t0 queued send (SABLE-h0jw) -----------
# SABLE-d21h fixed the queued-while-busy phantom-confirm by failing CLOSED the
# instant the pane was busy at t0 — but that filed a durable noise bead EVEN
# WHEN the queued line genuinely submitted+landed once the running turn ended
# (LINCOLN evidence 2026-07-14: chuck's pane, two instances). h0jw replaces
# fail-close-at-t0 with DELAYED confirmation: keep watching the queued line and
# only confirm once it PROVABLY became its own submitted turn (a signal a still-
# queued capture can never present), failing closed only on timeout.


def test_submitted_own_turn_rejects_queued_accepts_submitted_h0jw():
    # The load-bearing predicate. A line QUEUED behind a DIFFERENT running turn
    # (hoisted above the empty composer, the other turn's busy marker ABOVE that
    # composer) must be REJECTED — even though plain dispatch_landed false-
    # positives it (the d21h trap). The same line, once it SUBMITS as its own
    # turn (echoed on a prompt-glyph line with OUR running turn's busy marker
    # BELOW it, or the pane fallen idle with the line in the transcript), must be
    # ACCEPTED.
    msg = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other = "● Running the auth refactor…\n✻ Thinking… (12s · esc to interrupt)"

    queued = f"{msg}\n{other}\n❯ \n  ddc@host:~/wt"
    # dispatch_landed alone is fooled (this is exactly why d21h needed the t0 guard)
    assert sable_pane_lib.dispatch_landed(queued, msg) is True
    # but submitted_own_turn is NOT — the busy marker sits above the composer
    assert sable_pane_lib.submitted_own_turn(queued, msg) is False

    # submitted as OUR turn: echo + running-turn busy marker directly below it
    running = f"● auth refactor done\n❯ {msg}\n✻ Thinking… (2s · esc to interrupt)"
    assert sable_pane_lib.submitted_own_turn(running, msg) is True

    # or the pane fell fully idle with the line persisted in the transcript
    border = "─" * 80
    idle_done = f"❯ {msg}\n● reply…\n{border}\n❯\xa0\n{border}\n  ddc@host:~/wt"
    assert sable_pane_lib.submitted_own_turn(idle_done, msg) is True

    # a line NEVER present is never landed
    assert sable_pane_lib.submitted_own_turn(f"{other}\n❯ \n  ddc@host:~/wt", msg) is False


def test_deliver_message_busy_at_t0_then_submits_lands_via_delayed_confirmation_h0jw():
    # THE bead repro's happy path: the pane is BUSY running a DIFFERENT turn at
    # send time; our line queues for a few polls (hoisted above the composer,
    # visible-and-not-in-box — the d21h false-positive shape), then the running
    # turn ENDS and our queued line SUBMITS as its own turn. deliver_message must
    # report LANDED via delayed confirmation — NOT fail closed at t0 and file a
    # redundant noise bead.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other_turn = "● Running the auth refactor…\n✻ Thinking… (12s · esc to interrupt)"
    state = {"typed": False, "polls": 0}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            # t0: someone else's turn is running (busy) — no empty composer.
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"
        state["polls"] += 1
        if state["polls"] < 3:
            # queued behind the other turn: hoisted above the empty composer,
            # the OTHER turn's busy marker still ABOVE it. Looks landed to plain
            # dispatch_landed (d21h) but is only QUEUED.
            return f"{message}\n{other_turn}\n❯ \n  ddc@host:~/wt"
        # the other turn ENDED and our queued line SUBMITTED as its own turn:
        # echoed as a prompt-glyph line with OUR running turn's busy marker below.
        return f"● auth refactor done\n❯ {message}\n✻ Thinking… (2s · esc to interrupt)"

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run, capture=capture,
        sleep=lambda s: None, tries=8, interval=0.01,
    )
    assert landed is True


def test_deliver_message_busy_at_t0_turn_never_ends_times_out_and_fails_h0jw():
    # The other half of the bead spec: a busy pane whose running turn NEVER ends
    # within the poll budget (and our line never becomes its own submitted turn)
    # must still report NOT landed, so sable-msg files the durable fallback. The
    # delayed confirmation degrades to fail-closed on timeout — never worse than
    # d21h.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: urgent"
    other_turn = "● Running the auth refactor…\n✻ Thinking… (99s · esc to interrupt)"
    state = {"typed": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"
        # our line stays queued behind the never-ending turn, forever.
        return f"{message}\n{other_turn}\n❯ \n  ddc@host:~/wt"

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run, capture=capture,
        sleep=lambda s: None, tries=4, interval=0.01,
    )
    assert landed is False


def test_main_busy_delayed_land_files_no_fallback_bead_h0jw(monkeypatch):
    # End-to-end through the REAL main -> deliver_message -> deliver_text ->
    # submitted_own_turn composition: a busy-at-t0 send whose queued line later
    # submits+lands must report rc 0 AND must NOT file a durable fallback bead.
    # This is the exact regression the bead is about — every busy-pane send under
    # d21h permanently cost a noise bead even when the message landed.
    monkeypatch.setenv("SABLE_MSG_POLL_INTERVAL", "0")
    monkeypatch.setenv("SABLE_MSG_SUBMIT_TRIES", "8")
    monkeypatch.setenv("SABLE_MSG_READY_TIMEOUT", "1")
    monkeypatch.setattr(sable_msg, "lookup_pane",
                        lambda role, run=None, socket=None, session=None: "%2")
    # Pin _now() (SABLE-xwy0b) so main()'s own composed_at matches this test's
    # independently built `framed`, which fake_capture echoes back verbatim.
    monkeypatch.setattr(sable_msg, "_now", lambda: 1_700_000_000.0)

    framed = sable_msg.format_message("lincoln", "optimus", "cap in force", 1_700_000_000.0)
    other_turn = "● Running the auth refactor…\n✻ Thinking… (12s · esc to interrupt)"
    state = {"typed": False, "polls": 0}

    class FakeProc:
        returncode = 0

    def fake_run(cmd, **kw):
        if "-l" in cmd:
            state["typed"] = True
        return FakeProc()

    def fake_capture(base, pane):
        if not state["typed"]:
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"       # busy at t0
        state["polls"] += 1
        if state["polls"] < 3:
            return f"{framed}\n{other_turn}\n❯ \n  ddc@host:~/wt"  # queued
        return f"● done\n❯ {framed}\n✻ Thinking… (2s · esc to interrupt)"  # submitted

    monkeypatch.setattr(sable_msg.subprocess, "run", fake_run)
    monkeypatch.setattr(sable_msg, "_capture_pane", fake_capture)
    filed = []
    monkeypatch.setattr(sable_msg, "file_fallback_bead",
                        lambda *a, **k: filed.append(a) or "SABLE-should-not-file")

    rc = sable_msg.main(["optimus", "cap in force", "--from", "lincoln"])
    assert rc == 0
    assert filed == [], "a busy-at-t0 send that eventually lands must not file a noise bead"


# --- queued-composer footer + idempotent retry (SABLE-msxj) -----------------
# Recurrence of the h0jw class AFTER h0jw merged: a busy-at-t0 send that
# ACTUALLY queued (visible in the composer with the real Claude-TUI's 'Press up
# to edit queued messages' footer) was still scored as failure. h0jw's signals
# (submitted_own_turn branches 1-2) assumed a queued line gets hoisted ABOVE the
# composer with the box cleared — this TUI posture instead leaves the line IN
# the composer/box, so neither branch fires and the poll budget timed out on a
# send that had, in fact, already succeeded (SABLE-l8a5: closed false-fail,
# evidence the queued line was live in the pane the whole time). Worse, the
# caller then retried the call, and retyping into a still-busy pane whose
# earlier attempt's line was STILL queued produced a literal duplicate turn.


def test_pane_has_queued_message_true_with_footer_false_without():
    msg = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    with_footer = f"❯ {msg}\n  Press up to edit queued messages\n  ddc@host:~/wt"
    assert sable_pane_lib.pane_has_queued_message(with_footer, msg) is True
    # same text, no footer -> not proof of a queued-delivered send
    no_footer = f"❯ {msg}\n  ddc@host:~/wt"
    assert sable_pane_lib.pane_has_queued_message(no_footer, msg) is False
    # footer present but OUR text absent -> some OTHER queued line, not ours
    other_queued = "❯ some other line\n  Press up to edit queued messages\n  ddc@host:~/wt"
    assert sable_pane_lib.pane_has_queued_message(other_queued, msg) is False


def test_submitted_own_turn_accepts_queued_footer_even_when_box_scan_fails_closed_msxj():
    # THE bead's exact posture: the message sits IN the composer/box (never
    # "leaves" it, so dispatch_landed's box-based branches inside
    # submitted_own_turn fail closed here) but the queued-messages footer is
    # independent proof that it landed as a delivered-queued send.
    msg = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    queued_in_box = f"❯ {msg}\n  Press up to edit queued messages\n  ddc@host:~/wt"
    # proof the box-scan alone would fail closed here (the exact SABLE-l8a5 trap)
    assert sable_pane_lib.dispatch_landed(queued_in_box, msg) is False
    assert sable_pane_lib.submitted_own_turn(queued_in_box, msg) is True


def test_deliver_message_busy_at_t0_queued_footer_confirms_without_waiting_out_budget_msxj():
    # End-to-end through deliver_message: busy at t0, and the FIRST poll after
    # typing already shows the queued footer -> must confirm right away, not
    # exhaust the tries budget the way SABLE-l8a5 did (the running turn here
    # never ends, so ANY confirmation must come from the footer signal alone).
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other_turn = "● Running the auth refactor…\n✻ Thinking… (12s · esc to interrupt)"
    state = {"type_calls": 0}

    def run(cmd):
        if "-l" in cmd:
            state["type_calls"] += 1
        return True

    def capture():
        if state["type_calls"] == 0:
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"          # busy at t0
        # queued in the composer, footer shown, the OTHER turn never ends
        return (f"❯ {message}\n  Press up to edit queued messages\n"
                f"{other_turn}\n  ddc@host:~/wt")

    landed = sable_msg.deliver_message(
        "%47", message, interrupt=False, run=run, capture=capture,
        sleep=lambda s: None, tries=8, interval=0.01,
    )
    assert landed is True
    assert state["type_calls"] == 1, "must type exactly once, not double-queue"


def test_deliver_text_busy_at_t0_skips_retype_when_message_already_pending_msxj():
    # THE double-queue repro: deliver_text is invoked while the pane is BUSY and
    # a PRIOR attempt's line is already sitting queued in the composer (e.g. a
    # caller retrying after an earlier call reported false failure). The retry
    # must recognize the pre-existing text and skip typing it again -- a single
    # queued copy, not two.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other_turn = "● Running the auth refactor…\n✻ Thinking… (12s · esc to interrupt)"
    # t0: busy, AND our line from a prior attempt is already visible, queued.
    pending = f"{message}\n{other_turn}\n❯ \n  ddc@host:~/wt"
    state = {"type_calls": 0, "polls": 0}

    def run(cmd):
        if "-l" in cmd:
            state["type_calls"] += 1
        return True

    def capture():
        state["polls"] += 1
        if state["polls"] < 3:
            return pending
        # the running turn ends and our (already-queued) line submits as its own
        return f"● done\n❯ {message}\n✻ Thinking… (2s · esc to interrupt)"

    landed = sable_pane_lib.deliver_text(
        ["tmux"], "%47", message, message,
        tries=8, interval=0.01, run=run, capture=capture, sleep=lambda s: None,
    )
    assert landed is True
    assert state["type_calls"] == 0, "prior attempt's text already queued -- must not retype"


def test_deliver_text_idle_at_t0_always_types_even_if_snippet_coincidentally_visible_msxj():
    # The idempotent-retry guard is scoped to the BUSY-at-t0 path only (the
    # scenario the bead actually reports). An IDLE pane must always type
    # normally -- this is the common send path and must not regress.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: status?"
    state = {"type_calls": 0}

    def run(cmd):
        if "-l" in cmd:
            state["type_calls"] += 1
        return True

    def capture():
        if state["type_calls"] == 0:
            return "● earlier turn output\n● done\n❯ \n  ddc@host:~/wt"  # idle at t0
        return f"{message}\n✻ Thinking… (2s · esc to interrupt)\n❯ \n  ddc@host:~/wt"

    landed = sable_pane_lib.deliver_text(
        ["tmux"], "%47", message, message,
        tries=4, interval=0.01, run=run, capture=capture, sleep=lambda s: None,
    )
    assert landed is True
    assert state["type_calls"] == 1


# --- busy-at-t0 submit-race self-heal (SABLE-l7uv) --------------------------
# The false-undelivered class the msxj footer path did NOT retire. The original
# repro (SABLE-mgyh) is explicitly "NOT the queued-behind-a-turn state": our line
# sits UN-submitted in the recipient's EDITABLE composer (prompt-glyph line, NO
# 'Press up to edit queued messages' footer). The mechanism: the pane was BUSY at
# t0 (finishing the PRIOR turn — 'Baked for 8s' rendering), so deliver_text took
# the busy leg and sent Enter exactly ONCE; that Enter was absorbed in the
# busy->idle redraw, the prior turn then ended, and our text was left sitting in
# the now-EDITABLE composer. Because the busy leg never resent Enter, the line
# would NEVER auto-submit (it was never a real queued line) and submitted_own_turn
# could never confirm it -> the poll budget timed out -> false 'undelivered' +
# durable fallback bead, while the message sat visibly stuck. The fix: on the busy
# leg, once the pane has fallen IDLE with our snippet still un-submitted in the
# editable box, (re)send Enter to submit it as its own turn.


def test_deliver_text_busy_at_t0_then_idle_with_text_stuck_in_box_resends_enter_l7uv():
    # THE SABLE-l7uv repro, at the helper it lives in. Busy at t0; our Enter is
    # absorbed in the redraw so the first poll shows the pane fallen IDLE with the
    # snippet sitting UN-submitted in the editable composer (no footer, no busy
    # line). submitted_own_turn cannot confirm this (dispatch_landed False on an
    # idle pane == text still in box; no queued-footer). The busy leg must
    # self-heal by RE-SENDING Enter; the stand-in then submits it, and the next
    # poll confirms LANDED. Pre-fix: no Enter is ever resent -> times out -> False.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other_turn = "● Baking the prior turn…\n✻ Thinking… (8s · esc to interrupt)"
    state = {"typed": False, "submitted": False, "enter_after_type": 0}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        elif cmd[-1] == "Enter" and state["typed"]:
            state["enter_after_type"] += 1
            # The FIRST post-type Enter is the absorbed one (busy->idle redraw);
            # the SECOND (the self-heal resend on the idle editable box) submits.
            if state["enter_after_type"] >= 2:
                state["submitted"] = True
        return True

    def capture():
        if not state["typed"]:
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"          # busy at t0
        if not state["submitted"]:
            # prior turn ended; our line sits in the EDITABLE composer, no footer,
            # no busy status -> pane_idle True, dispatch_landed False (still in box)
            return f"❯ {message}\n  ddc@host:~/wt"
        # the self-heal Enter submitted it as its own turn
        return f"● prior done\n❯ {message}\n✻ Thinking… (1s · esc to interrupt)"

    landed = sable_pane_lib.deliver_text(
        ["tmux"], "%5", message, message,
        tries=8, interval=0.01, run=run, capture=capture, sleep=lambda s: None,
    )
    assert landed is True
    assert state["enter_after_type"] >= 2, "the busy leg must resend Enter to submit the stuck line"


def test_deliver_text_busy_at_t0_genuinely_queued_no_selfheal_double_submit_l7uv():
    # The guard against the d21h phantom-confirm regression: a line GENUINELY
    # queued behind a still-running turn (pane stays BUSY, line hoisted above the
    # composer) must NOT trigger the self-heal Enter — pane_idle is False the whole
    # time, so no stray Enter is sent, and when the turn ends the line auto-submits
    # and is confirmed by submitted_own_turn. Exactly one submission, never two.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other_turn = "● Running the merge gate…\n✻ Thinking… (12s · esc to interrupt)"
    state = {"typed": False, "polls": 0, "enter_after_type": 0}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        elif cmd[-1] == "Enter" and state["typed"]:
            state["enter_after_type"] += 1
        return True

    def capture():
        if not state["typed"]:
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"          # busy at t0
        state["polls"] += 1
        if state["polls"] < 3:
            # genuinely queued: hoisted above the composer, the OTHER turn still
            # running (busy) -> pane_idle False, self-heal must NOT fire.
            return f"{message}\n{other_turn}\n❯ \n  ddc@host:~/wt"
        # turn ended, our queued line auto-submitted as its own turn
        return f"● gate done\n❯ {message}\n✻ Thinking… (1s · esc to interrupt)"

    landed = sable_pane_lib.deliver_text(
        ["tmux"], "%21", message, message,
        tries=8, interval=0.01, run=run, capture=capture, sleep=lambda s: None,
    )
    assert landed is True
    assert state["enter_after_type"] == 1, "a genuinely-queued busy line must get NO self-heal Enter"


# --- interrupt idle-wait state machine (SABLE-m6is) -------------------------
# A busy Claude turn STILL shows the empty composer prompt at the bottom, so
# pane_ready fired mid-turn and --interrupt typed into a pane still redrawing the
# interrupted turn — the message was swallowed (two consecutive live sends failed
# all 8 submit attempts). The fix: send Escape ONCE, then defer injection until
# the pane is genuinely IDLE (empty prompt AND no 'esc to interrupt' status).

# A pane mid-turn: composer prompt present (pane_ready True) AND the running
# turn's interrupt affordance visible.
_BUSY_SCREEN = ("● Running the auth refactor…\n"
                "✻ Thinking… (12s · ↓ 1.2k tokens · esc to interrupt)\n"
                "❯ \n  ddc@host:~/wt")
# Same pane after the turn settles: prompt present, no busy status line.
_IDLE_SCREEN = "● earlier turn output\n● done\n❯ \n  ddc@host:~/wt"


def test_pane_busy_true_only_while_turn_running():
    assert sable_msg.pane_busy(_BUSY_SCREEN) is True
    assert sable_msg.pane_busy(_IDLE_SCREEN) is False
    # whitespace/padding in the status line must not defeat the match
    assert sable_msg.pane_busy("│   esc   to   interrupt   │") is True


def test_pane_idle_requires_ready_and_not_busy():
    # the crux: a busy pane is READY (has the empty prompt) but NOT idle
    assert sable_msg.pane_ready(_BUSY_SCREEN) is True
    assert sable_msg.pane_idle(_BUSY_SCREEN) is False
    assert sable_msg.pane_idle(_IDLE_SCREEN) is True
    # a booting pane (no prompt yet) is neither ready nor idle
    assert sable_msg.pane_idle("╭─ Claude Code ─╮\n│ booting… │") is False


def test_interrupt_sends_escape_once_and_defers_injection_until_idle():
    # The pane is busy for the first two readiness polls, then settles to idle,
    # then the typed message lands. --interrupt must (a) send Escape exactly
    # once, (b) NOT type the message while the pane is still busy — injection is
    # deferred until the pane is idle. wait_for_idle consumes 2 busy + 1 idle
    # capture, then deliver_text takes its OWN pre-send idle capture (the
    # SABLE-d21h t0 check) before typing, so injection lands at the 4th capture.
    # Under the old pane_ready wait it would have typed at the FIRST capture
    # (busy panes are 'ready'), so `typed_at_capture[0] > 1` is the regression
    # guard.
    landed_screen = ("⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force\n"
                     "● thinking…\n❯ \n  ddc@host:~/wt")
    screens = iter([_BUSY_SCREEN, _BUSY_SCREEN, _IDLE_SCREEN, _IDLE_SCREEN, landed_screen])
    captures = {"n": 0}
    sent = []
    typed_at_capture = []

    def run(cmd):
        sent.append(cmd)
        if "-l" in cmd:
            typed_at_capture.append(captures["n"])
        return True

    def capture():
        captures["n"] += 1
        return next(screens)

    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    landed = sable_msg.deliver_message(
        "%2", message, interrupt=True, run=run, capture=capture,
        sleep=lambda s: None, ready_timeout=10, interval=0.01, tries=5,
    )
    assert landed is True
    escapes = [c for c in sent if c[-1] == "Escape"]
    assert len(escapes) == 1                       # Escape sent exactly ONCE
    assert typed_at_capture == [4]                 # typed only after the idle polls (t0 check is #4)
    assert typed_at_capture[0] > 1                 # NOT at the first (busy) poll
    # ordering: Escape precedes the first keystroke injection
    assert sent.index(escapes[0]) < next(i for i, c in enumerate(sent) if "-l" in c)


def test_interrupt_never_types_while_pane_stays_busy_then_degrades():
    # A pane that never leaves the busy state (Escape did not settle it in time):
    # wait_for_idle times out, delivery is ATTEMPTED anyway (never worse than the
    # pre-idle-wait behavior) but the message is never confirmed out of the box,
    # so it degrades to a verified-delivery failure. Escape is still sent once and
    # the message text never appears, so no phantom "landed".
    sent = []

    def run(cmd):
        sent.append(cmd)
        return True

    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: urgent"
    landed = sable_msg.deliver_message(
        "%2", message, interrupt=True, run=run,
        capture=lambda: _BUSY_SCREEN,                # never goes idle
        sleep=lambda s: None, ready_timeout=0.05, interval=0.01, tries=3,
    )
    assert landed is False
    assert len([c for c in sent if c[-1] == "Escape"]) == 1


# --- wrapped-composer delivery (SABLE-1umr) ---------------------------------

def test_deliver_message_wrapped_composer_requires_a_real_enter():
    # SABLE-1umr: the wrapped-composer false positive meant deliver_message
    # could report delivered WITHOUT EVER SENDING ENTER (the first Enter used
    # to be sent only after a failed landed-check). Stateful fake: an empty idle
    # composer BEFORE we type (idle_at_send True, SABLE-d21h), then the message
    # sits wrapped in the composer until an Enter arrives, then shows as a
    # submitted turn.
    message = ("⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap all lanes at 4 "
               "workers and hold pushes until chuck drains the merge queue")
    state = {"typed": False, "entered": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        if cmd[-1] == "Enter":
            state["entered"] = True
        return True

    def capture():
        if state["entered"]:
            return f"{message}\n● thinking…\n❯ \n  ddc@host:~/wt"
        if state["typed"]:
            return ("❯ ⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap all lanes at 4\n"
                    "workers and hold pushes until chuck drains the merge queue\n"
                    "  ddc@host:~/wt")
        return "❯ \n  ddc@host:~/wt"                  # idle at t0, empty composer

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run, capture=capture,
        sleep=lambda s: None, tries=4, interval=0.01,
    )
    assert landed is True
    assert state["entered"] is True  # delivered must imply a submitted turn


def test_deliver_message_sends_enter_immediately_not_only_after_failed_poll():
    # Submission must not depend on the verifier failing once: the Enter is
    # part of typing the message, the retry loop only covers dropped Enters.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: status?"
    sent = []

    def run(cmd):
        sent.append(cmd)
        return True

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run,
        capture=lambda: f"{message}\n● thinking…\n❯ \n  ddc@host:~/wt",
        sleep=lambda s: None, tries=3, interval=0.01,
    )
    assert landed is True
    li = next(i for i, c in enumerate(sent) if "-l" in c)
    assert li + 1 < len(sent), "no keystroke followed the typed text"
    assert sent[li + 1][-1] == "Enter"


# --- bead-addressed worker delivery (SABLE-6izz) ----------------------------

def test_parse_worker_bead_tags_matches_only_worker_role():
    out = ("%1 worker market-brief-package-73t4 running\n"
           "%2 optimus \n"
           "%3 worker market-brief-package-6izz running\n")
    assert sable_msg.parse_worker_bead_tags(out) == {
        "market-brief-package-73t4": [("%1", "running")],
        "market-brief-package-6izz": [("%3", "running")],
    }


def test_parse_worker_bead_tags_skips_non_worker_roles():
    # a manager pane happening to carry a stray @sable_bead-shaped 3rd field
    # must never be treated as a bead-addressable pane.
    out = "%1 optimus market-brief-package-73t4 running\n"
    assert sable_msg.parse_worker_bead_tags(out) == {}


def test_parse_worker_bead_tags_preserves_duplicate_bead_tags_qq6r():
    # SABLE-qq6r: a REVISE re-spawn into the same worktree creates a fresh
    # pane before the old one is reaped, so two panes legitimately share one
    # @sable_bead tag. Both must survive parsing (not last-wins) so
    # lookup_worker_by_bead can filter by status.
    out = "%26 worker SABLE-pi5m done\n%37 worker SABLE-pi5m running\n"
    assert sable_msg.parse_worker_bead_tags(out) == {
        "SABLE-pi5m": [("%26", "done"), ("%37", "running")],
    }


def test_lookup_worker_by_bead_found_and_missing():
    fake_out = "%1 worker market-brief-package-73t4\n%2 optimus \n"
    runner = lambda args: fake_out
    assert sable_msg.lookup_worker_by_bead("market-brief-package-73t4", runner) == "%1"
    assert sable_msg.lookup_worker_by_bead("ghost-bead", runner) is None


def test_lookup_worker_by_bead_prefers_running_pane_over_done_duplicate_qq6r():
    # THE bead repro: old done-but-unreaped pane %26 and fresh running pane
    # %37 both tagged SABLE-pi5m. Resolution must pick the LIVE one, not
    # whichever the map happened to keep last.
    fake_out = "%26 worker SABLE-pi5m done\n%37 worker SABLE-pi5m running\n"
    runner = lambda args: fake_out
    assert sable_msg.lookup_worker_by_bead("SABLE-pi5m", runner) == "%37"
    # order-independence: the done pane listed second must not win either.
    fake_out_reordered = "%37 worker SABLE-pi5m running\n%26 worker SABLE-pi5m done\n"
    runner2 = lambda args: fake_out_reordered
    assert sable_msg.lookup_worker_by_bead("SABLE-pi5m", runner2) == "%37"


def test_lookup_worker_by_bead_only_done_pane_raises_hint_qq6r():
    # If ONLY a done pane matches, resolving to it would deliver into a dead
    # composer and (from the caller's perspective) silently report success.
    # Fail loudly instead with a reap hint.
    fake_out = "%26 worker SABLE-pi5m done\n"
    runner = lambda args: fake_out
    with pytest.raises(sable_msg.OnlyDonePane) as exc_info:
        sable_msg.lookup_worker_by_bead("SABLE-pi5m", runner)
    assert exc_info.value.bead_id == "SABLE-pi5m"
    assert exc_info.value.pane_id == "%26"


def test_manager_name_lookup_never_resolves_via_worker_bead_tag_even_when_stale():
    # SABLE-6izz reassigned regression (originally market-brief-package-0h8k):
    # a worker pane's @sable_bead happens to collide with a manager name
    # ("optimus"), and the real optimus manager pane's role tag is gone/stale
    # (e.g. a race during respawn). Manager-name-addressed delivery (the
    # default, no --bead) must NEVER fall through and land on that worker pane.
    stale_listing = "%1 worker optimus\n"  # worker pane whose bead tag == "optimus"
    runner = lambda args: stale_listing
    assert sable_msg.lookup_pane("optimus", runner) is None
    # bead-addressed lookup is a strictly separate path/flag, opted into explicitly
    assert sable_msg.lookup_worker_by_bead("optimus", runner) == "%1"


def test_main_bead_addressed_delivery(monkeypatch, capsys):
    monkeypatch.setattr(sable_msg, "lookup_worker_by_bead",
                        lambda bead, run=None, socket=None, session=None: "%9")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: True)
    rc = sable_msg.main(["market-brief-package-73t4", "hold the tree claim",
                        "--from", "optimus", "--bead"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "delivered" in err


def test_main_bead_addressed_unknown_bead_errors_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(sable_msg, "lookup_worker_by_bead",
                        lambda bead, run=None, socket=None, session=None: None)
    rc = sable_msg.main(["ghost-bead", "hello", "--from", "optimus", "--bead"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "ghost-bead" in err


def test_main_bead_addressed_only_done_pane_errors_with_reap_hint_qq6r(monkeypatch, capsys):
    # SABLE-qq6r: resolving to a done-but-unreaped duplicate must never report
    # "delivered" — it must fail loudly with a reap hint instead.
    def raise_only_done(bead, run=None, socket=None, session=None):
        raise sable_msg.OnlyDonePane(bead, "%26")

    monkeypatch.setattr(sable_msg, "lookup_worker_by_bead", raise_only_done)
    rc = sable_msg.main(["SABLE-pi5m", "hold", "--from", "optimus", "--bead"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "SABLE-pi5m" in err
    assert "done" in err
    assert "reap" in err.lower()


# --- --body-file: caller-shell command-substitution hazard (SABLE-tmbx1) ----
# The inline <body> positional is parsed by the CALLING shell before sable-msg
# ever sees it: a body composed inside a double-quoted shell argument has its
# backticks/$(...) command-substituted THERE, and the substituted command
# actually runs. sable-msg cannot detect this — by the time argv reaches
# main() the damage is already done. --body-file sidesteps the hazard
# entirely by reading the body from a file (or stdin), which no shell ever
# re-parses. These assertions use REAL metacharacter content, not an innocent
# fixture — an implementation that regressed to string-interpolating the body
# through a shell somewhere would fail them.

HAZARDOUS_BODY = "see `hostname` and $(id) for the failing host — do not run this"


def test_parse_args_body_file_flag_alone():
    ns = sable_msg.parse_args(["optimus", "--body-file", "/tmp/whatever"])
    assert ns.body is None
    assert ns.body_file == "/tmp/whatever"


def test_parse_args_body_and_body_file_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        sable_msg.parse_args(["optimus", "inline text", "--body-file", "/tmp/x"])


def test_resolve_body_returns_inline_positional_when_no_body_file():
    ns = sable_msg.parse_args(["optimus", HAZARDOUS_BODY])
    assert sable_msg.resolve_body(ns) == HAZARDOUS_BODY


def test_resolve_body_reads_hazardous_content_from_file_verbatim(tmp_path):
    body_path = tmp_path / "body.txt"
    body_path.write_text(HAZARDOUS_BODY, encoding="utf-8")
    ns = sable_msg.parse_args(["optimus", "--body-file", str(body_path)])
    assert sable_msg.resolve_body(ns) == HAZARDOUS_BODY


def test_resolve_body_reads_hazardous_content_from_stdin_dash():
    import io
    ns = sable_msg.parse_args(["optimus", "--body-file", "-"])
    fake_stdin = io.StringIO(HAZARDOUS_BODY)
    assert sable_msg.resolve_body(ns, stdin=fake_stdin) == HAZARDOUS_BODY


def test_main_body_file_delivers_hazardous_content_unmodified(monkeypatch, tmp_path):
    # NEGATIVE-CONTROL-SHAPED: this is an equality assertion against literal
    # backticks/$(...) — a regression that re-introduced shell interpolation
    # anywhere in this path would corrupt the string and fail it, not pass
    # vacuously the way a metacharacter-free fixture would.
    body_path = tmp_path / "body.txt"
    body_path.write_text(HAZARDOUS_BODY, encoding="utf-8")
    monkeypatch.setattr(sable_msg, "lookup_pane",
                        lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "_now", lambda: 1_700_000_000.0)
    delivered = {}

    def fake_deliver(pane, message, interrupt, **kwargs):
        delivered["message"] = message
        return True

    monkeypatch.setattr(sable_msg, "deliver_message", fake_deliver)
    rc = sable_msg.main(["optimus", "--body-file", str(body_path), "--from", "lincoln"])
    assert rc == 0
    assert "message" in delivered
    assert "`hostname`" in delivered["message"]
    assert "$(id)" in delivered["message"]
    assert delivered["message"] == sable_msg.format_message(
        "lincoln", "optimus", HAZARDOUS_BODY, 1_700_000_000.0)


def test_body_file_content_never_reaches_a_shell(monkeypatch, tmp_path):
    # Assert no subprocess is spawned FROM the content of the body at all —
    # not merely that delivery "looks right". Every subprocess.run call made
    # anywhere below main() is recorded; none may carry the hazardous
    # substring or shell=True.
    import subprocess as subprocess_module

    marker = "$(id)"
    body_path = tmp_path / "body.txt"
    body_path.write_text(f"see `hostname` and {marker} now", encoding="utf-8")

    calls = []
    real_run = subprocess_module.run

    def spying_run(*args, **kwargs):
        calls.append((args, kwargs))
        return real_run(["true"], capture_output=True, text=True)

    monkeypatch.setattr(subprocess_module, "run", spying_run)
    monkeypatch.setattr(sable_msg, "lookup_pane",
                        lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: True)

    rc = sable_msg.main(["optimus", "--body-file", str(body_path), "--from", "lincoln"])
    assert rc == 0
    for args, kwargs in calls:
        flat = " ".join(str(a) for a in args)
        assert marker not in flat
        assert "hostname" not in flat
        assert kwargs.get("shell") is not True


# --- freshness: supersession + expiry (SABLE-xwy0b) -------------------------
#
# A retrying sable-msg can confirm delivery AFTER it has been superseded, and
# neither the sender nor the recipient has any way to know — the retry loop
# that verified delivery requires is itself the mechanism. These cover the
# generation-counter primitives directly, deliver_with_freshness's own
# early-exit/mid-retry behavior against a stubbed deliver_message, and (per
# the bundle brief) the LOAD-BEARING negative controls: an over-matching
# supersession rule that caught a different sender or a different recipient
# would silently swallow unrelated coordination traffic, which is strictly
# worse than the defect this bead fixes.

def test_register_composition_increments_generation_per_pair():
    gen1 = sable_msg.register_composition("lincoln", "optimus", 100.0)
    gen2 = sable_msg.register_composition("lincoln", "optimus", 101.0)
    assert gen2 > gen1
    assert sable_msg.current_generation("lincoln", "optimus") == gen2


def test_current_generation_zero_when_never_registered():
    assert sable_msg.current_generation("nobody", "nowhere") == 0


def test_register_composition_scoped_to_exact_sender_recipient_pair_negative_control():
    # NEGATIVE CONTROL, load-bearing: a different sender to the same
    # recipient, and the same sender to a different recipient, must NOT share
    # a generation counter with the (lincoln, optimus) pair under test.
    sable_msg.register_composition("lincoln", "optimus", 100.0)
    assert sable_msg.current_generation("tarzan", "optimus") == 0
    assert sable_msg.current_generation("lincoln", "chuck") == 0


def test_register_composition_scoped_by_socket_and_session():
    # Two isolated fleets (different socket/session) must never share a
    # counter even for the identical (frm, to) role names (SABLE-e1e3.3-style
    # scoping, mirrored for this bead's state).
    sable_msg.register_composition("lincoln", "optimus", 100.0, socket="s1", session="a")
    assert sable_msg.current_generation("lincoln", "optimus", socket="s2", session="a") == 0
    assert sable_msg.current_generation("lincoln", "optimus", socket="s1", session="b") == 0
    assert sable_msg.current_generation("lincoln", "optimus", socket="s1", session="a") == 1


# --- deliver_with_freshness: delivery outcomes ------------------------------

def test_deliver_with_freshness_delivers_when_fresh(monkeypatch):
    monkeypatch.setattr(sable_msg, "_now", lambda: 1000.0)
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: True)
    outcome = sable_msg.deliver_with_freshness(
        "%2", "msg body", False, "lincoln", "optimus",
        composed_at=1000.0, expiry_seconds=300)
    assert outcome == sable_msg.DELIVERED


def test_deliver_with_freshness_reports_undelivered_when_pane_never_confirms(monkeypatch):
    monkeypatch.setattr(sable_msg, "_now", lambda: 1000.0)
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: False)
    outcome = sable_msg.deliver_with_freshness(
        "%2", "msg body", False, "lincoln", "optimus",
        composed_at=1000.0, expiry_seconds=300)
    assert outcome == sable_msg.UNDELIVERED


# --- deliver_with_freshness: expiry (SABLE-xwy0b) ---------------------------

def test_deliver_with_freshness_expired_message_never_attempts_delivery(monkeypatch):
    # UNIT spec item: an expired message (composed > N seconds ago) is not
    # delivered. Checked upfront, before deliver_message is ever called, so a
    # message that arrives already stale never so much as touches the pane.
    monkeypatch.setattr(sable_msg, "_now", lambda: 2000.0)
    attempted = []
    monkeypatch.setattr(sable_msg, "deliver_message",
                        lambda *a, **k: attempted.append(1) or True)
    outcome = sable_msg.deliver_with_freshness(
        "%2", "msg body", False, "lincoln", "optimus",
        composed_at=1000.0, expiry_seconds=300)  # 1000s old, 300s window
    assert outcome == sable_msg.EXPIRED
    assert attempted == [], "an already-expired message must never attempt delivery"


def test_deliver_with_freshness_delivers_inside_the_expiry_window(monkeypatch):
    # Positive control for the same spec item: inside the window, delivery
    # proceeds exactly as if expiry did not exist.
    monkeypatch.setattr(sable_msg, "_now", lambda: 1200.0)
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: True)
    outcome = sable_msg.deliver_with_freshness(
        "%2", "msg body", False, "lincoln", "optimus",
        composed_at=1000.0, expiry_seconds=300)  # 200s old, inside the window
    assert outcome == sable_msg.DELIVERED


def test_deliver_with_freshness_expiry_disabled_by_zero(monkeypatch):
    monkeypatch.setattr(sable_msg, "_now", lambda: 999999.0)
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: True)
    outcome = sable_msg.deliver_with_freshness(
        "%2", "msg body", False, "lincoln", "optimus",
        composed_at=0.0, expiry_seconds=0)
    assert outcome == sable_msg.DELIVERED


def test_deliver_with_freshness_expires_mid_retry(monkeypatch):
    # The mid-retry leg of expiry: this send is still polling (not yet
    # confirmed) when it ages past the window -- it must abort rather than
    # keep polling toward an eventually-stale confirmation.
    clock = {"t": 1000.0}
    monkeypatch.setattr(sable_msg, "_now", lambda: clock["t"])

    def fake_deliver_message(pane, message, interrupt, socket=None, snippet=None, run=None,
                             capture=None, sleep=None, ready_timeout=0,
                             tries=8, interval=1.0):
        for _ in range(tries):
            clock["t"] += 200.0  # each poll ages the message another 200s
            sleep(interval)
        return False

    monkeypatch.setattr(sable_msg, "deliver_message", fake_deliver_message)
    outcome = sable_msg.deliver_with_freshness(
        "%2", "hold", False, "lincoln", "optimus", composed_at=1000.0,
        expiry_seconds=300, sleep=lambda s: None, tries=8, interval=0)
    assert outcome == sable_msg.EXPIRED


# --- deliver_with_freshness: supersession (SABLE-xwy0b) ---------------------

def test_deliver_with_freshness_aborts_mid_retry_when_superseded(monkeypatch):
    # The core xwy0b repro at the unit level: this send is mid-retry (still
    # unconfirmed) when a LATER message from the SAME sender to the SAME
    # recipient is composed -- it must abort and report SUPERSEDED, never
    # eventually land after its replacement. Simulates deliver_text's own
    # retry loop, which calls sleep() between every poll.
    monkeypatch.setattr(sable_msg, "_now", lambda: 1000.0)
    poll = {"n": 0}

    def fake_deliver_message(pane, message, interrupt, socket=None, snippet=None, run=None,
                             capture=None, sleep=None, ready_timeout=0,
                             tries=8, interval=1.0):
        for _ in range(tries):
            sleep(interval)
            poll["n"] += 1
            if poll["n"] == 2:
                # A later message from the SAME (frm, to) pair is composed
                # while we are still mid-retry.
                sable_msg.register_composition("lincoln", "optimus", 1050.0)
        return False  # never confirmed landed within this stub

    monkeypatch.setattr(sable_msg, "deliver_message", fake_deliver_message)
    outcome = sable_msg.deliver_with_freshness(
        "%2", "hold: push only", False, "lincoln", "optimus", composed_at=1000.0,
        expiry_seconds=300, sleep=lambda s: None, tries=8, interval=0)
    assert outcome == sable_msg.SUPERSEDED


def test_deliver_with_freshness_not_superseded_when_still_freshest(monkeypatch):
    # Positive control: nobody else registers -- this send stays the newest
    # for its (frm, to) pair throughout, so it must deliver normally even
    # though it is still busy-retrying for a while.
    monkeypatch.setattr(sable_msg, "_now", lambda: 1000.0)

    def fake_deliver_message(pane, message, interrupt, socket=None, snippet=None, run=None,
                             capture=None, sleep=None, ready_timeout=0,
                             tries=8, interval=1.0):
        for _ in range(3):
            sleep(interval)
        return True

    monkeypatch.setattr(sable_msg, "deliver_message", fake_deliver_message)
    outcome = sable_msg.deliver_with_freshness(
        "%2", "hold", False, "lincoln", "optimus", composed_at=1000.0,
        expiry_seconds=300, sleep=lambda s: None, tries=8, interval=0)
    assert outcome == sable_msg.DELIVERED


def test_deliver_with_freshness_different_sender_does_not_supersede_negative_control(monkeypatch):
    # NEGATIVE CONTROL, load-bearing: a message from a DIFFERENT sender to the
    # same recipient must NOT suppress this one -- an over-matching
    # supersession rule would silently swallow unrelated coordination
    # traffic, converting a stale-message problem into a dropped-message
    # problem (strictly worse than the defect being fixed).
    monkeypatch.setattr(sable_msg, "_now", lambda: 1000.0)

    def fake_deliver_message(pane, message, interrupt, socket=None, snippet=None, run=None,
                             capture=None, sleep=None, ready_timeout=0,
                             tries=8, interval=1.0):
        sleep(interval)
        sable_msg.register_composition("tarzan", "optimus", 1050.0)  # different sender
        sleep(interval)
        return True

    monkeypatch.setattr(sable_msg, "deliver_message", fake_deliver_message)
    outcome = sable_msg.deliver_with_freshness(
        "%2", "hold", False, "lincoln", "optimus", composed_at=1000.0,
        expiry_seconds=300, sleep=lambda s: None, tries=2, interval=0)
    assert outcome == sable_msg.DELIVERED


def test_deliver_with_freshness_different_recipient_does_not_supersede_negative_control(monkeypatch):
    # NEGATIVE CONTROL, load-bearing: the SAME sender messaging a DIFFERENT
    # recipient must not suppress this send either.
    monkeypatch.setattr(sable_msg, "_now", lambda: 1000.0)

    def fake_deliver_message(pane, message, interrupt, socket=None, snippet=None, run=None,
                             capture=None, sleep=None, ready_timeout=0,
                             tries=8, interval=1.0):
        sleep(interval)
        sable_msg.register_composition("lincoln", "chuck", 1050.0)  # different recipient
        sleep(interval)
        return True

    monkeypatch.setattr(sable_msg, "deliver_message", fake_deliver_message)
    outcome = sable_msg.deliver_with_freshness(
        "%2", "hold", False, "lincoln", "optimus", composed_at=1000.0,
        expiry_seconds=300, sleep=lambda s: None, tries=2, interval=0)
    assert outcome == sable_msg.DELIVERED


# --- main(): freshness outcomes are reported, never silent (SABLE-xwy0b) ----

def test_main_reports_superseded_distinctly_and_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(sable_msg, "lookup_pane",
                        lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "deliver_with_freshness",
                        lambda *a, **k: sable_msg.SUPERSEDED)
    filed = []
    monkeypatch.setattr(sable_msg, "file_fallback_bead",
                        lambda *a, **k: filed.append(a) or "SABLE-should-not-file")
    rc = sable_msg.main(["optimus", "hold", "--from", "lincoln"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "superseded" in err
    assert "optimus" in err
    assert filed == [], "a suppressed-as-stale send must not ALSO file a fallback bead"


def test_main_reports_expired_distinctly_and_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(sable_msg, "lookup_pane",
                        lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "deliver_with_freshness",
                        lambda *a, **k: sable_msg.EXPIRED)
    filed = []
    monkeypatch.setattr(sable_msg, "file_fallback_bead",
                        lambda *a, **k: filed.append(a) or "SABLE-should-not-file")
    rc = sable_msg.main(["optimus", "hold", "--from", "lincoln"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "expired" in err
    assert filed == [], "an expired send must not ALSO file a fallback bead"


def test_main_still_reports_undelivered_and_auto_files_when_outcome_undelivered(monkeypatch, capsys):
    # Regression guard: routing through deliver_with_freshness must not
    # disturb the pre-existing UNDELIVERED -> fallback-bead behavior.
    monkeypatch.setattr(sable_msg, "lookup_pane",
                        lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "deliver_with_freshness",
                        lambda *a, **k: sable_msg.UNDELIVERED)
    calls = []
    monkeypatch.setattr(sable_msg, "file_fallback_bead",
                        lambda frm, to, msg, runner=None: calls.append((frm, to)) or "SABLE-fb99")
    rc = sable_msg.main(["optimus", "cap in force", "--from", "lincoln"])
    assert rc != 0
    assert calls == [("lincoln", "optimus")]
    err = capsys.readouterr().err
    assert "undelivered" in err
    assert "SABLE-fb99" in err


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
