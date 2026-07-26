#!/usr/bin/env python3
"""Integration rehearsals for the SCHEDULE LEG of bin/sable-reconcile-timer
(SABLE-5xz68).

WHY THIS SUITE EXISTS. sable-orchestration-install generated three artefacts —
a .service, a .timer and a .cron line — and then ECHOED the commands to install
them for a human to run. Nobody ran them. The units sat staged on disk for
months while `systemctl --user list-timers` and `crontab -l` knew nothing about
them, so the reconciliation floor's pane-independent leg did not exist at all;
the decisive evidence was that the log path the cron line redirects to had never
been created. Generating a unit file is not installing it, and reading a staged
file back can never tell you whether anything is scheduled.

So every assertion here interrogates the SCHEDULER SURFACES the way the real
check does — `systemctl --user is-active/cat` and `crontab -l` — through real
subprocesses on a real filesystem, with those two commands stubbed as actual
executables on PATH (the host's own systemd/cron are never touched, and this
suite must never schedule anything on the machine running it).

The second defect this pins is worse than the first: the generated units
hardcoded the SABLE tooling repo, so an operator who followed the printed
recipe exactly got a timer that fired on a cadence, swept the wrong repo, and
LOOKED protected. A schedule that exists but does not cover the repo you asked
about must therefore fail the check just as loudly as no schedule at all.
"""
import os
import subprocess
import sys
from pathlib import Path

TIMER_BIN = Path(__file__).resolve().parent / "sable-reconcile-timer"
INSTALLER = Path(__file__).resolve().parent / "sable-orchestration-install"

SERVICE = "sable-reconcile-timer.service"
TIMER_UNIT = "sable-reconcile-timer.timer"

# env that must not leak in from the developer session running the suite
_ENV_LEAKS = ("SABLE_RECONCILE_REPO", "SABLE_RECONCILE_INTERVAL_MIN",
              "SABLE_RECONCILE_TARGET_REPO", "SABLE_RC_BD", "SABLE_RC_REPO")

# A stub `systemctl`: `enable` marks the timer active by touching a state file
# (what real systemd does observably), `is-active` reports it, and `cat` echoes
# the unit that was ACTUALLY installed into the systemd --user dir — so repo
# coverage is read off the installed artefact, not off anything the test asserts
# into existence. STUB_ENABLE_BROKEN models the failure this whole check exists
# for: an enable that reports success while scheduling nothing.
_SYSTEMCTL_STUB = r"""#!/usr/bin/env python3
import os, sys, pathlib
args = [a for a in sys.argv[1:] if a != "--user"]
state = pathlib.Path(os.environ["STUB_STATE"])
log = state / "log"
log.open("a").write(" ".join(args) + "\n")
cmd = args[0] if args else ""
if cmd == "is-active":
    sys.exit(0 if (state / "active").exists() else 3)
if cmd == "enable":
    if os.environ.get("STUB_ENABLE_BROKEN") != "1":
        (state / "active").write_text("active\n")
    sys.exit(0)
if cmd == "cat":
    unit = pathlib.Path(os.environ["XDG_CONFIG_HOME"]) / "systemd" / "user" / args[1]
    sys.stdout.write(unit.read_text() if unit.is_file() else "")
    sys.exit(0)
sys.exit(0)
"""

_CRONTAB_STUB = r"""#!/usr/bin/env python3
import os, sys, pathlib
tab = pathlib.Path(os.environ["STUB_STATE"]) / "crontab"
if "-l" in sys.argv[1:]:
    if not tab.is_file():
        sys.stderr.write("no crontab for tester\n")
        sys.exit(1)
    sys.stdout.write(tab.read_text())
sys.exit(0)
"""


def _sandbox(tmp_path, *, enable_broken=False):
    """A HOME/XDG sandbox plus stub systemctl+crontab on PATH. Returns the env
    and the stub state dir (write `crontab`/read `log` there)."""
    home = tmp_path / "home"
    xdg = home / ".config"
    stub_bin = tmp_path / "stubbin"
    state = tmp_path / "stubstate"
    for d in (home, xdg, stub_bin, state):
        d.mkdir(parents=True, exist_ok=True)

    (stub_bin / "systemctl").write_text(_SYSTEMCTL_STUB)
    (stub_bin / "crontab").write_text(_CRONTAB_STUB)
    for name in ("systemctl", "crontab"):
        (stub_bin / name).chmod(0o755)

    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg),
        "STUB_STATE": str(state),
        # stub dir FIRST so the host's real systemctl/crontab are unreachable
        "PATH": f"{stub_bin}{os.pathsep}{env.get('PATH', '')}",
    })
    if enable_broken:
        env["STUB_ENABLE_BROKEN"] = "1"
    return env, state


def _timer(env, *args, cwd=None):
    return subprocess.run([sys.executable, str(TIMER_BIN), *args],
                          env=env, cwd=str(cwd) if cwd else None, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)


def _stage_units(tmp_path, repos):
    """Stand in for what sable-orchestration-install stages on disk."""
    units = tmp_path / "staged"
    units.mkdir(parents=True, exist_ok=True)
    repo_args = " ".join(f"--repo {r}" for r in repos)
    (units / SERVICE).write_text(
        "[Unit]\nDescription=SABLE reconciliation floor sweep\n\n"
        "[Service]\nType=oneshot\n"
        f"ExecStart=%h/.local/bin/sable-reconcile-timer --once {repo_args}\n")
    (units / TIMER_UNIT).write_text(
        "[Timer]\nOnUnitActiveSec=15min\nUnit=sable-reconcile-timer.service\n\n"
        "[Install]\nWantedBy=timers.target\n")
    return units


def _cron_line(repos):
    repo_args = " ".join(f"--repo {r}" for r in repos)
    return ('*/15 * * * * PATH="$PATH:$HOME/.local/bin" sable-reconcile-timer '
            f'--once {repo_args} >> "$HOME/.cache/sable/reconcile-timer.log" 2>&1\n')


# ===========================================================================
# DEFECT ONE — staged is not scheduled.
# ===========================================================================

def test_check_schedule_fails_loudly_when_nothing_is_scheduled(tmp_path):
    # The literal SABLE-5xz68 host state: unit files staged on disk, no timer
    # active, no crontab entry. Silence here is how it went unnoticed.
    env, _ = _sandbox(tmp_path)
    _stage_units(tmp_path, ["/srv/fleet-alpha"])
    r = _timer(env, "--check-schedule", "--repo", "/srv/fleet-alpha")
    assert r.returncode == 3, f"rc={r.returncode}\n{r.stdout}{r.stderr}"
    assert "NO SCHEDULE" in r.stderr, r.stderr


def test_check_schedule_fails_even_with_no_repo_argument(tmp_path):
    # 'is ANYTHING scheduled at all' is answerable without naming a repo, and
    # must still be answered NO rather than trivially green.
    env, _ = _sandbox(tmp_path)
    r = _timer(env, "--check-schedule")
    assert r.returncode == 3, r.stdout + r.stderr
    assert "NO SCHEDULE" in r.stderr, r.stderr


def test_check_schedule_passes_when_a_real_crontab_entry_covers_the_repo(tmp_path):
    env, state = _sandbox(tmp_path)
    (state / "crontab").write_text(_cron_line(["/srv/fleet-alpha"]))
    r = _timer(env, "--check-schedule", "--repo", "/srv/fleet-alpha")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK:" in r.stdout, r.stdout


def test_check_schedule_ignores_a_commented_out_crontab_entry(tmp_path):
    # A commented line is exactly what a half-finished manual activation leaves
    # behind, and it schedules nothing.
    env, state = _sandbox(tmp_path)
    (state / "crontab").write_text("# " + _cron_line(["/srv/fleet-alpha"]))
    r = _timer(env, "--check-schedule", "--repo", "/srv/fleet-alpha")
    assert r.returncode == 3, r.stdout + r.stderr
    assert "NO SCHEDULE" in r.stderr, r.stderr


# ===========================================================================
# DEFECT TWO — scheduled for the WRONG repo is the more dangerous state.
# ===========================================================================

def test_check_schedule_fails_when_the_schedule_sweeps_a_different_repo(tmp_path):
    env, state = _sandbox(tmp_path)
    (state / "crontab").write_text(_cron_line(["/home/ddc/dev-environment/SABLE"]))
    r = _timer(env, "--check-schedule", "--repo", "/srv/costing-comparison")
    assert r.returncode == 3, r.stdout + r.stderr
    assert "/srv/costing-comparison" in r.stderr, r.stderr
    # it IS scheduled — misreporting this as 'nothing installed' would send the
    # operator to install a second, equally wrong schedule.
    assert "NO SCHEDULE" not in r.stderr, r.stderr


def test_check_schedule_fails_when_only_some_fleets_are_covered(tmp_path):
    env, state = _sandbox(tmp_path)
    (state / "crontab").write_text(_cron_line(["/srv/fleet-alpha"]))
    r = _timer(env, "--check-schedule", "--repo", "/srv/fleet-alpha",
               "--repo", "/srv/fleet-beta")
    assert r.returncode == 3, r.stdout + r.stderr
    assert "/srv/fleet-beta" in r.stderr, r.stderr


# ===========================================================================
# --install-schedule — the single operator command, which VERIFIES itself.
# ===========================================================================

def test_install_schedule_installs_enables_and_verifies(tmp_path):
    env, state = _sandbox(tmp_path)
    units = _stage_units(tmp_path, ["/srv/fleet-alpha", "/srv/fleet-beta"])

    r = _timer(env, "--install-schedule", "--units-dir", str(units))
    assert r.returncode == 0, r.stdout + r.stderr

    dest = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    assert (dest / SERVICE).is_file(), sorted(p.name for p in dest.iterdir())
    assert (dest / TIMER_UNIT).is_file()
    log = (state / "log").read_text()
    assert "daemon-reload" in log, log
    assert f"enable --now {TIMER_UNIT}" in log, log
    # and it reports what is actually swept, read back off the running system
    assert "/srv/fleet-alpha" in r.stdout and "/srv/fleet-beta" in r.stdout, r.stdout


def test_install_schedule_is_not_green_when_enabling_scheduled_nothing(tmp_path):
    # THE bug, in its most dangerous form: the install steps all 'succeed' but
    # no timer is actually active afterwards. Reporting success here is what
    # produced a host that looked protected for months.
    env, _ = _sandbox(tmp_path, enable_broken=True)
    units = _stage_units(tmp_path, ["/srv/fleet-alpha"])
    r = _timer(env, "--install-schedule", "--units-dir", str(units))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "NO SCHEDULE" in r.stderr, r.stderr


def test_install_schedule_refuses_when_units_were_never_staged(tmp_path):
    env, _ = _sandbox(tmp_path)
    r = _timer(env, "--install-schedule", "--units-dir", str(tmp_path / "nothing-here"))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "not found" in r.stderr, r.stderr


def test_check_schedule_reads_the_installed_unit_not_the_staged_one(tmp_path):
    """After activation, editing the STAGED file changes nothing about what is
    scheduled — the check must keep answering from the running system, or it
    becomes another way to believe a file instead of a host."""
    env, _ = _sandbox(tmp_path)
    units = _stage_units(tmp_path, ["/srv/fleet-alpha"])
    assert _timer(env, "--install-schedule", "--units-dir", str(units)).returncode == 0

    (units / SERVICE).write_text(
        "[Service]\nExecStart=%h/.local/bin/sable-reconcile-timer --once "
        "--repo /srv/fleet-beta\n")
    r = _timer(env, "--check-schedule", "--repo", "/srv/fleet-beta")
    assert r.returncode == 3, r.stdout + r.stderr
    assert "/srv/fleet-beta" in r.stderr, r.stderr
    # the genuinely-scheduled repo still verifies
    assert _timer(env, "--check-schedule", "--repo", "/srv/fleet-alpha").returncode == 0


# ===========================================================================
# End-to-end: the REAL installer generates the units, the REAL activation
# command installs them, and the REAL check confirms the fleets it names are
# the fleets that get swept. This is the composition the bead's two defects
# broke in different places.
# ===========================================================================

def test_generated_units_activate_and_verify_for_the_repos_passed_to_the_installer(tmp_path):
    env, _ = _sandbox(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    install_env = dict(env)
    install_env["SABLE_PROJECT_DIR"] = str(project)
    install_env["SABLE_RECONCILE_TARGET_REPO"] = "/srv/fleet-alpha:/srv/fleet-beta"
    cp = subprocess.run([str(INSTALLER), "--project"], env=install_env, cwd=str(tmp_path),
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        timeout=180)
    assert cp.returncode == 0, cp.stdout

    staged = project / ".claude" / "sable" / "reconcile-timer"
    # staging alone must NOT have scheduled anything (install stays inert)
    assert _timer(env, "--check-schedule").returncode == 3

    r = _timer(env, "--install-schedule", "--units-dir", str(staged))
    assert r.returncode == 0, r.stdout + r.stderr
    for repo in ("/srv/fleet-alpha", "/srv/fleet-beta"):
        chk = _timer(env, "--check-schedule", "--repo", repo)
        assert chk.returncode == 0, f"{repo} not covered:\n{chk.stdout}{chk.stderr}"
    # and a fleet nobody asked for is still reported as uncovered
    assert _timer(env, "--check-schedule", "--repo", "/srv/fleet-gamma").returncode == 3
