#!/usr/bin/env python3
"""Integration tests for `sable-screen activation` — the landability screen
(SABLE-6eu9w). Real filesystem, real git, real bd, real CLI. No mocks.

WHAT MAKES THIS AN INTEGRATION TEST rather than the unit suite with more
setup: every one of the four activation classes is produced by a REAL install
artifact of that shape — a real symlink into a real git working tree, a real
symlink into a real frozen snapshot directory, and a real plain-file copy —
and the classification is read back through the REAL command line, from real
beads in a real bd store, whose footprint sections are real prose parsed by the
real tokenizer. The unit suite can be fooled by a fixture that models the
install layout wrongly; this one cannot, because there is no model.

*** THE INSTALL PREFIX IS A SCRATCH DIRECTORY, AND THAT IS ENFORCED, NOT
INTENDED. *** SABLE-2avau records an installer silently targeting the live
~/.claude and SABLE-k0nvp a pinning suite polluting the real ~/.local twice.
test_screen_never_touches_the_real_install_prefix asserts the CLI reports
scanning ONLY the scratch prefix, so a future default that quietly reaches for
the developer's ~/.local/bin fails here instead of on their machine.

Self-skips when bd is absent (SABLE-k35mw: the ci-verify clean room is
tmux+pytest only, and an unguarded `bd` call ERRORS the gate rather than
skipping it).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-screen"

HAVE_BD = shutil.which("bd") is not None
pytestmark = pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips",
)

_ENV_LEAKS = ("CLAUDE_AGENT_NAME", "TMUX_PANE", "SABLE_TMUX_SOCKET",
              "SABLE_INTEGRATION_BRANCH", "SABLE_BASE_BRANCH",
              "SABLE_SCREEN_INSTALL_ROOTS")


def _env(home, extra=None):
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    if extra:
        env.update(extra)
    return env


def _run(argv, cwd, home, extra_env=None, check=True):
    cp = subprocess.run(argv, cwd=str(cwd), env=_env(home, extra_env), text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        timeout=180)
    if check and cp.returncode != 0:
        raise AssertionError(f"{argv} failed: {cp.stdout}")
    return cp


def _git(cwd, *args, check=True):
    cp = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                        cwd=str(cwd), text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and cp.returncode != 0:
        raise AssertionError(f"git {args} failed: {cp.stdout}")
    return cp.stdout.strip()


def _bd(cwd, home, *args, check=True):
    return _run(["bd", *args], cwd, home, check=check)


def _robust_bd_init(work, home):
    """Mirrors test_sable_screen_integration.py: `bd init` on the embedded-Dolt
    backend can leave a partial DB on a first-run race (rc 0 but no
    .beads/config.yaml) — gate success on that artifact, wipe and retry."""
    beads = work / ".beads"
    last = None
    for _ in range(4):
        if beads.exists():
            shutil.rmtree(beads)
        last = _run(["bd", "init", "--non-interactive"], work, home, check=False)
        if last.returncode == 0 and (beads / "config.yaml").is_file():
            return last
    raise AssertionError(f"bd init never produced a clean DB: "
                         f"{last.stdout if last else '<none>'}")


# --- the real fixture -------------------------------------------------------

def _setup(tmp_path):
    """A real git working tree, a real bd store, and a real install prefix
    holding one artifact of each installed shape."""
    work = tmp_path / "work"
    home = tmp_path / "home"
    prefix = tmp_path / "local-bin"
    snapshot = tmp_path / "pinned-snapshot"
    for d in (work, home, prefix, snapshot):
        d.mkdir(parents=True)

    (work / "bin").mkdir()
    (work / "docs").mkdir()
    (work / "bin" / "toolA").write_text(
        "#!/usr/bin/env python3\nimport lib_a\nprint(lib_a.VALUE)\n")
    (work / "bin" / "lib_a.py").write_text("VALUE = 1\n")
    (work / "bin" / "toolB").write_text("#!/usr/bin/env python3\nprint('b')\n")
    (work / "bin" / "toolC").write_text("#!/usr/bin/env python3\nprint('c')\n")
    (work / "bin" / "test_toolA.py").write_text("def test_x():\n    assert 1\n")
    (work / "docs" / "NOTES.md").write_text("# notes\n")

    _git(work, "init", "-q")
    nohooks = tmp_path / "nohooks"
    nohooks.mkdir()
    _git(work, "config", "--local", "core.hooksPath", str(nohooks))
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")

    # (1) HOT-SWAP: a real symlink into the real working tree.
    os.symlink(work / "bin" / "toolA", prefix / "toolA")
    # (2) PIN-MANAGED: a real symlink into a real frozen snapshot directory.
    (snapshot / "toolB").write_text("#!/usr/bin/env python3\n# frozen copy\n")
    os.symlink(snapshot / "toolB", prefix / "toolB")
    # (3) COPY-INSTALLED: a real plain file.
    (prefix / "toolC").write_text("#!/usr/bin/env python3\n# installed copy\n")

    _robust_bd_init(work, home)
    return work, home, prefix


def _create_bead(work, home, *, title, footprint=None):
    description = "Real integration fixture bead.\n"
    if footprint is not None:
        description += f"\n## File footprint\n{footprint}\n"
    cp = _bd(work, home, "create", "--sandbox", "--json", "--title", title,
             "--description", description, "--type=task", "--priority=2")
    return json.loads(cp.stdout)["id"]


def _screen(work, home, prefix, *args, extra_env=None, check=False):
    argv = [sys.executable, str(BIN), "activation", *args,
            "--repo", str(work), "--format", "json"]
    if prefix is not None:
        argv += ["--install-root", str(prefix)]
    return _run(argv, work, home, extra_env=extra_env, check=check)


def _rows(cp):
    payload = json.loads(cp.stdout)
    return payload["meta"], {r["bead_id"]: r for r in payload["results"]}


def _classes(row):
    return {p["path"]: p["class"] for p in row["paths"]}


# ===========================================================================
# the three installed shapes, produced for real, distinguished for real
# ===========================================================================

def test_three_install_shapes_come_back_distinctly(tmp_path):
    work, home, prefix = _setup(tmp_path)
    hot = _create_bead(work, home, title="touches the live-symlinked tool",
                       footprint="bin/toolA")
    pinned = _create_bead(work, home, title="touches the snapshot-pinned tool",
                          footprint="bin/toolB")
    copied = _create_bead(work, home, title="touches the plain-copy tool",
                          footprint="bin/toolC")

    cp = _screen(work, home, prefix, hot, pinned, copied)
    meta, rows = _rows(cp)

    assert _classes(rows[hot])["bin/toolA"] == "hot-swap"
    assert _classes(rows[pinned])["bin/toolB"] == "pin-managed"
    assert _classes(rows[copied])["bin/toolC"] == "copy-installed"
    # three distinct classes, not three spellings of one
    assert len({_classes(rows[b])[p] for b, p in
                ((hot, "bin/toolA"), (pinned, "bin/toolB"),
                 (copied, "bin/toolC"))}) == 3

    assert rows[hot]["verdict"] == "hold-expected"
    assert rows[pinned]["verdict"] == "landable-owes-activation"
    assert rows[copied]["verdict"] == "landable-owes-activation"
    assert meta["installed_artifacts"] == 3
    assert cp.returncode == 1     # a hold-expected bead is present


def test_hot_swap_evidence_names_the_real_installed_artifact(tmp_path):
    """The verdict has to be checkable by hand: it names the artifact whose
    resolution forced it, and that artifact really does resolve into the
    working tree."""
    work, home, prefix = _setup(tmp_path)
    hot = _create_bead(work, home, title="live tool", footprint="bin/toolA")

    _, rows = _rows(_screen(work, home, prefix, hot))
    installed = rows[hot]["paths"][0]["installed"]

    assert installed == [str(prefix / "toolA")]
    assert os.path.realpath(installed[0]) == \
        os.path.realpath(str(work / "bin" / "toolA"))


def test_inert_footprint_is_landable_end_to_end_and_exits_zero(tmp_path):
    """The negative control through the real CLI. Tests and docs activate
    nothing, so the screen must be quiet AND release."""
    work, home, prefix = _setup(tmp_path)
    inert = _create_bead(work, home, title="tests and docs only",
                         footprint="bin/test_toolA.py, docs/NOTES.md")

    cp = _screen(work, home, prefix, inert)
    _, rows = _rows(cp)

    assert rows[inert]["verdict"] == "landable"
    assert cp.returncode == 0

    text = _run([sys.executable, str(BIN), "activation", inert,
                 "--repo", str(work), "--install-root", str(prefix)],
                work, home, check=True).stdout
    assert "LANDABLE" in text
    assert "bin/test_toolA.py" not in text     # no path table for a clean bead


def test_worst_case_governs_end_to_end(tmp_path):
    work, home, prefix = _setup(tmp_path)
    mixed = _create_bead(
        work, home, title="one live tool among several inert files",
        footprint="bin/toolA, bin/test_toolA.py, docs/NOTES.md")

    cp = _screen(work, home, prefix, mixed)
    _, rows = _rows(cp)

    assert rows[mixed]["verdict"] == "hold-expected"
    classes = _classes(rows[mixed])
    assert classes["bin/toolA"] == "hot-swap"
    assert classes["bin/test_toolA.py"] == "inert"
    assert cp.returncode == 1


def test_import_closure_of_a_live_symlink_is_held(tmp_path):
    """bin/lib_a.py is installed by nothing, but the live-symlinked bin/toolA
    imports it, so a pull activates it. The paired control is the test file
    beside it, which stays inert."""
    work, home, prefix = _setup(tmp_path)
    lib = _create_bead(work, home, title="touches only the imported library",
                       footprint="bin/lib_a.py")
    neighbour = _create_bead(work, home, title="touches only the test beside it",
                             footprint="bin/test_toolA.py")

    _, rows = _rows(_screen(work, home, prefix, lib, neighbour))

    assert _classes(rows[lib])["bin/lib_a.py"] == "hot-swap-closure"
    assert rows[lib]["verdict"] == "hold-expected"
    assert rows[neighbour]["verdict"] == "landable"


def test_phantom_token_is_could_not_assess_not_landable(tmp_path):
    """SABLE-p0grx's real token, through the real parser: a phantom WITH a
    slash must not be reported inert, because that pushes the bead toward the
    landable list."""
    work, home, prefix = _setup(tmp_path)
    phantom = _create_bead(work, home, title="declares a phantom",
                           footprint="wk-runlock-instrument/land).")

    cp = _screen(work, home, prefix, phantom)
    _, rows = _rows(cp)

    assert rows[phantom]["verdict"] == "could-not-assess"
    # The exact token is whatever the borrowed tokenizer yields — it strips
    # trailing punctuation, so this asserts on the CLASS and on the token's
    # stem rather than re-encoding the lib's stripping rules here.
    classes = _classes(rows[phantom])
    assert list(classes.values()) == ["could-not-assess"]
    assert any(p.startswith("wk-runlock-instrument/land") for p in classes)
    assert cp.returncode == 2


def test_absent_section_dispatches_normally(tmp_path):
    work, home, prefix = _setup(tmp_path)
    silent = _create_bead(work, home, title="declares no footprint at all")

    cp = _screen(work, home, prefix, silent)
    _, rows = _rows(cp)

    assert rows[silent]["verdict"] == "no-declaration"
    assert cp.returncode == 0


def test_unknown_bead_id_is_not_found_not_landable(tmp_path):
    work, home, prefix = _setup(tmp_path)

    cp = _screen(work, home, prefix, "SABLE-nosuchbead")
    _, rows = _rows(cp)

    assert rows["SABLE-nosuchbead"]["verdict"] == "not-found"
    assert cp.returncode == 2


# ===========================================================================
# resolved, not enumerated — end to end
# ===========================================================================

def test_repinning_the_real_symlink_flips_the_real_verdict(tmp_path):
    """The strongest form of the not-enumerated guard: nothing about the repo
    or the bead changes, only the install prefix on disk, and the CLI's answer
    follows the disk."""
    work, home, prefix = _setup(tmp_path)
    bead = _create_bead(work, home, title="touches toolA", footprint="bin/toolA")

    before = _rows(_screen(work, home, prefix, bead))[1][bead]["verdict"]

    # Re-pin for real: drop the live symlink, install a plain copy instead.
    os.unlink(prefix / "toolA")
    shutil.copy2(work / "bin" / "toolA", prefix / "toolA")

    after = _rows(_screen(work, home, prefix, bead))[1][bead]["verdict"]

    assert before == "hold-expected"
    assert after == "landable-owes-activation"


def test_screen_never_touches_the_real_install_prefix(tmp_path):
    """Pollution guard. The run must report scanning ONLY the scratch prefix —
    if a default ever reaches for the developer's ~/.local/bin or ~/.claude,
    this fails here rather than on their machine."""
    work, home, prefix = _setup(tmp_path)
    bead = _create_bead(work, home, title="touches toolA", footprint="bin/toolA")

    meta, _ = _rows(_screen(work, home, prefix, bead))

    assert meta["install_roots_scanned"] == [os.path.realpath(str(prefix))]
    assert meta["install_roots_unreadable"] == []
    for scanned in meta["install_roots_scanned"]:
        assert str(tmp_path) in scanned


def test_env_seam_selects_the_install_roots(tmp_path):
    """SABLE_SCREEN_INSTALL_ROOTS is the seam every downstream suite will use
    to keep off the live prefix, so it is pinned here rather than assumed."""
    work, home, prefix = _setup(tmp_path)
    bead = _create_bead(work, home, title="touches toolA", footprint="bin/toolA")

    cp = _screen(work, home, None, bead,
                 extra_env={"SABLE_SCREEN_INSTALL_ROOTS": str(prefix)})
    meta, rows = _rows(cp)

    assert meta["install_roots_scanned"] == [os.path.realpath(str(prefix))]
    assert rows[bead]["verdict"] == "hold-expected"


def test_empty_readable_prefix_is_landable_not_degraded(tmp_path):
    """A real, readable, empty prefix really does mean 'nothing is installed'
    — the paired control for the degraded-instrument case below."""
    work, home, prefix = _setup(tmp_path)
    empty = tmp_path / "empty-prefix"
    empty.mkdir()
    bead = _create_bead(work, home, title="touches toolA", footprint="bin/toolA")

    cp = _screen(work, home, empty, bead)
    meta, rows = _rows(cp)

    assert meta["blocked"] == ""
    assert rows[bead]["verdict"] == "landable"
    assert cp.returncode == 0


def test_missing_install_root_degrades_loudly(tmp_path):
    """...and a prefix that is not there at all is a NON-ANSWER, not an empty
    one. Both polarities are exercised, so the check is shown able to fire."""
    work, home, _ = _setup(tmp_path)
    bead = _create_bead(work, home, title="touches toolA", footprint="bin/toolA")

    cp = _screen(work, home, tmp_path / "no-such-prefix", bead)
    meta, rows = _rows(cp)

    assert meta["blocked"]
    assert rows[bead]["verdict"] == "could-not-assess"
    assert cp.returncode == 2


# ===========================================================================
# pool form
# ===========================================================================

def test_ready_pool_form_screens_every_bead_in_one_bulk_read(tmp_path):
    """--ready is the form a manager uses to choose between candidates, and it
    must cover the WHOLE pool: the first hand-run of this screen shelled bd
    once per bead and never finished. Attributable identity, never a count
    (SABLE-jd5fj.15) — the specific fixture ids must all be present."""
    work, home, prefix = _setup(tmp_path)
    hot = _create_bead(work, home, title="live tool", footprint="bin/toolA")
    inert = _create_bead(work, home, title="docs only", footprint="docs/NOTES.md")
    silent = _create_bead(work, home, title="declares nothing")

    cp = _screen(work, home, prefix, "--ready")
    _, rows = _rows(cp)

    assert {hot, inert, silent} <= set(rows)
    assert rows[hot]["verdict"] == "hold-expected"
    assert rows[inert]["verdict"] == "landable"
    assert rows[silent]["verdict"] == "no-declaration"


def test_named_bead_is_not_screened_twice_by_the_pool_form(tmp_path):
    work, home, prefix = _setup(tmp_path)
    hot = _create_bead(work, home, title="live tool", footprint="bin/toolA")

    payload = json.loads(_screen(work, home, prefix, hot, "--ready").stdout)
    ids = [r["bead_id"] for r in payload["results"]]

    assert ids.count(hot) == 1


def test_no_arguments_is_a_usage_error_not_a_silent_pass(tmp_path):
    """An invocation that screens nothing must not exit 0 — that is
    indistinguishable from 'screened everything, all clear'."""
    work, home, prefix = _setup(tmp_path)

    cp = _screen(work, home, prefix)

    assert cp.returncode == 2
