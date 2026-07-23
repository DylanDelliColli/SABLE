#!/usr/bin/env python3
"""Integration tests for the suite-run registry — REAL processes, REAL git
repos, no mocks (SABLE-pk15w / SABLE-4qlcf).

Every case here starts an actual runner in a scratch git repo and samples
clearance while it runs. The scratch repo has its OWN git common dir, so it has
its own registry and the ambient fleet on the host is invisible to it — the
isolation is structural, not a stub.

WHAT THE UNIT SUITE CANNOT PROVE, AND THIS ONE MUST:

  * THE GAP BETWEEN SUBPROCESS INVOCATIONS. A runner that execs each suite is
    visible to ps only DURING an invocation. That is the precise moment the
    2026-07-22 clearance would have read CLEAR with a suite run in flight. Here
    the gaps are real elapsed time and the sampler proves a registration was
    held across them WHILE NO SUITE PROCESS EXISTED — the assertion is on that
    conjunction, not merely on "never clear", because "never clear" is also what
    a gate that can never release produces.
  * THE IN-PROCESS RUN. A real `pytest` run loads bins in-process and never
    appears in the process table as anything but pytest itself. Its registration
    comes from bin/conftest.py, which pytest loads for ANY collection — the
    reason the python half needs no runner enumeration at all.
  * THE SHIPPED RUNNERS, verbatim. .github/ci/test-tiers.sh and
    .github/ci/shell-run-set.sh are COPIED, not reimplemented; a test that
    reimplemented them would pass while the real ones stayed uncovered.

PLANT-AND-FAIL for the SABLE-4qlcf half lives here (the pk15w half is in
test_sable_runlock.py, deliberately separate): delete the registration from one
runner and the corresponding case goes red — and goes red by producing an actual
FALSE CLEAR in a gap, which is the defect itself rather than a proxy for it.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent
REPO = BIN.parent
_spec = importlib.util.spec_from_file_location(
    "sable_runlock_lib_itest", BIN / "sable_runlock_lib.py")
rl = importlib.util.module_from_spec(_spec)
sys.modules["sable_runlock_lib_itest"] = rl
_spec.loader.exec_module(rl)

HAVE_GIT = shutil.which("git") is not None
HAVE_PS = shutil.which("ps") is not None
pytestmark = pytest.mark.skipif(not (HAVE_GIT and HAVE_PS),
                                reason="needs real git + ps")

SAMPLE_INTERVAL = 0.02


def _run(*args, cwd, env=None):
    return subprocess.run(args, cwd=str(cwd), check=True, capture_output=True,
                          text=True, env=env)


def _child_env(**extra):
    """A child env with the registry override REMOVED — the scratch runs must
    resolve their registry the production way (via their own git common dir),
    or these tests would prove nothing about the real resolution path."""
    env = {k: v for k, v in os.environ.items() if k != rl.ENV_REGISTRY_DIR}
    env.update(extra)
    return env


@pytest.fixture()
def scratch(tmp_path):
    """A real git repo carrying VERBATIM copies of the shipped runners."""
    repo = tmp_path / "scratch"
    (repo / "bin").mkdir(parents=True)
    (repo / ".github" / "ci").mkdir(parents=True)
    (repo / "hooks" / "test").mkdir(parents=True)

    for rel in ("bin/sable_runlock_lib.py", "bin/sable-run-registry",
                "bin/conftest.py", ".github/ci/shell-run-set.sh",
                ".github/ci/test-tiers.sh"):
        dst = repo / rel
        shutil.copy2(REPO / rel, dst)
        dst.chmod(0o755)

    env = _child_env(GIT_CONFIG_GLOBAL=str(tmp_path / "gitconfig"),
                     GIT_CONFIG_SYSTEM="/dev/null")
    _run("git", "init", "-q", "-b", "main", cwd=repo, env=env)
    _run("git", "config", "user.email", "t@t", cwd=repo, env=env)
    _run("git", "config", "user.name", "t", cwd=repo, env=env)
    return repo


def _stub_suites(repo: Path, names: list[str], seconds: float) -> None:
    for name in names:
        p = repo / "hooks" / "test" / name
        p.write_text(f"#!/usr/bin/env bash\nsleep {seconds}\nexit 0\n")
        p.chmod(0o755)


def _pre_push_suites() -> list[str]:
    """The pre_push tier membership, read from the REAL tier SSOT rather than
    hardcoded — this suite must not become the second place that list lives."""
    out = subprocess.run(["bash", str(REPO / ".github/ci/test-tiers.sh"),
                          "--list", "pre_push"],
                         capture_output=True, text=True, cwd=str(REPO))
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def _suite_processes_visible(repo: Path) -> bool:
    """What a POINT-IN-TIME ps probe would see right now: is any suite-shaped
    process of this scratch repo in the process table at this instant?"""
    roots = rl.worktree_roots(str(repo))
    try:
        attributed, _ = rl.scan_processes(roots)
    except rl.ProbeError:
        return False
    return bool(attributed)


def _sample_until_exit(proc: subprocess.Popen, repo: Path, limit: float = 90.0):
    """Sample clearance densely for the runner's whole lifetime. A sample is
    (state, ps_visible)."""
    samples: list[tuple[str, bool]] = []
    deadline = time.monotonic() + limit
    while proc.poll() is None and time.monotonic() < deadline:
        visible = _suite_processes_visible(repo)
        state = rl.clearance(base=str(repo)).state
        samples.append((state, visible))
        time.sleep(SAMPLE_INTERVAL)
    proc.wait(timeout=30)
    return samples


def held_window(samples):
    """The samples from the FIRST held registration to the LAST one, inclusive.

    Sampling a real process necessarily straddles two moments that are not part
    of the run: after fork but before the runner has taken its lock, and after
    it has released but before the OS reports it exited. Both are correctly
    not-busy, and folding them into the claim would make this suite assert
    something the interlock never promised — and, worse, make it flaky in the
    direction of passing for the wrong reason. The claim under test is that the
    registration is CONTINUOUS: nothing in between may read clear.
    """
    idx = [i for i, (s, _) in enumerate(samples) if s == rl.BUSY]
    if not idx:
        return []
    return samples[idx[0]:idx[-1] + 1]


def gap_covered(window) -> bool:
    """True when the registration was held at an instant where NO suite process
    was visible — the between-invocations moment a point-in-time ps probe falls
    into, and the reason this is a registry rather than a probe."""
    return any(s == rl.BUSY and not visible for s, visible in window)


# --- negative control: a real, quiet scratch repo clears ---------------------


def test_real_quiet_repo_returns_clear(scratch):
    """PROVE-THE-GATE-CAN-RELEASE, end to end: real git, real ps, real registry
    resolution, nothing running. If this ever fails the interlock has become a
    gate that never releases, which gets reverted within a day."""
    c = rl.clearance(base=str(scratch))
    assert c.state == rl.CLEAR, c.render()
    assert c.exit_code == 0


def test_real_registry_resolves_under_the_scratch_repos_own_git_dir(scratch):
    d = rl.registry_dir(str(scratch))
    assert d.startswith(str(Path(scratch / ".git").resolve()))
    assert rl.registry_dir(str(REPO)) != d      # and cannot collide with the fleet's


# --- the shipped shell runner, verbatim --------------------------------------


def test_real_tier_runner_is_never_clear_and_covers_the_gaps(scratch):
    """.github/ci/test-tiers.sh --run pre_push, copied verbatim, executing five
    real suite subprocesses.

    THREE assertions, and the third is the one this bead exists for:
      1. no sample reads CLEAR while it runs;
      2. no sample reads UNREGISTERED-RUNNER — the runner's own children are
         covered by its registration, so the loud state stays meaningful;
      3. at least one sample is BUSY *while no suite process is visible to ps* —
         i.e. the registration covered a moment when a point-in-time probe would
         have seen nothing and cleared the swap.
    """
    suites = _pre_push_suites()
    assert suites, "tier SSOT returned no pre_push suites"
    _stub_suites(scratch, suites, seconds=0.35)

    proc = subprocess.Popen(
        ["bash", str(scratch / ".github/ci/test-tiers.sh"), "--run", "pre_push"],
        cwd=str(scratch), env=_child_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples = _sample_until_exit(proc, scratch)
    window = held_window(samples)

    assert len(window) > 5, f"registration window too short to mean anything: {samples}"
    assert rl.CLEAR not in [s for s, _ in window], \
        f"clearance read CLEAR while the tier runner was in flight: {samples}"
    assert rl.UNREGISTERED not in [s for s, _ in window], \
        f"the runner's own children read as unregistered: {samples}"
    assert gap_covered(window), (
        "never sampled a moment where the registration was held but no suite "
        f"process was visible — the between-invocations case is unproven: {samples}")

    assert rl.clearance(base=str(scratch)).state == rl.CLEAR, \
        "the registration was not released when the runner exited"


def test_real_shell_run_set_registers_itself(scratch):
    """The other shipped runner. Its ALLOW list names suites this scratch repo
    does not have, so the run reds — irrelevant: the claim under test is that it
    HOLDS A REGISTRATION while it runs, and it must do so whether green or red.
    A runner that only registers on the happy path is not an interlock."""
    marker = scratch / "held.txt"
    # Give it a few real ALLOW-list suites (read from the tier SSOT, which
    # aliases ALLOW by reference) as slow stubs, so the run lasts long enough to
    # observe. The rest of ALLOW is absent and reds the run — deliberately.
    allow = subprocess.run(["bash", str(REPO / ".github/ci/test-tiers.sh"),
                            "--list", "merge_preview"],
                           capture_output=True, text=True, cwd=str(REPO))
    names = [ln.strip() for ln in allow.stdout.splitlines() if ln.strip()][:3]
    assert names, "tier SSOT returned no merge_preview suites"
    _stub_suites(scratch, names, seconds=0.5)
    proc = subprocess.Popen(
        ["bash", "-c",
         f'bash "{scratch}/.github/ci/shell-run-set.sh" --run >/dev/null 2>&1 &\n'
         f'RUNPID=$!\n'
         f'sleep 0.4\n'
         f'python3 "{scratch}/bin/sable-run-registry" list > "{marker}" 2>&1\n'
         f'wait $RUNPID\n'],
        cwd=str(scratch), env=_child_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait(timeout=90)
    held = marker.read_text()
    assert "shell-run-set.sh --run" in held, held
    assert held.startswith("LIVE"), held
    assert rl.clearance(base=str(scratch)).state == rl.CLEAR, "not released on exit"


# --- the in-process (pytest) run ---------------------------------------------


def test_real_pytest_run_is_never_clear_for_its_whole_duration(scratch):
    """The case NO process-table probe can see: pytest LOADS the bins in-process.
    Registration comes from bin/conftest.py, so the run is visible for its whole
    duration without ever forking anything."""
    (scratch / "bin" / "test_scratch_slow.py").write_text(
        "def test_slow():\n    import time\n    time.sleep(2.0)\n")
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "bin/test_scratch_slow.py", "-q",
         "-p", "no:cacheprovider"],
        cwd=str(scratch), env=_child_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples = _sample_until_exit(proc, scratch)
    window = held_window(samples)

    assert len(window) > 5, f"registration window too short: {samples}"
    assert rl.CLEAR not in [s for s, _ in window], \
        f"CLEAR during a live pytest run: {samples}"
    # The startup edge is covered too, by the OTHER layer: between fork and the
    # moment conftest registers, the corroborating probe already sees a pytest
    # process attributed to this repo, so those samples read UNREGISTERED-RUNNER
    # — loud, and conservative in the safe direction. Never clear.
    lead = samples[:samples.index(next(s for s in samples if s[0] == rl.BUSY))]
    assert rl.CLEAR not in [s for s, _ in lead], \
        f"a pytest run read CLEAR before its session registered: {samples}"
    assert rl.clearance(base=str(scratch)).state == rl.CLEAR, "not released on exit"


def test_a_pytest_run_of_a_single_file_registers_too(scratch):
    """Not just `pytest bin/`. conftest is loaded for ANY collection under bin/,
    which is why "which pytest command was it" is not a question anyone has to
    answer — the v2 clearance form's exact mistake."""
    (scratch / "bin" / "test_one.py").write_text(
        "def test_x():\n    import time\n    time.sleep(1.2)\n")
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "bin/test_one.py::test_x", "-q",
         "-p", "no:cacheprovider"],
        cwd=str(scratch), env=_child_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples = _sample_until_exit(proc, scratch)
    assert rl.BUSY in {s for s, _ in samples}, samples


# --- an UNREGISTERED runner, for real ----------------------------------------


def _write_third_party_runner(repo: Path, register: bool) -> Path:
    """A runner class that exists in no list anywhere — the "third runner" both
    beads are about. It execs suites as subprocesses with DELIBERATE, generous
    gaps between them, which is what makes the between-invocations window
    observable rather than a race."""
    hold = ""
    if register:
        hold = (
            f'TOK="$(python3 "{repo}/bin/sable-run-registry" register '
            f'--runner "third-party-harness" --pid $$)"\n'
            f'trap \'python3 "{repo}/bin/sable-run-registry" release "$TOK"\' EXIT\n')
    p = repo / "third-party-harness.sh"
    p.write_text(
        "#!/usr/bin/env bash\n"
        f"cd '{repo}'\n"
        f"{hold}"
        "for s in hooks/test/test-a.sh hooks/test/test-b.sh; do\n"
        "  bash \"$s\"\n"
        "  sleep 0.7\n"      # <- the gap a point-in-time ps probe falls into
        "done\n")
    p.chmod(0o755)
    for name in ("test-a.sh", "test-b.sh"):
        q = repo / "hooks" / "test" / name
        q.write_text("#!/usr/bin/env bash\nsleep 0.25\nexit 0\n")
        q.chmod(0o755)
    return p


def test_unregistered_real_runner_reads_unregistered_not_clear(scratch):
    """A REAL process executing a REAL suite of this repo, with no registration.
    While its subprocess is visible the answer must be UNREGISTERED-RUNNER —
    a third state, distinct from busy and from clear, so "a runner that never
    registered" is something the seat LEARNS rather than something it cannot
    tell from an idle host."""
    runner = _write_third_party_runner(scratch, register=False)
    proc = subprocess.Popen(["bash", str(runner)], cwd=str(scratch),
                            env=_child_env(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples = _sample_until_exit(proc, scratch)
    states = [s for s, _ in samples]
    assert rl.UNREGISTERED in states, f"the gap was never detected at all: {samples}"
    assert rl.BUSY not in states, "nothing registered, so nothing may read busy"


def test_registering_that_same_runner_closes_the_gap(scratch):
    """The SAME runner, one registration added. Now it is BUSY for its whole
    life, including the 0.7s gaps where nothing is visible to ps — and the
    UNREGISTERED alarm falls silent, which is what makes that alarm worth
    reading."""
    runner = _write_third_party_runner(scratch, register=True)
    proc = subprocess.Popen(["bash", str(runner)], cwd=str(scratch),
                            env=_child_env(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples = _sample_until_exit(proc, scratch)
    window = held_window(samples)
    states = [s for s, _ in window]
    assert rl.CLEAR not in states, f"CLEAR while the runner ran: {samples}"
    assert rl.UNREGISTERED not in states, f"false alarm on a registered run: {samples}"
    assert gap_covered(window), f"the between-invocations gap was never sampled: {samples}"
    assert rl.clearance(base=str(scratch)).state == rl.CLEAR


def test_plant_and_fail_4qlcf_half_removing_a_runners_registration(scratch):
    """PLANT-AND-FAIL for the SABLE-4qlcf half, and the exact defect reproduced:
    with the registration removed, sampling the SAME runner produces at least one
    honest-looking CLEAR — during a gap between its suite invocations, with a
    suite run demonstrably in flight.

    That is the false CLEAR that would hot-swap a bin underneath a running
    suite. The paired assertion is the fix: with the registration restored, no
    sample clears (test_registering_that_same_runner_closes_the_gap)."""
    runner = _write_third_party_runner(scratch, register=False)
    proc = subprocess.Popen(["bash", str(runner)], cwd=str(scratch),
                            env=_child_env(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples = _sample_until_exit(proc, scratch)
    states = [s for s, _ in samples]
    assert rl.CLEAR in states, (
        "the planted defect did not reproduce — if an unregistered runner never "
        "reads CLEAR here, this suite is not testing what it claims")
    assert rl.UNREGISTERED in states, (
        "and the gap must still be DETECTABLE while its subprocess is visible — "
        "otherwise the failure is silent, which is the shape being removed")


# --- stale, for real ---------------------------------------------------------


def test_a_killed_runner_leaves_a_stale_entry_that_does_not_release(scratch):
    """A real SIGKILL — no trap fires, no release happens. The entry must read
    STALE (fail-closed) and stay until an explicit reap: a crashed runner is
    exactly when a silent timeout would hand out a false CLEAR."""
    script = scratch / "killme.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'TOK="$(python3 "{scratch}/bin/sable-run-registry" register '
        f'--runner "killme" --pid $$)"\n'
        f'echo "$TOK" > "{scratch}/tok"\n'
        "sleep 60\n")
    script.chmod(0o755)
    proc = subprocess.Popen(["bash", str(script)], cwd=str(scratch),
                            env=_child_env())
    for _ in range(200):
        if (scratch / "tok").is_file():
            break
        time.sleep(0.02)
    assert rl.clearance(base=str(scratch)).state == rl.BUSY
    proc.kill()
    proc.wait(timeout=10)

    c = rl.clearance(base=str(scratch))
    assert c.state == rl.STALE, c.render()
    assert c.state != rl.CLEAR

    token = (scratch / "tok").read_text().strip()
    r = subprocess.run([sys.executable, str(scratch / "bin/sable-run-registry"),
                        "reap", token], cwd=str(scratch), env=_child_env(),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert rl.clearance(base=str(scratch)).state == rl.CLEAR, \
        "an explicit reap must actually release — a gate that cannot be cleared " \
        "by the operator will be worked around instead"


def test_an_orderly_sigterm_releases_the_registration(scratch):
    """The other side of the stale case, and the reason it is not just a nuisance
    setting: a CI `timeout` or a Ctrl-C is an ORDERLY termination and must
    release. bash fires no EXIT trap on an untrapped signal, so without the
    signal traps in runlock_hold every timed-out CI run would leave a stale
    entry and the next clearance would be NOT-CLEAR for a runner that is long
    gone — a gate people learn to reap reflexively is a gate they will reap
    while something is genuinely running."""
    suites = _pre_push_suites()
    _stub_suites(scratch, suites, seconds=5.0)
    proc = subprocess.Popen(
        ["bash", str(scratch / ".github/ci/test-tiers.sh"), "--run", "pre_push"],
        cwd=str(scratch), env=_child_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(300):
        if rl.clearance(base=str(scratch)).state == rl.BUSY:
            break
        time.sleep(0.02)
    assert rl.clearance(base=str(scratch)).state == rl.BUSY, "never registered"

    proc.terminate()
    proc.wait(timeout=30)
    for _ in range(100):
        if rl.clearance(base=str(scratch)).state == rl.CLEAR:
            break
        time.sleep(0.02)
    c = rl.clearance(base=str(scratch))
    assert c.state == rl.CLEAR, f"SIGTERM left the registration behind: {c.render()}"


def test_a_sigtermed_pytest_run_releases_its_registration(scratch):
    """The python half of the same claim, and it is not hypothetical: a
    `timeout 900 python -m pytest bin/` expiring on a loaded host is exactly how
    this was found. Without conftest's SIGTERM handler the session dies before
    pytest_sessionfinish and leaves a stale entry indistinguishable from a
    crash."""
    (scratch / "bin" / "test_long.py").write_text(
        "def test_long():\n    import time\n    time.sleep(30)\n")
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "bin/test_long.py", "-q",
         "-p", "no:cacheprovider"],
        cwd=str(scratch), env=_child_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(400):
        if rl.clearance(base=str(scratch)).state == rl.BUSY:
            break
        time.sleep(0.02)
    assert rl.clearance(base=str(scratch)).state == rl.BUSY, "never registered"

    proc.terminate()
    proc.wait(timeout=30)
    for _ in range(100):
        if rl.clearance(base=str(scratch)).state == rl.CLEAR:
            break
        time.sleep(0.02)
    c = rl.clearance(base=str(scratch))
    assert c.state == rl.CLEAR, f"SIGTERM left the pytest registration: {c.render()}"


# --- the CLI contract a merge seat actually uses -----------------------------


def test_cli_clearance_exit_code_drives_a_shell_gate(scratch):
    """How the seat uses it: `sable-run-registry clearance || refuse-to-merge`."""
    cli = str(scratch / "bin" / "sable-run-registry")
    quiet = subprocess.run([sys.executable, cli, "clearance"], cwd=str(scratch),
                           env=_child_env(), capture_output=True, text=True)
    assert quiet.returncode == 0, quiet.stdout
    assert "CLEAR" in quiet.stdout

    tok = subprocess.run([sys.executable, cli, "register", "--runner", "seat-test"],
                         cwd=str(scratch), env=_child_env(),
                         capture_output=True, text=True).stdout.strip()
    # That python process has exited, so its entry is STALE by construction —
    # and STALE must not clear.
    busy = subprocess.run([sys.executable, cli, "clearance"], cwd=str(scratch),
                          env=_child_env(), capture_output=True, text=True)
    assert busy.returncode != 0
    assert "CLEAR" not in busy.stdout.splitlines()[0]

    subprocess.run([sys.executable, cli, "release", tok], cwd=str(scratch),
                   env=_child_env(), check=True)
    again = subprocess.run([sys.executable, cli, "clearance"], cwd=str(scratch),
                           env=_child_env(), capture_output=True, text=True)
    assert again.returncode == 0, again.stdout
