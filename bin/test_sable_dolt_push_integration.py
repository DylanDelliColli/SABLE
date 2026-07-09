#!/usr/bin/env python3
"""Integration tests for bin/sable-dolt-push (SABLE-ipcf).

Real composition: the actual `sable-dolt-push` binary run as a subprocess,
real filesystem locks, real concurrent OS processes, and — where dolt is
installed — a REAL `dolt push` to a scratch `file://` remote.

SAFETY (non-negotiable): nothing here runs a real `bd dolt push`. dolt push is
CHUCK-ONLY and the shared beads remote corrupts on concurrent pushes. Every
push/pull/bounce the wrapper drives is redirected via the SABLE_DOLT_*_CMD
seams to scratch scripts or a throwaway scratch dolt repo whose remote is a
local directory created per-test.
"""
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-dolt-push"
HAVE_DOLT = shutil.which("dolt") is not None


# --- helpers -----------------------------------------------------------------

def write_script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def base_env(tmp_path, lock, **extra):
    env = {**os.environ}
    env["HOME"] = str(tmp_path)                 # isolate any HOME-derived path
    env["SABLE_DOLT_PUSH_LOCK"] = str(lock)
    env["SABLE_FLEET_ID"] = extra.pop("fleet", "test-fleet")
    # no-op pull/bounce by default; individual tests override the push seam
    env["SABLE_DOLT_PULL_CMD"] = extra.pop("pull", "true")
    env["SABLE_DOLT_BOUNCE_CMD"] = extra.pop("bounce", "true")
    env.update(extra)
    return env


def run_wrapper(env, *args, timeout=60):
    return subprocess.run([sys.executable, str(BIN), *args],
                          capture_output=True, text=True, env=env, timeout=timeout)


def assert_no_interleave(log_path: Path):
    """The push log must be perfectly nested START/END per pid — proof the lock
    serialized the critical sections (never two pushes in flight at once)."""
    lines = [l.split() for l in log_path.read_text().splitlines() if l.strip()]
    stack = []
    for kind, pid in lines:
        if kind == "START":
            assert not stack, f"interleave: {pid} started while {stack} in flight"
            stack.append(pid)
        elif kind == "END":
            assert stack and stack[-1] == pid, f"bad END for {pid}, stack={stack}"
            stack.pop()
    assert not stack, f"unterminated push: {stack}"


# --- serialization: two concurrent wrappers never interleave -----------------

def test_two_concurrent_wrappers_serialize(tmp_path):
    lock = tmp_path / "dolt-push.lock"
    log = tmp_path / "push.log"
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    # push script: bracket a 0.4s critical section with START/END lines keyed by pid
    push = write_script(tmp_path / "push.sh", f"""
echo "START $$" >> "{log}"
sleep 0.4
echo "END $$" >> "{log}"
: > "{marker_dir}/$$"
""")
    env = base_env(tmp_path, lock, pull="true", bounce="true")
    env["SABLE_DOLT_PUSH_CMD"] = f"sh {push}"
    env["SABLE_DOLT_LOCK_WAIT"] = "30"          # the loser WAITS, not fails

    p1 = subprocess.Popen([sys.executable, str(BIN)], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p2 = subprocess.Popen([sys.executable, str(BIN)], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert p1.wait(timeout=60) == 0
    assert p2.wait(timeout=60) == 0

    assert len(list(marker_dir.iterdir())) == 2   # both pushes actually ran
    assert_no_interleave(log)                      # and they serialized
    assert not lock.exists()                       # lock released after each


# --- contention fail-fast: wait=0 against a live holder ----------------------

def test_contention_fails_cleanly_without_pushing(tmp_path):
    lock = tmp_path / "dolt-push.lock"
    ran = tmp_path / "ran"
    push = write_script(tmp_path / "push.sh", f': > "{ran}"\n')
    # pre-plant a FRESH (non-stale) lock held by another fleet
    lock.write_text(
        '{"fleet":"other","pid":999999,"timestamp":%d,"nonce":"deadbeef"}\n'
        % int(time.time()))
    env = base_env(tmp_path, lock)
    env["SABLE_DOLT_PUSH_CMD"] = f"sh {push}"
    env["SABLE_DOLT_LOCK_WAIT"] = "0"

    r = run_wrapper(env)
    assert r.returncode == 11, r.stderr           # EXIT_CONTENTION
    assert not ran.exists(), "push must NOT run when the lock is held"
    # the incumbent lock is left untouched (never clobbered / interleaved)
    assert '"fleet":"other"' in lock.read_text()


# --- stale-break: a lock older than the TTL is broken and the push proceeds ---

def test_stale_lock_broken_and_push_proceeds(tmp_path):
    lock = tmp_path / "dolt-push.lock"
    ran = tmp_path / "ran"
    push = write_script(tmp_path / "push.sh", f': > "{ran}"\n')
    # a lock left by a crashed holder ~1 hour ago
    lock.write_text(
        '{"fleet":"crashed","pid":123,"timestamp":%d,"nonce":"stale00"}\n'
        % (int(time.time()) - 3600))
    env = base_env(tmp_path, lock)
    env["SABLE_DOLT_PUSH_CMD"] = f"sh {push}"
    env["SABLE_DOLT_LOCK_WAIT"] = "0"             # would fail if it weren't stale
    env["SABLE_DOLT_LOCK_TTL"] = "600"

    r = run_wrapper(env)
    assert r.returncode == 0, r.stderr
    assert ran.exists(), "push should run after breaking the stale lock"
    assert not lock.exists()


# --- bounce-on-dangling: bounce once, retry once, then succeed ---------------

def test_dangling_chunk_bounces_and_retries_once(tmp_path):
    lock = tmp_path / "dolt-push.lock"
    counter = tmp_path / "attempts"
    bounced = tmp_path / "bounced"
    # push fails with a dangling-chunk error the FIRST time, succeeds the SECOND
    push = write_script(tmp_path / "push.sh", f"""
n=$(cat "{counter}" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "{counter}"
if [ "$n" -eq 1 ]; then
  echo "error: dangling chunk reference deadbeef" >&2
  exit 1
fi
echo "pushed ok"
""")
    bounce = write_script(tmp_path / "bounce.sh", f'echo bounced >> "{bounced}"\n')
    env = base_env(tmp_path, lock, pull="true")
    env["SABLE_DOLT_PUSH_CMD"] = f"sh {push}"
    env["SABLE_DOLT_BOUNCE_CMD"] = f"sh {bounce}"

    r = run_wrapper(env)
    assert r.returncode == 0, r.stderr
    assert counter.read_text().strip() == "2"     # original + exactly one retry
    assert bounced.exists()                        # server was bounced once
    assert len(bounced.read_text().splitlines()) == 1


def test_dangling_chunk_retry_still_failing_fails_loudly(tmp_path):
    lock = tmp_path / "dolt-push.lock"
    counter = tmp_path / "attempts"
    push = write_script(tmp_path / "push.sh", f"""
n=$(cat "{counter}" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "{counter}"
echo "fatal: dangling chunk reference persists" >&2
exit 1
""")
    env = base_env(tmp_path, lock, pull="true", bounce="true")
    env["SABLE_DOLT_PUSH_CMD"] = f"sh {push}"

    r = run_wrapper(env)
    assert r.returncode == 13, r.stderr            # EXIT_PUSH_FAIL
    assert counter.read_text().strip() == "2"      # one retry, no infinite loop
    assert not lock.exists()                        # lock released even on failure


# --- pull-before-push: a failed pull aborts before any push ------------------

def test_pull_failure_aborts_before_push(tmp_path):
    lock = tmp_path / "dolt-push.lock"
    ran = tmp_path / "ran"
    push = write_script(tmp_path / "push.sh", f': > "{ran}"\n')
    env = base_env(tmp_path, lock, pull="false")   # pull exits nonzero
    env["SABLE_DOLT_PUSH_CMD"] = f"sh {push}"

    r = run_wrapper(env)
    assert r.returncode == 12, r.stderr            # EXIT_PULL_FAIL
    assert not ran.exists(), "must not push when pull fails"


# --- REAL dolt: scratch file:// remote round-trip ---------------------------

@pytest.fixture()
def dolt_scratch(tmp_path):
    """A throwaway dolt repo wired to a local file:// remote, fully isolated
    from the user's real dolt config via DOLT_ROOT_PATH."""
    root = tmp_path / "doltcfg"
    root.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    remote = tmp_path / "remote"
    remote.mkdir()
    denv = {**os.environ, "DOLT_ROOT_PATH": str(root)}

    def d(*args, cwd=src):
        return subprocess.run(["dolt", *args], cwd=str(cwd), env=denv,
                              capture_output=True, text=True)

    d("config", "--global", "--add", "user.name", "sable-test", cwd=root)
    d("config", "--global", "--add", "user.email", "sable-test@example.com", cwd=root)
    assert d("init").returncode == 0
    assert d("remote", "add", "origin", f"file://{remote}").returncode == 0
    return {"src": src, "remote": remote, "denv": denv, "d": d}


@pytest.mark.skipif(not HAVE_DOLT, reason="dolt not installed")
def test_real_dolt_push_to_scratch_remote(tmp_path, dolt_scratch):
    d = dolt_scratch["d"]
    src = dolt_scratch["src"]
    assert d("sql", "-q", "create table t (id int primary key)").returncode == 0
    assert d("add", ".").returncode == 0
    assert d("commit", "-m", "init").returncode == 0

    lock = tmp_path / "dolt-push.lock"
    push = write_script(tmp_path / "push.sh",
                        f'cd "{src}"\nexec dolt push origin main\n')
    env = base_env(tmp_path, lock, pull="true", bounce="true")
    env["DOLT_ROOT_PATH"] = str(dolt_scratch["src"].parent / "doltcfg")
    env["SABLE_DOLT_PUSH_CMD"] = f"sh {push}"

    r = run_wrapper(env)
    assert r.returncode == 0, r.stdout + r.stderr
    # the scratch remote actually received main
    ls = subprocess.run(["dolt", "branch", "-r"], cwd=str(src),
                        env=dolt_scratch["denv"], capture_output=True, text=True)
    # fetch first so the remote-tracking ref is visible
    subprocess.run(["dolt", "fetch", "origin"], cwd=str(src),
                   env=dolt_scratch["denv"], capture_output=True, text=True)
    ls = subprocess.run(["dolt", "branch", "-r"], cwd=str(src),
                        env=dolt_scratch["denv"], capture_output=True, text=True)
    assert "origin/main" in ls.stdout, ls.stdout + ls.stderr


@pytest.mark.skipif(not HAVE_DOLT, reason="dolt not installed")
def test_two_concurrent_real_dolt_pushes_serialize(tmp_path, dolt_scratch):
    """Two wrappers push DISTINCT branches to the SAME scratch remote at once.
    The lock must serialize them so neither corrupts the remote and both land."""
    d = dolt_scratch["d"]
    src = dolt_scratch["src"]
    assert d("sql", "-q", "create table t (id int primary key)").returncode == 0
    assert d("add", ".").returncode == 0
    assert d("commit", "-m", "init").returncode == 0
    # pre-create two branches so the push scripts only push (no concurrent mutation)
    assert d("branch", "b1").returncode == 0
    assert d("branch", "b2").returncode == 0

    lock = tmp_path / "dolt-push.lock"
    log = tmp_path / "push.log"

    def make_env(branch):
        push = write_script(tmp_path / f"push-{branch}.sh", f"""
echo "START $$" >> "{log}"
cd "{src}"
dolt push origin {branch}
rc=$?
echo "END $$" >> "{log}"
exit $rc
""")
        env = base_env(tmp_path, lock, pull="true", bounce="true", fleet=f"fleet-{branch}")
        env["DOLT_ROOT_PATH"] = str(dolt_scratch["denv"]["DOLT_ROOT_PATH"])
        env["SABLE_DOLT_PUSH_CMD"] = f"sh {push}"
        env["SABLE_DOLT_LOCK_WAIT"] = "60"
        return env

    p1 = subprocess.Popen([sys.executable, str(BIN)], env=make_env("b1"),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p2 = subprocess.Popen([sys.executable, str(BIN)], env=make_env("b2"),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert p1.wait(timeout=120) == 0
    assert p2.wait(timeout=120) == 0

    assert_no_interleave(log)                       # serialized: never in flight together
    subprocess.run(["dolt", "fetch", "origin"], cwd=str(src),
                   env=dolt_scratch["denv"], capture_output=True, text=True)
    ls = subprocess.run(["dolt", "branch", "-r"], cwd=str(src),
                        env=dolt_scratch["denv"], capture_output=True, text=True)
    assert "origin/b1" in ls.stdout and "origin/b2" in ls.stdout, ls.stdout + ls.stderr
    assert not lock.exists()
