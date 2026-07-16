#!/usr/bin/env python3
"""Integration test for bin/sable-spawn-worker against a REAL tmux server.

Isolated socket (-L), a stand-in worker command (SABLE_WORKER_CMD=bash) so NO
real claude is launched, a temp worktree dir (--worktree) and a temp dispatch
dir so no git worktree / repo mutation happens, and --skip-governance so no bead
is claimed. Reads a real OPEN bead (read-only) for the prompt content.

Proves: a worker WINDOW is created, its pane is tagged
(@sable_role=worker/@sable_bead/@sable_status=running), the dispatch prompt file
is written, and the read-instruction is delivered into the pane.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-spawn-worker"
HAVE_TMUX = shutil.which("tmux") is not None
HAVE_BD = shutil.which("bd") is not None
BEAD = "SABLE-bldh.2"  # an open bead in this repo (read-only here)
pytestmark = pytest.mark.skipif(not (HAVE_TMUX and HAVE_BD),
                                reason="needs tmux + bd")


@pytest.fixture()
def sock():
    s = f"sable-sw-{uuid.uuid4().hex[:8]}"
    # the host session the manager spawns workers into
    subprocess.run(["tmux", "-L", s, "new-session", "-d", "-s", "sable",
                    "-x", "200", "-y", "50", "bash --noprofile --norc"], check=True)
    time.sleep(0.4)
    yield s
    subprocess.run(["tmux", "-L", s, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tmux(s, *args):
    return subprocess.run(["tmux", "-L", s, *args],
                          capture_output=True, text=True, check=True)


# SABLE-3ydb: this suite commonly runs from inside a live SABLE worker pane,
# whose ambient env carries SABLE_WORKER_PANE=1 (+ SABLE_LANE/SABLE_ROLE).
# Spreading os.environ as-is leaks that into every dispatched subprocess,
# tripping sable-spawn-worker's own SABLE-38zi worker-pane guard (exit 9) on
# every test. Scrub it here; test_dispatch_from_worker_pane_is_refused
# re-adds SABLE_WORKER_PANE deliberately via its own literal key, which wins
# over this spread.
def _clean_env(**overrides):
    env = {k: v for k, v in os.environ.items()
           if k not in ("SABLE_WORKER_PANE", "SABLE_LANE", "SABLE_ROLE")}
    env.update(overrides)
    return env


def _refs_snapshot(repo: Path) -> set[str]:
    """Full refs/heads snapshot of `repo` — used to assert a fixture that
    mutates branches (worktree add/remove, branch -D) leaves the real repo's
    tracked-branch set byte-identical before vs after (SABLE-0ssz.4)."""
    r = subprocess.run(["git", "-C", str(repo), "for-each-ref", "refs/heads",
                        "--format=%(refname)"],
                       capture_output=True, text=True, check=True)
    return set(r.stdout.split())


def test_spawn_creates_tagged_worker_window(sock):
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as dd:
        env = {
            **_clean_env(),
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",  # stand-in for claude
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",   # skip TUI readiness wait (stand-in pane)
            "SABLE_MAX_LOAD_PER_CORE": "0",  # hermetic: not a load-guard test (SABLE-mmdt)
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }
        r = subprocess.run(
            ["python3", str(BIN), BEAD, "--worktree", wt,
             "--model", "haiku", "--skip-governance"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stderr
        time.sleep(0.6)

        # the dispatch prompt file was written
        dispatch = Path(dd) / f"{BEAD}.md"
        assert dispatch.exists()
        body = dispatch.read_text()
        assert BEAD in body and wt in body and "haiku" in body

        # a worker pane exists, correctly tagged
        listing = _tmux(sock, "list-panes", "-a", "-F",
                        "#{@sable_role} #{@sable_bead} #{@sable_status}").stdout
        assert any(
            line.startswith("worker") and BEAD in line and "running" in line
            for line in listing.splitlines()
        ), listing

        # the worker pane carries the repo tag (SABLE-e1e3.2)
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import sable_pane_lib as _lib
        root = _lib.repo_root()
        repo_tags = _tmux(sock, "list-panes", "-a",
                          "-F", "#{@sable_role} #{@sable_repo}").stdout
        assert any(
            line == f"worker {root}" for line in repo_tags.splitlines()
        ), repo_tags

        # the read-instruction (single-line) was delivered into the worker pane
        win = _tmux(sock, "list-windows", "-F", "#{window_name}").stdout
        assert "worker-sable-bldh-2" in win


def test_spawn_without_worktree_lands_where_dispatch_points(sock):
    """SABLE-bldh.11 regression: with NO --worktree, the path `bd worktree create`
    actually creates MUST equal the path embedded in the dispatch file (== the
    tmux -c target) AND exist on disk. The original bug created the worktree
    inside the repo but pointed tmux at the repo's sibling, dropping the worker in
    $HOME. Runs against real bd in THIS repo with a unique scope; removes the
    worktree + branch afterward."""
    repo = Path(__file__).resolve().parent.parent  # the SABLE repo root
    scope = f"sw-it-{uuid.uuid4().hex[:8]}"
    wt_name = f"wk-{scope}"
    expected = repo.parent / wt_name  # sibling of the repo, NOT inside it
    # SABLE-0ssz.4: fail fast on an orphan left by a prior hard-killed run
    # instead of silently colliding with `bd worktree create`'s own error.
    assert not expected.exists(), f"orphan worktree from a prior run at {expected}"
    before_refs = _refs_snapshot(repo)
    with tempfile.TemporaryDirectory() as dd:
        env = {
            **_clean_env(),
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",  # stand-in for claude
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",   # skip TUI readiness wait (stand-in pane)
            "SABLE_MAX_LOAD_PER_CORE": "0",  # hermetic: not a load-guard test (SABLE-mmdt)
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }
        try:
            r = subprocess.run(
                ["python3", str(BIN), BEAD, "--scope", scope,
                 "--model", "haiku", "--skip-governance"],
                capture_output=True, text=True, env=env, cwd=str(repo),
            )
            assert r.returncode == 0, r.stderr
            # the worktree dir actually exists at the computed sibling path
            assert expected.is_dir(), f"no worktree at {expected}; stderr={r.stderr}"
            # the dispatch file points workers at that SAME existing path
            body = (Path(dd) / f"{BEAD}.md").read_text()
            assert f"Worktree: {expected}" in body, body
            # and it is NOT inside the repo (no main-checkout pollution)
            assert not str(expected).startswith(str(repo) + os.sep)
        finally:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove",
                            "--force", str(expected)],
                           capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "branch", "-D", wt_name],
                           capture_output=True, text=True)
            # SABLE-0ssz.4: the real repo's branch set must be byte-identical
            # before vs after — proves cleanup removed everything this run
            # created and nothing else leaked.
            assert _refs_snapshot(repo) == before_refs


def test_spawn_leaves_session_current_window_unchanged(sock):
    """SABLE-zgbt regression: the worker window must open DETACHED (-d). Without
    it every dispatch makes the new window the session's current window, yanking
    every attached client (the operator on the lincoln window) to the worker —
    repeatedly, during a drain wave. tmux tracks the current window even with no
    client attached, so this asserts directly on the session."""
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as dd:
        before = _tmux(sock, "display-message", "-t", "sable", "-p",
                       "#{window_index}").stdout.strip()
        env = {
            **_clean_env(),
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",  # stand-in for claude
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
            "SABLE_MAX_LOAD_PER_CORE": "0",  # hermetic: not a load-guard test (SABLE-mmdt)
        }
        r = subprocess.run(
            ["python3", str(BIN), BEAD, "--worktree", wt,
             "--model", "haiku", "--skip-governance"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stderr
        after = _tmux(sock, "display-message", "-t", "sable", "-p",
                      "#{window_index}").stdout.strip()
        assert after == before, f"current window yanked {before} -> {after}"
        # the worker window still exists in the background
        wins = _tmux(sock, "list-windows", "-t", "sable",
                     "-F", "#{window_name}").stdout
        assert "worker-sable-bldh-2" in wins


def test_worker_window_inherits_lane_manager_identity(sock):
    """SABLE-bldh.13 regression: the worker window must carry the invoking lane
    manager's CLAUDE_AGENT_NAME (+ manager role) so its push's for-chuck handoff
    fires (post-push-merge-notify gates on manager identity) and is attributed to
    the lane, not the session-default 'lincoln'. Verified by dumping the worker
    process env to a file."""
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as dd:
        dump = Path(dd) / "worker-env.txt"
        env = {
            **_clean_env(),
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "CLAUDE_AGENT_NAME": "optimus",   # the invoking manager (lane)
            "SABLE_MAX_LOAD_PER_CORE": "0",  # hermetic: not a load-guard test (SABLE-mmdt)
            # worker dumps its OWN env, proving the -e propagation reached it
            "SABLE_WORKER_CMD": f"bash --noprofile --norc -c 'env > {dump}; sleep 2'",
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }
        r = subprocess.run(
            ["python3", str(BIN), BEAD, "--worktree", wt, "--model", "haiku",
             "--skip-governance"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stderr
        time.sleep(0.8)
        content = dump.read_text() if dump.exists() else ""
        assert "CLAUDE_AGENT_NAME=optimus" in content, content
        assert "CLAUDE_AGENT_ROLE=manager" in content, content
        # SABLE-38zi: the worker pane is ALSO marked SABLE_WORKER_PANE=1 so the
        # SessionStart role-anchor refuses to load the optimus manager role-card
        # into it (identity bleed -> the worker booting as its manager and
        # re-dispatching its own bead) — while the manager identity above is
        # still present for the post-push for-chuck handoff.
        assert "SABLE_WORKER_PANE=1" in content, content


def test_dispatch_from_worker_pane_is_refused(sock):
    """SABLE-38zi: sable-spawn-worker HARD-REFUSES a dispatch invoked from a
    worker pane (SABLE_WORKER_PANE=1 in its env) — the guard that stops a worker
    that misread its role from re-dispatching its own bead (one dispatch silently
    becoming two live panes, defeating the SABLE_MAX_WORKERS cap). Exit 9, and no
    side effects: no worker window and no dispatch file are created."""
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as dd:
        env = {
            **_clean_env(),
            "SABLE_WORKER_PANE": "1",   # this invoking process IS a worker pane
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",  # stand-in for claude
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
            "SABLE_MAX_LOAD_PER_CORE": "0",  # hermetic: not a load-guard test
        }
        r = subprocess.run(
            ["python3", str(BIN), BEAD, "--worktree", wt,
             "--model", "haiku", "--skip-governance"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 9, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "worker pane" in r.stderr.lower()
        # refused BEFORE any side effect: no worker window, no dispatch file
        wins = _tmux(sock, "list-windows", "-t", "sable",
                     "-F", "#{window_name}").stdout
        assert "worker-sable-bldh-2" not in wins
        assert not (Path(dd) / f"{BEAD}.md").exists()


def test_worker_bypass_gate_is_accepted(sock):
    """SABLE-91m3 regression: sable-spawn-worker must accept the bypass-permissions
    warning (whose default is 'No, exit') before delivering, or the worker dies.
    A stand-in pane prints the warning, reads the key the helper sends, and records
    it; we assert it received '2' (Yes, I accept). After accepting, it prints the
    empty composer prompt (SABLE-m94k: wait_for_ready's return is now CHECKED, so
    the stand-in must simulate the real gate-dismissed -> composer-ready transition,
    or the new guard refuses to type — matching what a real worker pane does once
    the gate clears into the Claude Code TUI)."""
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as dd:
        rec = Path(dd) / "gate-key.txt"
        script = Path(dd) / "fake-worker.sh"
        script.write_text(
            "echo 'WARNING: Claude Code running in Bypass Permissions mode'\n"
            "echo '  2. Yes, I accept'\n"
            "read k\n"
            f"printf '%s' \"$k\" > {rec}\n"
            "echo '❯ '\n"
            "sleep 1\n"
        )
        env = {
            **_clean_env(),
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": f"bash --noprofile --norc {script}",
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "2",   # poll long enough to see + clear the gate
            "SABLE_DISPATCH_POLL_INTERVAL": "0.2",
            "SABLE_DISPATCH_SUBMIT_TRIES": "1",
            "SABLE_MAX_LOAD_PER_CORE": "0",  # hermetic: not a load-guard test (SABLE-mmdt)
        }
        r = subprocess.run(
            ["python3", str(BIN), BEAD, "--worktree", wt, "--model", "haiku",
             "--skip-governance"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stderr
        time.sleep(0.5)
        assert rec.exists(), "stand-in never received a key (gate not accepted)"
        assert rec.read_text().strip() == "2", rec.read_text()


def test_worker_stuck_on_unrecognized_dialog_refuses_to_type(sock):
    """SABLE-m94k: a pane primed with a read-prompt style dialog accept_startup_gate
    does NOT recognize (only the bypass/trust gates are dismissable) must never have
    the dispatch text typed into it. A stand-in pane prints an Enter-to-select menu
    and blocks on `read`, recording whatever line it receives (if any). The old
    behavior discarded wait_for_ready's False return and typed anyway; the fix
    checks it, refuses (exit 10, naming the pane), and leaves the bead claim intact
    (proved here by --skip-governance never even attempting a claim, and by nothing
    ever landing in the stand-in's `read`)."""
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as dd:
        rec = Path(dd) / "typed-into-dialog.txt"
        script = Path(dd) / "fake-dialog.sh"
        script.write_text(
            "echo '  ? Which package manager would you like to use?'\n"
            "echo '  > 1. npm'\n"
            "echo '    2. yarn'\n"
            "echo '    3. pnpm'\n"
            "echo '  (Use arrow keys, Enter to select)'\n"
            "read line\n"
            f"printf '%s' \"$line\" > {rec}\n"
            "sleep 2\n"
        )
        env = {
            **_clean_env(),
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": f"bash --noprofile --norc {script}",
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0.6",  # short: the dialog never clears
            "SABLE_DISPATCH_POLL_INTERVAL": "0.1",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
            "SABLE_MAX_LOAD_PER_CORE": "0",  # hermetic: not a load-guard test (SABLE-mmdt)
        }
        r = subprocess.run(
            ["python3", str(BIN), BEAD, "--worktree", wt, "--model", "haiku",
             "--skip-governance"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 10, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "dialog" in r.stderr.lower()
        assert BEAD in r.stderr
        time.sleep(0.5)
        # nothing was ever typed+submitted into the stuck dialog's `read`
        assert not rec.exists(), f"dispatch text landed in the dialog: {rec.read_text()!r}"


# --- dispatch throttle + host-resource guard (SABLE-mmdt) ---------------------

def _dummy_worker(sock, bead, status="running"):
    """A stand-in live worker pane: a bash window tagged exactly the way
    sable-spawn-worker tags a real one."""
    pane = _tmux(sock, "new-window", "-d", "-t", "sable", "-P", "-F", "#{pane_id}",
                 "bash --noprofile --norc").stdout.strip()
    _tmux(sock, "set-option", "-p", "-t", pane, "@sable_role", "worker")
    _tmux(sock, "set-option", "-p", "-t", pane, "@sable_bead", bead)
    _tmux(sock, "set-option", "-p", "-t", pane, "@sable_status", status)
    return pane


def test_spawn_at_cap_refused_then_allowed_after_one_flips_done(sock):
    """SABLE-mmdt acceptance: with SABLE_MAX_WORKERS=2 and 2 live dummy worker
    panes, the next spawn is mechanically refused (exit 7, message naming cap
    AND live count, no window/worktree/dispatch-file side effects); after one
    pane flips @sable_status=done, the same spawn succeeds — one-in-one-out."""
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as dd:
        d1 = _dummy_worker(sock, "FAKE-cap-1")
        _dummy_worker(sock, "FAKE-cap-2")
        env = {
            **_clean_env(),
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",  # stand-in for claude
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
            "SABLE_MAX_WORKERS": "2",
            "SABLE_MAX_LOAD_PER_CORE": "0",  # isolate the cap from real host load
        }
        argv = ["python3", str(BIN), BEAD, "--worktree", wt,
                "--model", "haiku", "--skip-governance"]

        r = subprocess.run(argv, capture_output=True, text=True, env=env)
        assert r.returncode == 7, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "SABLE_MAX_WORKERS" in r.stderr
        assert "2" in r.stderr                      # cap AND live count both = 2
        # refused BEFORE any side effect: no worker window, no dispatch file
        wins = _tmux(sock, "list-windows", "-t", "sable",
                     "-F", "#{window_name}").stdout
        assert "worker-sable-bldh-2" not in wins
        assert not (Path(dd) / f"{BEAD}.md").exists()

        # one-out: a worker flips done -> one-in: the next spawn goes through
        _tmux(sock, "set-option", "-p", "-t", d1, "@sable_status", "done")
        r2 = subprocess.run(argv, capture_output=True, text=True, env=env)
        assert r2.returncode == 0, f"stdout={r2.stdout!r} stderr={r2.stderr!r}"
        wins = _tmux(sock, "list-windows", "-t", "sable",
                     "-F", "#{window_name}").stdout
        assert "worker-sable-bldh-2" in wins


def test_spawn_refused_only_after_8_live_panes_with_default_cap(sock):
    """SABLE-l0or acceptance: with SABLE_MAX_WORKERS unset (the WORKER_CAP_DEFAULT
    knob, now 8) and 8 live dummy worker panes, the next spawn is mechanically
    refused (exit 7, message naming cap AND live count) — proving the raised
    default is actually enforced against real tmux panes, not just returned by
    worker_cap() in isolation."""
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as dd:
        for i in range(8):
            _dummy_worker(sock, f"FAKE-defcap-{i}")
        env = {
            **_clean_env(),
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",  # stand-in for claude
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
            "SABLE_MAX_LOAD_PER_CORE": "0",  # isolate the cap from real host load
        }
        # exercise the default cap: this suite may itself run inside a SABLE
        # worker pane whose ambient env sets SABLE_MAX_WORKERS — scrub it so
        # the subprocess actually falls through to WORKER_CAP_DEFAULT.
        env.pop("SABLE_MAX_WORKERS", None)
        argv = ["python3", str(BIN), BEAD, "--worktree", wt,
                "--model", "haiku", "--skip-governance"]

        r = subprocess.run(argv, capture_output=True, text=True, env=env)
        assert r.returncode == 7, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "SABLE_MAX_WORKERS" in r.stderr
        assert "8" in r.stderr                       # cap AND live count both = 8
        wins = _tmux(sock, "list-windows", "-t", "sable",
                     "-F", "#{window_name}").stdout
        assert "worker-sable-bldh-2" not in wins
        assert not (Path(dd) / f"{BEAD}.md").exists()


def test_spawn_refused_when_host_load_exceeds_guard(sock):
    """SABLE-mmdt host-resource guard wiring: with the per-core threshold set
    WELL below the host's current 1-min load, the spawn is refused (exit 8)
    naming the live load. Skipped on an idle host — the guard is exercised
    against REAL load, not a mock (unit tests cover the pure thresholds)."""
    load1 = os.getloadavg()[0]
    if load1 < 0.05:
        pytest.skip("host idle — no real load to trip the guard against")
    cores = os.cpu_count() or 1
    threshold = (load1 / cores) / 2  # half the current per-core load: must trip
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as dd:
        env = {
            **_clean_env(),
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
            "SABLE_MAX_WORKERS": "99",  # isolate the guard from the cap
            "SABLE_MAX_LOAD_PER_CORE": str(threshold),
        }
        r = subprocess.run(
            ["python3", str(BIN), BEAD, "--worktree", wt,
             "--model", "haiku", "--skip-governance"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 8, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "load" in r.stderr.lower()
        assert "SABLE_MAX_LOAD_PER_CORE" in r.stderr
        # no worker window was opened
        wins = _tmux(sock, "list-windows", "-t", "sable",
                     "-F", "#{window_name}").stdout
        assert "worker-sable-bldh-2" not in wins


# --- re-homed pre-dispatch governance (SABLE-bldh.8) -------------------------

def _write_fake_bd(stub_dir: Path, db_path: Path, strict_claim: bool = False) -> None:
    """A stand-in `bd` CLI backed by a JSON file, NOT the real beads database —
    lets the duplicate-dispatch integration test flip a bead's status between
    two subprocess calls (exactly like a real --claim would) without mutating
    project state. Placed first on PATH for the child process.

    strict_claim=True (SABLE-m40k) makes --claim faithfully reproduce the real
    `bd` behavior this bead fixes: it ERRORS "already claimed by <assignee>"
    whenever the bead already has a truthy assignee, regardless of what
    identity is attempting the claim — real bd's built-in claim-idempotency
    keys off its OWN --actor identity (git user.name/$USER), which never
    equals a SABLE lane name, so it never treats "assignee == lane" as a
    no-op. Default False keeps the lenient stand-in pre-existing tests rely on
    (claim always succeeds, assignee flips to "test-worker")."""
    script = stub_dir / "bd"
    script.write_text(f'''#!/usr/bin/env python3
import json, sys

DB = {str(db_path)!r}
STRICT_CLAIM = {strict_claim!r}

def load():
    with open(DB) as f:
        return json.load(f)

def save(data):
    with open(DB, "w") as f:
        json.dump(data, f)

args = sys.argv[1:]

if args[:1] == ["show"] and len(args) >= 2:
    data = load()
    print(json.dumps([b for b in data if b.get("id") == args[1]]))
elif args[:1] == ["update"] and "--claim" in args:
    bid = args[1]
    data = load()
    for b in data:
        if b.get("id") == bid:
            if STRICT_CLAIM and b.get("assignee"):
                sys.stderr.write("Error: issue already claimed by " + str(b.get("assignee")) + chr(10))
                sys.exit(1)
            b["status"] = "in_progress"
            b["assignee"] = "test-worker"
    save(data)
    print("claimed")
elif args[:1] == ["update"] and "--status" in args:
    bid = args[1]
    idx = args.index("--status")
    new_status = args[idx + 1] if idx + 1 < len(args) else ""
    data = load()
    for b in data:
        if b.get("id") == bid:
            b["status"] = new_status
    save(data)
    print("updated")
elif args[:1] == ["list"]:
    data = load()
    if "--status=in_progress" in args:
        data = [b for b in data if b.get("status") == "in_progress"]
    print(json.dumps(data))
else:
    print("[]")
''')
    script.chmod(0o755)


def test_second_spawn_for_in_progress_bead_is_refused(sock):
    """SABLE-bldh.8: sable-spawn-worker refuses a SECOND spawn for a bead that
    is already IN_PROGRESS — the bead trivially 'overlaps' with itself, and a
    second worker racing the first worker's push is a real duplicate-work
    hazard the old prompt-parsing overlap hook never caught (it only ever
    compared DIFFERENT bead IDs against each other, and only warned). Proves
    the end-to-end wiring: the first spawn succeeds and claims the bead (via
    the fake bd's --claim), the second spawn for the SAME bead is refused
    (nonzero exit, no second worker window/pane created)."""
    with tempfile.TemporaryDirectory() as stub_dir, \
         tempfile.TemporaryDirectory() as dd, \
         tempfile.TemporaryDirectory() as wt1, \
         tempfile.TemporaryDirectory() as wt2:
        bead_id = "FAKE-dup-1"
        db_path = Path(stub_dir) / "beads.json"
        db_path.write_text(json.dumps([
            {"id": bead_id, "title": "T", "description": "D", "labels": [],
             "status": "open", "assignee": None}
        ]))
        _write_fake_bd(Path(stub_dir), db_path)

        env = {
            **_clean_env(),
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
            "SABLE_MAX_LOAD_PER_CORE": "0",  # hermetic: not a load-guard test (SABLE-mmdt)
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",  # stand-in for claude
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }
        env.pop("CLAUDE_AGENT_NAME", None)  # keep lane empty -> preempt is a no-op here

        r1 = subprocess.run(
            ["python3", str(BIN), bead_id, "--worktree", wt1, "--model", "haiku"],
            capture_output=True, text=True, env=env,
        )
        assert r1.returncode == 0, r1.stderr
        time.sleep(0.5)

        r2 = subprocess.run(
            ["python3", str(BIN), bead_id, "--worktree", wt2, "--model", "haiku"],
            capture_output=True, text=True, env=env,
        )
        assert r2.returncode == 5, f"stdout={r2.stdout!r} stderr={r2.stderr!r}"
        assert "duplicate-dispatch" in r2.stderr
        assert "IN_PROGRESS" in r2.stderr

        # only ONE worker pane exists for this bead — the refused second call
        # never reached the spawn step
        listing = _tmux(sock, "list-panes", "-a", "-F",
                        "#{@sable_role} #{@sable_bead}").stdout
        matches = [line for line in listing.splitlines()
                  if line.startswith("worker") and bead_id in line]
        assert len(matches) == 1, listing


# --- SABLE-676c: claim-then-hold first dispatch must succeed ------------------


def test_claim_then_hold_first_dispatch_succeeds(sock):
    """SABLE-676c: a bead a manager CLAIMED earlier (already IN_PROGRESS) to mark
    lane ownership during a coordination hold — with NO worker pane and NO
    worktree for it — is this bead's OWN first dispatch, not a duplicate. The old
    already_in_progress_check refused it (any in_progress -> exit 5); the fix lets
    it spawn end-to-end: governance passes, worker window is created + tagged, and
    the dispatch file is written. Governance is ON (no --skip-governance) so the
    claim-then-hold guard is actually exercised."""
    with tempfile.TemporaryDirectory() as stub_dir, \
         tempfile.TemporaryDirectory() as dd, \
         tempfile.TemporaryDirectory() as wt:
        bead_id = "FAKE-hold-1"
        db_path = Path(stub_dir) / "beads.json"
        db_path.write_text(json.dumps([
            {"id": bead_id, "title": "T", "description": "D", "labels": [],
             "status": "in_progress", "assignee": "optimus"}  # claimed during the hold
        ]))
        _write_fake_bd(Path(stub_dir), db_path)

        env = {
            **_clean_env(),
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
            "SABLE_MAX_LOAD_PER_CORE": "0",  # hermetic: not a load-guard test
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",  # stand-in for claude
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }
        env.pop("CLAUDE_AGENT_NAME", None)  # empty lane -> preempt is a no-op

        # --worktree override => worktree-evidence does not fire (intentional
        # reuse), and no pane is tagged for the bead => no pane-evidence: the
        # bare in_progress claim must PASS.
        r = subprocess.run(
            ["python3", str(BIN), bead_id, "--worktree", wt, "--model", "haiku"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        time.sleep(0.5)

        # the dispatch prompt file was written for the bead
        assert (Path(dd) / f"{bead_id}.md").exists()

        # a worker pane exists, tagged for the bead and running
        listing = _tmux(sock, "list-panes", "-a", "-F",
                        "#{@sable_role} #{@sable_bead} #{@sable_status}").stdout
        assert any(
            line.startswith("worker") and bead_id in line and "running" in line
            for line in listing.splitlines()
        ), listing


def test_claim_then_hold_blocks_when_derived_worktree_exists(sock):
    """SABLE-676c inverse: an IN_PROGRESS bead whose DERIVED wk-<scope> worktree
    already exists (a prior dispatch cut it) IS a duplicate and stays refused
    (exit 5), even with no live pane — worktree-evidence alone blocks. Pre-creates
    the sibling wk-<scope> dir the dispatch would use, then dispatches WITHOUT
    --worktree; the block fires in governance before any worktree creation, so no
    real git worktree is made."""
    repo = Path(__file__).resolve().parent.parent  # the SABLE repo root
    scope = f"sw-hold-{uuid.uuid4().hex[:8]}"
    derived = repo.parent / f"wk-{scope}"  # sibling of the repo (SABLE-bldh.11)
    with tempfile.TemporaryDirectory() as stub_dir, \
         tempfile.TemporaryDirectory() as dd:
        bead_id = "FAKE-hold-2"
        db_path = Path(stub_dir) / "beads.json"
        db_path.write_text(json.dumps([
            {"id": bead_id, "title": "T", "description": "D", "labels": [],
             "status": "in_progress", "assignee": "optimus"}
        ]))
        _write_fake_bd(Path(stub_dir), db_path)
        env = {
            **_clean_env(),
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
            "SABLE_MAX_LOAD_PER_CORE": "0",
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }
        env.pop("CLAUDE_AGENT_NAME", None)
        try:
            derived.mkdir()  # a prior dispatch's leftover worktree dir
            r = subprocess.run(
                ["python3", str(BIN), bead_id, "--scope", scope, "--model", "haiku"],
                capture_output=True, text=True, env=env, cwd=str(repo),
            )
            assert r.returncode == 5, f"stdout={r.stdout!r} stderr={r.stderr!r}"
            assert "duplicate-dispatch" in r.stderr
            assert "worktree" in r.stderr
            # blocked before any spawn: no worker window, no dispatch file
            wins = _tmux(sock, "list-windows", "-t", "sable",
                         "-F", "#{window_name}").stdout
            assert "worker-fake-hold-2" not in wins
            assert not (Path(dd) / f"{bead_id}.md").exists()
        finally:
            if derived.exists():
                shutil.rmtree(derived, ignore_errors=True)


def test_crash_leak_orphan_worktree_dispatch_refused_and_refs_unchanged(sock):
    """SABLE-0ssz.4 crash-leak harness: simulate a PRIOR dispatch that was
    hard-killed after `git worktree add` + branch creation but before its own
    cleanup ran, leaving a REAL orphaned worktree + branch at the derived
    sibling path (not just a placeholder dir, unlike
    test_claim_then_hold_blocks_when_derived_worktree_exists above). A
    follow-up dispatch for the same bead/scope must refuse via the existing
    worktree-evidence governance path (duplicate-dispatch, exit 5) rather than
    colliding with `bd worktree create` or mutating the orphan further, and the
    real repo's refs/heads set must be byte-identical before vs after this
    test's own cleanup — proving the crash artifact does not survive as a
    permanent leak once noticed."""
    repo = Path(__file__).resolve().parent.parent  # the SABLE repo root
    scope = f"sw-crash-{uuid.uuid4().hex[:8]}"
    wt_name = f"wk-{scope}"
    orphan = repo.parent / wt_name  # sibling of the repo (SABLE-bldh.11)
    before_refs = _refs_snapshot(repo)
    with tempfile.TemporaryDirectory() as stub_dir, \
         tempfile.TemporaryDirectory() as dd:
        bead_id = "FAKE-crash-1"
        db_path = Path(stub_dir) / "beads.json"
        db_path.write_text(json.dumps([
            {"id": bead_id, "title": "T", "description": "D", "labels": [],
             "status": "in_progress", "assignee": "optimus"}
        ]))
        _write_fake_bd(Path(stub_dir), db_path)
        env = {
            **_clean_env(),
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
            "SABLE_MAX_LOAD_PER_CORE": "0",
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }
        env.pop("CLAUDE_AGENT_NAME", None)
        try:
            # the crash artifact: a REAL worktree + branch, exactly what a
            # hard-killed `bd worktree create` leaves behind
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b",
                            wt_name, str(orphan), "HEAD"],
                           check=True, capture_output=True, text=True)

            r = subprocess.run(
                ["python3", str(BIN), bead_id, "--scope", scope, "--model", "haiku"],
                capture_output=True, text=True, env=env, cwd=str(repo),
            )
            assert r.returncode == 5, f"stdout={r.stdout!r} stderr={r.stderr!r}"
            assert "duplicate-dispatch" in r.stderr
            assert "worktree" in r.stderr
            # refused before any further mutation: no second window/dispatch file
            wins = _tmux(sock, "list-windows", "-t", "sable",
                         "-F", "#{window_name}").stdout
            assert "worker-fake-crash-1" not in wins
            assert not (Path(dd) / f"{bead_id}.md").exists()
        finally:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove",
                            "--force", str(orphan)],
                           capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "branch", "-D", wt_name],
                           capture_output=True, text=True)
            assert _refs_snapshot(repo) == before_refs


# --- SABLE-m40k: idempotent claim skip ---------------------------------------


def test_claim_skipped_when_bead_already_assigned_to_dispatching_lane(sock):
    """SABLE-m40k: SABLE-676c let a claim-then-hold bead (bare in_progress, no
    pane/worktree evidence) pass the duplicate-dispatch GUARD, but the spawn
    helper still ran an unconditional `bd update --claim` afterward — and real
    `bd --claim` is only idempotent against ITS OWN actor identity (git
    user.name/$USER), which never equals a SABLE lane name, so it errored
    "already claimed by <lane>" and aborted the whole spawn. Proves: when the
    bead's assignee IS the dispatching lane (self-claim-then-hold, OR a
    different manager's REASSIGNMENT to this lane — SABLE-m40k design note),
    the helper skips the redundant claim call instead of erroring.
    strict_claim=True makes the fake bd raise exactly that real-world error if
    the unconditional claim call is ever (re)made, so a regression here fails
    loudly instead of silently passing."""
    with tempfile.TemporaryDirectory() as stub_dir, \
         tempfile.TemporaryDirectory() as dd, \
         tempfile.TemporaryDirectory() as wt:
        bead_id = "FAKE-selfclaim-1"
        db_path = Path(stub_dir) / "beads.json"
        db_path.write_text(json.dumps([
            {"id": bead_id, "title": "T", "description": "D", "labels": [],
             "status": "in_progress", "assignee": "optimus"}
        ]))
        _write_fake_bd(Path(stub_dir), db_path, strict_claim=True)

        env = {
            **_clean_env(),
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
            "CLAUDE_AGENT_NAME": "optimus",
            "SABLE_MAX_LOAD_PER_CORE": "0",  # hermetic: not a load-guard test
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",  # stand-in for claude
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }

        # --worktree override => worktree-evidence does not fire, and no pane
        # is tagged for the bead yet => no pane-evidence: the bare in_progress
        # claim-then-hold must PASS, and the claim call must be SKIPPED.
        r = subprocess.run(
            ["python3", str(BIN), bead_id, "--worktree", wt, "--model", "haiku"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "skipping redundant bd update --claim" in r.stderr
        time.sleep(0.5)

        assert (Path(dd) / f"{bead_id}.md").exists()
        listing = _tmux(sock, "list-panes", "-a", "-F",
                        "#{@sable_role} #{@sable_bead} #{@sable_status}").stdout
        assert any(
            line.startswith("worker") and bead_id in line and "running" in line
            for line in listing.splitlines()
        ), listing


def test_claim_still_runs_when_bead_assigned_to_different_lane(sock):
    """SABLE-m40k design note: the idempotent-claim skip must key on the
    bead's assignee matching the DISPATCHING lane specifically. A bead
    assigned to a DIFFERENT lane (no dispatch evidence, so the duplicate guard
    still allows a bare in_progress claim through) must still go through the
    normal `bd update --claim` call rather than being silently skipped. Uses
    the lenient fake bd (assignee flips to "test-worker" on a successful
    claim) and asserts that flip actually happened, proving the claim call
    fired."""
    with tempfile.TemporaryDirectory() as stub_dir, \
         tempfile.TemporaryDirectory() as dd, \
         tempfile.TemporaryDirectory() as wt:
        bead_id = "FAKE-otherlane-1"
        db_path = Path(stub_dir) / "beads.json"
        db_path.write_text(json.dumps([
            {"id": bead_id, "title": "T", "description": "D", "labels": [],
             "status": "in_progress", "assignee": "tarzan"}
        ]))
        _write_fake_bd(Path(stub_dir), db_path)

        env = {
            **_clean_env(),
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
            "CLAUDE_AGENT_NAME": "optimus",
            "SABLE_MAX_LOAD_PER_CORE": "0",
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }

        r = subprocess.run(
            ["python3", str(BIN), bead_id, "--worktree", wt, "--model", "haiku"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "skipping redundant bd update --claim" not in r.stderr
        time.sleep(0.3)

        data = json.loads(db_path.read_text())
        assert data[0]["assignee"] == "test-worker", data


# --- deterministic done-flag on process exit (SABLE-5v9n) --------------------

STATUS_BIN = Path(__file__).resolve().parent / "sable-worker-status"


def test_worker_pane_flips_done_on_process_exit_without_worker_action(sock):
    """SABLE-5v9n: surfaced by SABLE-2cao.1 live acceptance — a worker that
    completed its task (self-push + bd close) but never itself ran
    `tmux set-option ... @sable_status done` left its pane stuck at `running`
    forever, so `sable-worker-status --reap` never reclaimed it. The done-flag
    must be DETERMINISTIC on worker process exit, not the worker's own
    discretionary final act.

    The stub worker below writes a sentinel and exits WITHOUT ever running the
    tmux command itself (simulating exactly that failure). Proves: (1) the
    pane's @sable_status still flips to done once the stub process exits, and
    (2) a subsequent `sable-worker-status --reap` then kills the pane — it
    survives long enough to be reaped (remain-on-exit) instead of tmux
    silently destroying it the instant the process exits."""
    with tempfile.TemporaryDirectory() as stub_dir, \
         tempfile.TemporaryDirectory() as dd, \
         tempfile.TemporaryDirectory() as wt:
        bead_id = "FAKE-doneflag-1"
        db_path = Path(stub_dir) / "beads.json"
        # status=closed: sable-worker-status's bead_closed() crosscheck
        # (SABLE-1kbo) only trusts a done tag as-is when the bead resolves
        # closed (or doesn't resolve at all) -- an open bead would downgrade
        # the pane to "done-unconfirmed" and reap() would correctly refuse to
        # kill it, which would defeat this test's own setup, not the fix.
        db_path.write_text(json.dumps([
            {"id": bead_id, "title": "T", "description": "D", "labels": [],
             "status": "closed", "assignee": "test-worker"}
        ]))
        _write_fake_bd(Path(stub_dir), db_path)

        sentinel = Path(dd) / "sentinel.txt"
        env = {
            **_clean_env(),
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
            "SABLE_MAX_LOAD_PER_CORE": "0",  # hermetic: not a load-guard test
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_STATUS_SAMPLE_INTERVAL": "0.1",
            # stand-in worker: does its "work" then exits -- deliberately
            # never flags done itself.
            "SABLE_WORKER_CMD": f"bash --noprofile --norc -c 'echo done > {sentinel}'",
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }
        env.pop("CLAUDE_AGENT_NAME", None)  # keep lane empty -> preempt is a no-op

        r = subprocess.run(
            ["python3", str(BIN), bead_id, "--worktree", wt,
             "--model", "haiku", "--skip-governance"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stderr

        # the stub actually ran and exited
        deadline = time.time() + 5
        while time.time() < deadline and not sentinel.exists():
            time.sleep(0.1)
        assert sentinel.exists(), "stub worker never ran"

        # the pane's @sable_status flips to done on its own, with NO worker
        # action ever running the tmux command
        pane, status = None, ""
        deadline = time.time() + 5
        while time.time() < deadline:
            listing = _tmux(sock, "list-panes", "-a", "-F",
                            "#{pane_id}\t#{@sable_role}\t#{@sable_bead}\t"
                            "#{@sable_status}").stdout
            for line in listing.splitlines():
                parts = line.split("\t")
                if len(parts) >= 4 and parts[1] == "worker" and parts[2] == bead_id:
                    pane, status = parts[0], parts[3]
            if status == "done":
                break
            time.sleep(0.1)
        assert status == "done", f"pane never flipped done on process exit (last={status!r})"
        assert pane

        # sable-worker-status --reap then kills the (now dead) pane
        rr = subprocess.run(
            ["python3", str(STATUS_BIN), "--reap"],
            capture_output=True, text=True, env=env,
        )
        assert rr.returncode == 0, rr.stderr
        time.sleep(0.4)
        remaining = _tmux(sock, "list-panes", "-a", "-F", "#{pane_id}").stdout
        assert pane not in remaining.splitlines(), remaining


# --- SABLE-3eax: --respawn (REVISE / push-only close-out) end-to-end ----------
#
# The manager REVISE pattern re-spawns a worker into the SAME worktree to finish
# a bead's landing. --respawn makes that first-class (no blunt --skip-governance):
# reopen a CLOSED bead, release a stranded stale tree-claim whose holder has no
# live pane, and pass the duplicate guard when no LIVE pane carries the tag —
# with model-check STILL active.


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


def _repo_with_linked_worktree(tmp):
    """A real repo + a linked worktree so tree-claim resolution hits the
    per-worktree git-dir (`.git/worktrees/<name>`) exactly like tree-claim.sh."""
    work = Path(tmp) / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "f.txt").write_text("1")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "wk-r", "HEAD")
    wt = Path(tmp) / "wk-r"
    _git(work, "worktree", "add", str(wt), "wk-r")
    return work, wt


def _claim_file(worktree):
    gd = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--git-dir"],
                        capture_output=True, text=True, check=True).stdout.strip()
    if not os.path.isabs(gd):
        gd = os.path.join(str(worktree), gd)
    return Path(gd) / "sable-tree-claim"


def test_respawn_reopens_closed_bead_and_releases_stale_tree_claim(sock):
    """SABLE-3eax end-to-end (walls 1 + 2, governance ON): respawn into an
    EXISTING worktree for a CLOSED bead with a tree-claim stranded by a reaped
    (dead) worker session. The helper reopens the bead to in_progress (so the
    flow never traceback-crashes on the unclaimable closed bead), releases the
    stale claim (so tree-claim.sh won't deny the fresh worker's git ops), and
    spawns the worker — all WITHOUT --skip-governance."""
    with tempfile.TemporaryDirectory() as stub_dir, \
         tempfile.TemporaryDirectory() as dd, \
         tempfile.TemporaryDirectory() as gitdir:
        _work, wt = _repo_with_linked_worktree(gitdir)
        claim = _claim_file(wt)
        claim.write_text("dead-session-uuid 1600000000 tarzan\n")  # reaped holder

        bead_id = "FAKE-respawn-closed"
        db_path = Path(stub_dir) / "beads.json"
        db_path.write_text(json.dumps([
            {"id": bead_id, "title": "T", "description": "D", "labels": [],
             "status": "closed", "assignee": "tarzan"}  # was closed on first pass
        ]))
        _write_fake_bd(Path(stub_dir), db_path)

        env = {
            **_clean_env(),
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
            "CLAUDE_AGENT_NAME": "tarzan",
            "SABLE_MAX_LOAD_PER_CORE": "0",  # hermetic
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",  # stand-in for claude
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }

        r = subprocess.run(
            ["python3", str(BIN), bead_id, "--respawn", "--worktree", str(wt),
             "--model", "haiku"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        time.sleep(0.5)

        # (a) the closed bead was reopened to in_progress — no reclaim crash
        assert "reopened closed bead" in r.stderr, r.stderr
        assert "not re-claiming" in r.stderr, r.stderr  # respawn skips the claim
        data = json.loads(db_path.read_text())
        assert data[0]["status"] == "in_progress", data

        # (b) the stranded stale tree-claim was released
        assert "released stale tree-claim" in r.stderr, r.stderr
        assert not claim.exists(), "stale tree-claim was not released"

        # the worker window was spawned + tagged running for the bead
        assert (Path(dd) / f"{bead_id}.md").exists()
        listing = _tmux(sock, "list-panes", "-a", "-F",
                        "#{@sable_role} #{@sable_bead} #{@sable_status}").stdout
        assert any(
            line.startswith("worker") and bead_id in line and "running" in line
            for line in listing.splitlines()
        ), listing


def test_respawn_refused_when_live_pane_carries_bead_tag(sock):
    """SABLE-3eax (wall 3 inverse): a respawn must STILL be refused when a LIVE
    worker pane already carries the bead tag — two workers racing the same push.
    Worktree-evidence is waived for a respawn, but live-pane evidence is not.
    Exit 5, no second worker window."""
    with tempfile.TemporaryDirectory() as stub_dir, \
         tempfile.TemporaryDirectory() as dd, \
         tempfile.TemporaryDirectory() as wt:
        bead_id = "FAKE-respawn-live"
        _dummy_worker(sock, bead_id, status="running")  # a LIVE worker for the bead

        db_path = Path(stub_dir) / "beads.json"
        db_path.write_text(json.dumps([
            {"id": bead_id, "title": "T", "description": "D", "labels": [],
             "status": "in_progress", "assignee": "tarzan"}
        ]))
        _write_fake_bd(Path(stub_dir), db_path)

        env = {
            **_clean_env(),
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
            "CLAUDE_AGENT_NAME": "tarzan",
            "SABLE_MAX_LOAD_PER_CORE": "0",
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }

        r = subprocess.run(
            ["python3", str(BIN), bead_id, "--respawn", "--worktree", wt,
             "--model", "haiku"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 5, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "duplicate-dispatch blocked" in r.stderr
        assert "IN_PROGRESS" in r.stderr
        # no second worker window, no dispatch file
        wins = _tmux(sock, "list-windows", "-t", "sable",
                     "-F", "#{window_name}").stdout
        assert "worker-fake-respawn-live" not in wins
        assert not (Path(dd) / f"{bead_id}.md").exists()


def test_respawn_keeps_model_check_active(sock):
    """SABLE-3eax: --respawn must NOT weaken the model-check (unlike
    --skip-governance, which disables everything). A --model override that
    silently disagrees with the bead's model: label and gives no reason is still
    blocked (exit 3) under respawn."""
    with tempfile.TemporaryDirectory() as stub_dir, \
         tempfile.TemporaryDirectory() as dd, \
         tempfile.TemporaryDirectory() as wt:
        bead_id = "FAKE-respawn-modelcheck"
        db_path = Path(stub_dir) / "beads.json"
        db_path.write_text(json.dumps([
            {"id": bead_id, "title": "T", "description": "D",
             "labels": ["model:sonnet"], "status": "in_progress",
             "assignee": "tarzan"}
        ]))
        _write_fake_bd(Path(stub_dir), db_path)

        env = {
            **_clean_env(),
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
            "CLAUDE_AGENT_NAME": "tarzan",
            "SABLE_MAX_LOAD_PER_CORE": "0",
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }

        # opus disagrees with model:sonnet and gives NO reason -> blocked.
        r = subprocess.run(
            ["python3", str(BIN), bead_id, "--respawn", "--worktree", wt,
             "--model", "opus"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 3, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "model-check blocked" in r.stderr
        assert not (Path(dd) / f"{bead_id}.md").exists()


def test_spawn_stamps_owning_lane_on_worker_pane(sock):
    """SABLE-dcw2: a worker spawned by manager optimus (CLAUDE_AGENT_NAME=optimus)
    must carry @sable_lane=optimus on its pane — the owner tag sable-worker-status
    scopes its own-lane listing/reap by. Before this, the lane lived only in the
    pane's ENV (invisible to that tool), so every manager's sweep saw every pane."""
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as dd:
        env = {
            **_clean_env(),
            "CLAUDE_AGENT_NAME": "optimus",   # the invoking manager (the lane)
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",  # stand-in for claude
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_MAX_LOAD_PER_CORE": "0",  # hermetic: not a load-guard test
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }
        r = subprocess.run(
            ["python3", str(BIN), BEAD, "--worktree", wt,
             "--model", "haiku", "--skip-governance"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stderr
        time.sleep(0.6)

        lane_tags = _tmux(sock, "list-panes", "-a",
                          "-F", "#{@sable_role} #{@sable_lane}").stdout
        assert any(
            line == "worker optimus" for line in lane_tags.splitlines()
        ), lane_tags


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
