#!/usr/bin/env python3
"""Integration test for sable_footprint_lib's read-side loud-on-ambiguity
report (SABLE-kznzo), against a REAL bd store and a REAL bead body.

The unit suite (test_footprint_lib.py) proves the parser in isolation. This
file proves the same property end to end through the actual seam
`declared_reads()`/`parse_declared_reads()` use in production: a real bead
created with `bd create`, read back with the real `bd show --json`, exactly
as sable-merge-gate would encounter it.

Sandbox discipline matches test_sable_bd_remember_integration.py: a throwaway
HOME + `bd init --non-interactive` in a scratch work dir, never the
developer's own beads DB. Self-skips when bd is absent (ci-verify's
clean-room has no bd by design).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sable_footprint_lib as fp  # noqa: E402

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


def _run(argv, cwd, home, check=True):
    env = _env(home)
    cp = subprocess.run(argv, cwd=str(cwd), env=env, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    if check and cp.returncode != 0:
        raise AssertionError(f"{argv} failed: {cp.stdout}")
    return cp


def _robust_bd_init(work, home):
    """Mirrors test_sable_bd_remember_integration.py's helper: `bd init` on
    the embedded-Dolt backend can leave a partial DB on a first-run race (rc
    0 but no .beads/config.yaml) — gate success on that artifact and
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


def test_real_bead_with_prose_section_does_not_hide_a_dropped_path(sandbox, tmp_path, monkeypatch):
    """THE case the whole bead exists for, end to end: a real bead's
    '## File reads' section carries six real paths, a bare filename
    ('Makefile' — no '/', no known code suffix, lexically identical to an
    English word), AND a rationale paragraph explaining why comma form is
    load-bearing (the exact shape that tripped chuck's bridge on SABLE-1u6dr).
    The bare filename must be surfaced BY NAME in the dropped report, not
    merely folded into a drop count that would make it indistinguishable
    from the surrounding prose noise."""
    work, home = sandbox

    body = (
        "Story: exercise the reads parser against a live bead body.\n\n"
        "## File reads\n"
        "bin/one.py, bin/two.py, bin/three.py, bin/four.py, bin/five.py, "
        "Makefile, COMMA FORM IS DELIBERATE (SABLE-546m5): newline-per-path "
        "is silently truncated to one entry by the dispatch-side parser, so "
        "do not reformat this section.\n"
    )
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")

    cp = _run(["bd", "create", "--title", "prose reads section repro",
              "--type", "task", "--priority", "3",
              "--body-file", str(body_file), "--silent"], work, home)
    bead_id = cp.stdout.strip()
    assert bead_id, "bd create did not return an issue id"

    readback = _run(["bd", "show", bead_id, "--json"], work, home)
    assert "Makefile" in readback.stdout, "the fixture body itself must round-trip through real bd"

    # Exercise the real production seam: _read_bead shells out to the real
    # `bd show --json` via the same subprocess seam declared_reads() uses.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BD_NON_INTERACTIVE", "1")
    monkeypatch.setenv("CI", "true")
    for leak in _ENV_LEAKS:
        monkeypatch.delenv(leak, raising=False)

    record = fp._read_bead(str(work), bead_id)
    declared, entries, dropped = fp.parse_declared_reads(record["description"])

    assert declared is True
    assert entries == {
        "bin/one.py", "bin/two.py", "bin/three.py", "bin/four.py", "bin/five.py",
    }, "the five real declared paths must all parse"
    assert "Makefile" in dropped, (
        "the bare filename must be named specifically in the dropped report — "
        "a count alone is exactly what makes this class of drop invisible")
    assert len(dropped) > 1, (
        "the rationale paragraph must ALSO contribute drops — this is not a "
        "fix that special-cases 'Makefile', it is the tokenizer honestly "
        "reporting every non-path-shaped token it saw")

    # declared_reads() (the Footprint-returning production entry point) must
    # still resolve on this bead — a present section, even one that dropped
    # tokens, is an ANSWER (not a FootprintUndetermined non-answer) per the
    # SABLE-jd5fj.18 trichotomy this module builds on.
    footprint = fp.declared_reads(str(work), bead_id)
    assert footprint.entries == entries


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
