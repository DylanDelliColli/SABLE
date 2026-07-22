#!/usr/bin/env python3
"""Integration tests for bin/sable-bd-remember against a REAL sandbox bd (SABLE-tmbx1).

A throwaway HOME + `bd init --non-interactive` in a scratch dir, matching the
fixture discipline used elsewhere in this repo's real-bd suites (see
test_sable_reconcile_handoffs_integration.py) — never the developer's own
beads DB. Self-skips when bd/dolt are absent (ci-verify's clean-room has
neither by design).

Proves the fix end-to-end with REAL metacharacter content, not an innocent
fixture: `--file` round-trips backticks/$() through the real `bd remember` /
`bd memories` pipeline unexecuted and uncorrupted, and (plant-and-fail) the
OLD/vulnerable shape — the same content composed inline inside a
double-quoted shell argument, exactly how an agent's Bash tool would
naturally invoke `bd remember "<insight>"` — actually corrupts it and
actually executes the embedded command when run through a real shell. If the
plant-and-fail test ever stopped failing, the round-trip test above would no
longer be evidence of anything.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-bd-remember"

HAVE_BD = shutil.which("bd") is not None
pytestmark = pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips",
)

_ENV_LEAKS = ("CLAUDE_AGENT_NAME", "TMUX_PANE", "SABLE_TMUX_SOCKET")


def _env(home):
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    return env


def _run(argv, cwd, home, extra_env=None, check=True):
    env = _env(home)
    if extra_env:
        env.update(extra_env)
    cp = subprocess.run(argv, cwd=str(cwd), env=env, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    if check and cp.returncode != 0:
        raise AssertionError(f"{argv} failed: {cp.stdout}")
    return cp


def _robust_bd_init(work, home):
    """Mirrors test_sable_reconcile_handoffs_integration.py's helper: `bd init`
    on the embedded-Dolt backend can leave a partial DB on a first-run race
    (rc 0 but no .beads/config.yaml) — gate success on that artifact and
    wipe+retry rather than run against a broken DB."""
    beads = work / ".beads"
    last = None
    for _ in range(4):
        if beads.exists():
            shutil.rmtree(beads)
        last = _run(["bd", "init", "--non-interactive"], work, home, check=False)
        if last.returncode == 0 and (beads / "config.yaml").is_file():
            return last
    raise AssertionError(f"bd init never produced a clean DB: {last.stdout if last else '<none>'}")


@pytest.fixture()
def sandbox(tmp_path):
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    _robust_bd_init(work, home)
    return work, home


HAZARDOUS_INSIGHT = "see `hostname` and $(touch {marker}) now"


def test_body_file_round_trips_backticks_and_dollar_paren_through_real_bd(sandbox, tmp_path):
    work, home = sandbox
    marker = tmp_path / "would-be-pwned"
    insight = HAZARDOUS_INSIGHT.format(marker=marker)
    insight_path = tmp_path / "insight.txt"
    insight_path.write_text(insight, encoding="utf-8")

    cp = _run(["python3", str(BIN), "--file", str(insight_path), "--key", "tmbx1-repro"],
              work, home)
    assert cp.returncode == 0, cp.stdout

    readback = _run(["bd", "memories", "tmbx1-repro"], work, home)
    assert "`hostname`" in readback.stdout
    assert f"$(touch {marker})" in readback.stdout
    assert not marker.exists(), "the file-based path must never let $(...) execute"


def test_negative_control_inline_bd_remember_through_a_real_shell_is_corrupted(sandbox, tmp_path):
    """Plant-and-fail: the OLD/vulnerable shape — `bd remember "<insight>"`
    with the insight embedded inline inside a double-quoted shell argument,
    exactly how an agent's Bash tool would naively compose the call — must be
    shown to actually execute the embedded command and actually corrupt the
    stored memory. This is the reproduction --file above defends against."""
    work, home = sandbox
    marker = tmp_path / "executed-marker"
    shell_cmd = f'bd remember "see `hostname` and $(touch {marker}) now" --key tmbx1-plant'
    env = _env(home)
    cp = subprocess.run(shell_cmd, shell=True, cwd=str(work), env=env,
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    assert cp.returncode == 0, cp.stdout

    # HARM 1 -- EXECUTION: $(...) actually ran, with the caller's privileges,
    # before bd's argv was even populated.
    assert marker.exists(), (
        "expected the pre-fix vulnerable invocation to execute the command "
        "inside $(...) -- if this fails, the reproduction is no longer "
        "faithful to the reported bug and the contrast above proves nothing"
    )
    # HARM 2 -- CORRUPTION: the stored memory is missing the substituted
    # commands, silently replaced by their stdout, not the literal source.
    readback = _run(["bd", "memories", "tmbx1-plant"], work, home)
    assert "$(touch" not in readback.stdout
    assert "`hostname`" not in readback.stdout


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
