#!/usr/bin/env python3
"""SABLE-sx1rb, per-site plant-and-fail: every hooks/test/*.sh call site that
builds its OWN isolated bd DB must not inherit an ambient BEADS_DB.

WHY A STANDALONE SUITE RUN PROVES NOTHING ABOUT THIS DEFECT (per optimus,
2026-07-27): nothing outside the impact tier ever sets BEADS_DB. Every one of
these suites passed every hand-run before this fix, and passes every
hand-run after it, whether the fix is right, wrong, or absent — that
invisibility standalone is exactly why the defect survived in the tree. The
only condition that discriminates is an AMBIENT, ALREADY-INITIALIZED
BEADS_DB — the exact thing bin/sable_gate_promote_lib.py's
_impact_isolated_env exports at :1284 before running any selected suite.

So this file reproduces that inheritance directly against each real site,
rather than running the suites standalone (see bin/test_sable_gate_promote_
integration.py for the complementary proof driven through the actual tier
object, promote_lib.run_impact_tier, for one representative pattern —
guarded GREEN twice in a row, unguarded reproducing the reported collision).

Each row EXTRACTS the real, currently-shipped `bd init` invocation straight
out of the named file at the named line range — never a hand-retyped copy —
so a future edit that silently drops the `-u BEADS_DB` guard is caught by
re-extracting the CURRENT text, not a frozen fixture. For each site this
proves BOTH directions against a real `bd`, under a real exported BEADS_DB
pointing at an already-initialized decoy workspace:

  * the extracted (shipped) line, AS SHIPPED, builds the site's own isolated
    DB despite the ambient var — GREEN.
  * the SAME extracted text with the guard mechanically stripped (the
    literal substring "-u BEADS_DB " removed) fails to build it, reproducing
    the exact "already initialized" collision reported on SABLE-r0mzn /
    SABLE-6eu9w — RED. If this arm does not go red, the reproduction itself
    is wrong, and the assertion says so rather than banking a vacuous green
    (the negative-control discipline the bead's own test spec requires).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HAVE_BD = shutil.which("bd") is not None


def _extract(path: str, start: int, end: int) -> str:
    """1-indexed, inclusive — the exact shipped text, re-read every run."""
    lines = (REPO_ROOT / path).read_text().splitlines()
    span = lines[start - 1:end]
    assert span, f"{path}:{start}-{end} extracted nothing — line numbers drifted"
    return "\n".join(span)


def _strip_comment_lines(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))


def _strip_guard(text: str) -> str:
    """Strip the FUNCTIONAL guard ("env -u BEADS_DB " -> "env ") and PROVE
    the plant actually took effect before handing the result back — a strip
    that is a silent no-op must fail LOUDLY here, never fall through to
    running an unchanged (still-guarded) command and letting the caller
    misread its GREEN as a demonstrated RED.

    THIS IS NOT HYPOTHETICAL (optimus, 2026-07-27): an earlier version of
    this function did a naive first-match replace on the bare "-u BEADS_DB "
    token. Two wrapper-site extraction spans include this test file's OWN
    explanatory comment ("# -u BEADS_DB (SABLE-sx1rb): ...") ahead of the
    real `env -u BEADS_DB ...` line, and the first-match replace hit the
    COMMENT instead of the code — producing two false GREENs on what should
    have been the RED arm. That bug is the exact enumerate-a-spelling-
    instead-of-the-property defect this whole bead is about, reproduced
    inside the instrument built to catch it. The fix is not a narrower
    regex (one clever comment away from the same failure) — it is this
    function refusing to return a "stripped" result unless it can prove,
    on the EXECUTABLE (non-comment) text specifically, that the guard is
    actually gone.

    A NEGATIVE CONTROL MUST PROVE IT ACTUALLY CREATED THE NEGATIVE
    CONDITION BEFORE IT IS ENTITLED TO INTERPRET THE RESULT."""
    occurrences = text.count("env -u BEADS_DB ")
    assert occurrences >= 1, (
        f"expected at least one 'env -u BEADS_DB ' occurrence in the "
        f"extracted text, found none — the guard may have moved or been "
        f"reworded, so this plant cannot be constructed:\n{text}")
    stripped = text.replace("env -u BEADS_DB ", "env ", occurrences)
    assert stripped != text, (
        f"strip produced byte-identical text — the plant did not take "
        f"effect and must not be run:\n{text}")
    assert "env -u BEADS_DB " not in stripped, (
        f"the functional guard token still appears after stripping — the "
        f"plant is incomplete and must not be run:\n{stripped}")
    # Prove the guard is gone from the EXECUTABLE text specifically, not
    # merely absent from one literal substring match — strip comment lines
    # from a copy and require the bare token to be absent there too. This
    # is what actually would have caught the comment-collision bug above:
    # a stripped-but-still-guarded command has "-u BEADS_DB" surviving in
    # its non-comment lines even when the (wrong) occurrence removed was a
    # comment's.
    executable = _strip_comment_lines(stripped)
    assert "-u BEADS_DB" not in executable, (
        f"the guard is still present in the EXECUTABLE (non-comment) text "
        f"after stripping — the plant is incomplete and must not be run:\n"
        f"{executable}")
    return stripped


def _decoy_ambient_beads_db(tmp_path: Path) -> str:
    """An already-initialized workspace, distinct from anything a site under
    test will build — reproducing the tier's own already-initialized DB
    that a second, unguarded `bd init` collides with."""
    decoy = tmp_path / "decoy-ambient"
    decoy.mkdir()
    cp = subprocess.run(["bd", "init", "--prefix=decoy"], cwd=str(decoy),
                        env={**os.environ, "BD_NON_INTERACTIVE": "1"},
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert (decoy / ".beads").is_dir(), f"decoy ambient DB setup itself failed: {cp.stdout}"
    return str(decoy / ".beads")


def _run_under_ambient_beads_db(script: str, ambient_beads_db: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "BEADS_DB": ambient_beads_db}
    return subprocess.run(["bash", "-c", "set -u\n" + script], env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)


def _diagnostic_variant(text: str, capture_var: str | None) -> str:
    """A variant of an (unguarded) init command that surfaces bd's own
    stderr/stdout directly, for asserting the REPORTED collision signature
    independently of whether the real site's own form discards it.

    Several real sites deliberately redirect `bd init`'s output to
    /dev/null (they only check whether their own DB directory got built,
    never print bd's message) or capture it into a shell variable this
    harness's outer echo never reads. Neither is a defect in the site — it
    is simply not the thing that proves the abort HAPPENED. This strips a
    trailing `>/dev/null 2>&1` so bd's own text reaches this process's
    stdout, and echoes `capture_var` when the site captures into one."""
    diag = text.replace(" >/dev/null 2>&1", "")
    if capture_var:
        diag += f'\necho "${capture_var}"'
    return diag


# Each row: (id, file, start, end, setup — shell that binds the site's own
# scratch-dir var(s) fresh, check — the file this site's OWN init must
# produce, capture_var — the shell var this site's own line captures bd's
# combined output into via `$(... 2>&1)`, or None when the site instead
# redirects straight to /dev/null). `setup` intentionally does NOT
# reproduce each file's surrounding fixture logic (tmux sessions, git
# repos, ...) — only what the extracted init line itself reads (its target
# directory variable(s)) — the extracted line is the thing under test, not
# the rest of the suite.
_DIRECT_SITES = [
    ("overlap-dispatch-e2e:110", "hooks/test/test-overlap-dispatch-e2e.sh", 110, 110,
     'BEADS_ROOT="$(mktemp -d)/beads"; mkdir -p "$BEADS_ROOT"', "$BEADS_ROOT/.beads",
     "BD_INIT_OUT"),
    ("landing-pair-gate:119", "hooks/test/test-landing-pair-gate.sh", 119, 119,
     'BEADS_ROOT="$(mktemp -d)/beads"; mkdir -p "$BEADS_ROOT"', "$BEADS_ROOT/.beads",
     "INIT_OUT"),
    ("seat-sighting:56", "hooks/test/test-seat-sighting.sh", 56, 56,
     'BEADS_ROOT="$(mktemp -d)/beads"; mkdir -p "$BEADS_ROOT"', "$BEADS_ROOT/.beads",
     "INIT_OUT"),
    ("bd-inline-body-guard:134-135", "hooks/test/test-bd-inline-body-guard.sh", 134, 135,
     'SCRATCH_BEADS_DIR="$(mktemp -d)"', "$SCRATCH_BEADS_DIR/.beads", None),
    ("worktree-placement-guard:195-196", "hooks/test/test-worktree-placement-guard.sh", 195, 196,
     'MAIN="$(mktemp -d)/main"; mkdir -p "$MAIN"', "$MAIN/.beads", None),
    ("tmux-e2e:49-50", "hooks/test/test-tmux-e2e.sh", 49, 50,
     'SCRATCH_BEADS_DIR="$(mktemp -d)"', "$SCRATCH_BEADS_DIR/.beads", None),
    ("sable-msg:262-263", "hooks/test/test-sable-msg.sh", 262, 263,
     'SCRATCH_BEADS_DIR="$(mktemp -d)"', "$SCRATCH_BEADS_DIR/.beads", None),
    ("sable-msg:378-379", "hooks/test/test-sable-msg.sh", 378, 379,
     'PRETEND_LIVE_DIR="$(mktemp -d)"', "$PRETEND_LIVE_DIR/.beads", None),
    ("worker-flag-done:119-120", "hooks/test/test-worker-flag-done.sh", 119, 120,
     'SCRATCH_BEADS_DIR="$(mktemp -d)"', "$SCRATCH_BEADS_DIR/.beads", None),
    ("tier-budget-bead:89", "hooks/test/test-tier-budget-bead.sh", 89, 89,
     'REPO_DIR="$(mktemp -d)"; BD_HOME="$(mktemp -d)"', "$REPO_DIR/.beads/config.yaml", None),
    ("bead-description-gate:538", "hooks/test/test-bead-description-gate.sh", 538, 538,
     'ORIGIN_REPO_DIR="$(mktemp -d)"; ORIGIN_BD_HOME="$(mktemp -d)"',
     "$ORIGIN_REPO_DIR/.beads/config.yaml", None),
]

# The two `bd_in_sandbox() { ... }` wrapper sites: the guard lives in the
# function DEFINITION (every call through it must be isolated, not just
# `init`), so these extract the whole function body and call it.
_WRAPPER_SITES = [
    ("tier-budget-bead:wrapper:100-104", "hooks/test/test-tier-budget-bead.sh", 100, 104,
     'REPO_DIR="$(mktemp -d)"; BD_HOME="$(mktemp -d)"',
     "bd_in_sandbox init --non-interactive", "$REPO_DIR/.beads/config.yaml"),
    ("snapshot-freeze:wrapper:444-449", "hooks/test/test-snapshot-freeze.sh", 444, 449,
     'W5="$(mktemp -d)"; BD_HOME="$(mktemp -d)"',
     "bd_in_sandbox init --non-interactive", "$W5/.beads/config.yaml"),
]


@pytest.mark.skipif(not HAVE_BD, reason="requires a real bd on PATH")
@pytest.mark.parametrize("site_id,path,start,end,setup,check_path,capture_var", _DIRECT_SITES,
                         ids=[s[0] for s in _DIRECT_SITES])
def test_shipped_init_builds_its_own_db_despite_ambient_beads_db(
        tmp_path, site_id, path, start, end, setup, check_path, capture_var):
    """GREEN arm: the line as shipped isolates correctly."""
    ambient = _decoy_ambient_beads_db(tmp_path)
    extracted = _extract(path, start, end)
    script = f'{setup}\n{extracted}\necho "OWN_DB_EXISTS=$([ -e "{check_path}" ] && echo yes || echo no)"'
    cp = _run_under_ambient_beads_db(script, ambient)
    assert "OWN_DB_EXISTS=yes" in cp.stdout, (
        f"{site_id}: the SHIPPED init line failed to build its own DB under an "
        f"ambient BEADS_DB — the guard is missing or broken: {cp.stdout!r}")


@pytest.mark.skipif(not HAVE_BD, reason="requires a real bd on PATH")
@pytest.mark.parametrize("site_id,path,start,end,setup,check_path,capture_var", _DIRECT_SITES,
                         ids=[s[0] for s in _DIRECT_SITES])
def test_stripping_the_guard_reproduces_the_reported_collision(
        tmp_path, site_id, path, start, end, setup, check_path, capture_var):
    """RED arm, the negative control: proves the GREEN arm above is not
    vacuous. The SAME extracted text with `-u BEADS_DB ` mechanically
    removed must fail to build its own DB — it inherits the ambient one
    instead, exactly like the pre-fix suites did in the wild.

    Two independent proofs, both required: (1) the site's OWN check (its DB
    directory never appears — the behavior a caller of the real suite would
    observe), run through the EXACT unguarded form; (2) the REPORTED
    signature text itself ("This workspace is already initialized"),
    observed through a diagnostic variant that surfaces bd's own
    stderr/stdout independently of whether the real site's own form
    discards it into /dev/null or an uninspected capture variable — a
    site's own output-hiding is not license to skip verifying WHY it
    failed."""
    ambient = _decoy_ambient_beads_db(tmp_path)
    unguarded = _strip_guard(_extract(path, start, end))

    script = f'{setup}\n{unguarded}\necho "OWN_DB_EXISTS=$([ -e "{check_path}" ] && echo yes || echo no)"'
    cp = _run_under_ambient_beads_db(script, ambient)
    assert "OWN_DB_EXISTS=no" in cp.stdout, (
        f"{site_id}: stripping the guard should have reproduced the reported "
        f"collision (own DB never built) — instead: {cp.stdout!r}. If this "
        f"assertion fails, the reproduction harness itself is unsound for "
        f"this site, not merely 'still green'.")

    diag_root = tmp_path / "diag"
    diag_root.mkdir()
    ambient2 = _decoy_ambient_beads_db(diag_root)
    diag_script = f'{setup}\n{_diagnostic_variant(unguarded, capture_var)}'
    diag_cp = _run_under_ambient_beads_db(diag_script, ambient2)
    assert "already initialized" in diag_cp.stdout.lower(), (
        f"{site_id}: RED, but not the REPORTED signature — expected bd's own "
        f"'This workspace is already initialized' abort naming the collision, "
        f"got: {diag_cp.stdout!r}")
    signature_line = next((ln for ln in diag_cp.stdout.splitlines()
                           if "already initialized" in ln.lower()), "")
    print(f"RED evidence [{site_id}]: {signature_line.strip()}")


@pytest.mark.skipif(not HAVE_BD, reason="requires a real bd on PATH")
@pytest.mark.parametrize("site_id,path,start,end,setup,call,check_path", _WRAPPER_SITES,
                         ids=[s[0] for s in _WRAPPER_SITES])
def test_wrapper_shipped_definition_builds_its_own_db_despite_ambient_beads_db(
        tmp_path, site_id, path, start, end, setup, call, check_path):
    ambient = _decoy_ambient_beads_db(tmp_path)
    extracted = _extract(path, start, end)
    script = f'{setup}\n{extracted}\n{call}\necho "OWN_DB_EXISTS=$([ -e "{check_path}" ] && echo yes || echo no)"'
    cp = _run_under_ambient_beads_db(script, ambient)
    assert "OWN_DB_EXISTS=yes" in cp.stdout, (
        f"{site_id}: the SHIPPED wrapper failed to build its own DB under an "
        f"ambient BEADS_DB — the guard is missing or broken: {cp.stdout!r}")


@pytest.mark.skipif(not HAVE_BD, reason="requires a real bd on PATH")
@pytest.mark.parametrize("site_id,path,start,end,setup,call,check_path", _WRAPPER_SITES,
                         ids=[s[0] for s in _WRAPPER_SITES])
def test_wrapper_stripping_the_guard_reproduces_the_reported_collision(
        tmp_path, site_id, path, start, end, setup, call, check_path):
    ambient = _decoy_ambient_beads_db(tmp_path)
    unguarded = _strip_guard(_extract(path, start, end))
    script = f'{setup}\n{unguarded}\n{call}\necho "OWN_DB_EXISTS=$([ -e "{check_path}" ] && echo yes || echo no)"'
    cp = _run_under_ambient_beads_db(script, ambient)
    assert "OWN_DB_EXISTS=no" in cp.stdout, (
        f"{site_id}: stripping the guard should have reproduced the reported "
        f"collision — instead: {cp.stdout!r}")
    assert "already initialized" in cp.stdout.lower(), (
        f"{site_id}: RED, but not the REPORTED signature — expected bd's own "
        f"'This workspace is already initialized' abort naming the collision, "
        f"got: {cp.stdout!r}")
    signature_line = next((ln for ln in cp.stdout.splitlines()
                           if "already initialized" in ln.lower()), "")
    print(f"RED evidence [{site_id}]: {signature_line.strip()}")
