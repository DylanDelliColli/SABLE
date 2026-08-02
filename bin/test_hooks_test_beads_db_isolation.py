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

Each row LOCATES the real, currently-shipped `bd init` invocation by a unique
piece of the command itself — never by a hardcoded line number and never by a
hand-retyped copy — so edits above a site cannot break the harness. A future
edit that silently drops the `-u BEADS_DB` guard is caught by failing loudly
to resolve the site, not by yielding an empty span. For each site this
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
# sable-test-load: measured-slow -- per-site controls intentionally exercise real bd init and collision paths
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HAVE_BD = shutil.which("bd") is not None


def _resolve_site(path: str | Path, selector: str, *, wrapper: bool = False) -> str:
    """Find one guarded command and return its shell command or wrapper body.

    The selector is a stable, site-specific fragment of the command (normally
    its bd prefix or target directory). Resolution is deliberately strict:
    zero or multiple guarded matches mean this harness no longer identifies a
    real site unambiguously and must fail before either test arm can run.
    """
    source = Path(path)
    if not source.is_absolute():
        source = REPO_ROOT / source
    lines = source.read_text().splitlines()
    matches = [index for index, line in enumerate(lines)
               if "env -u BEADS_DB " in line and selector in line]
    assert len(matches) == 1, (
        f"{path}: expected exactly one guarded invocation matching "
        f"{selector!r}, found {len(matches)}")
    match = matches[0]

    if wrapper:
        starts = [index for index in range(match, -1, -1)
                  if lines[index].strip().endswith("() {")]
        assert starts, f"{path}: guarded match {selector!r} has no enclosing wrapper"
        start = starts[0]
        ends = [index for index in range(match + 1, len(lines))
                if lines[index].strip() == "}"]
        assert ends, f"{path}: guarded wrapper {selector!r} has no closing brace"
        end = ends[0]
    else:
        start = match
        end = match
        while lines[end].rstrip().endswith("\\"):
            end += 1
            assert end < len(lines), (
                f"{path}: guarded invocation {selector!r} ends in a dangling continuation")
    return "\n".join(lines[start:end + 1])


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


class _AmbientBdTemplate:
    """One immutable real-bd baseline, deep-copied for each collision arm.

    The old fixture paid for a fresh ``bd init`` on every call: 37 real inits
    in this 28-case module.  The ambient store is evidence, not the subject of
    any mutation, so building that evidence once and copying it preserves the
    test contract while deleting manufactured integration load.  Copies stay
    per-case because sharing a writable Dolt directory would trade runtime for
    order dependence.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.beads_dir = workspace / ".beads"

    @classmethod
    def create(cls, parent: Path) -> "_AmbientBdTemplate":
        workspace = parent / "decoy-ambient"
        workspace.mkdir(parents=True)
        env = dict(os.environ)
        env.pop("BEADS_DB", None)
        env["BD_NON_INTERACTIVE"] = "1"
        cp = subprocess.run(
            ["bd", "init", "--prefix=decoy"], cwd=str(workspace), env=env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        assert (workspace / ".beads").is_dir(), (
            f"decoy ambient DB setup itself failed: {cp.stdout}"
        )
        return cls(workspace)

    def copy_into(self, parent: Path) -> str:
        destination = parent / "decoy-ambient"
        shutil.copytree(self.workspace, destination)
        beads_dir = destination / ".beads"
        assert beads_dir.is_dir(), f"copied decoy DB is absent: {beads_dir}"
        return str(beads_dir)


@pytest.fixture(scope="session")
def ambient_bd_template(tmp_path_factory):
    """Build the real ambient-collision evidence exactly once per worker."""
    return _AmbientBdTemplate.create(
        tmp_path_factory.mktemp("ambient-beads-template")
    )


def _decoy_ambient_beads_db(
        tmp_path: Path, template: _AmbientBdTemplate) -> str:
    """Give one test arm an independent copy of the initialized ambient DB."""
    return template.copy_into(tmp_path)


def test_decoy_template_initializes_real_bd_once_then_copies_without_more_inits(
        tmp_path, monkeypatch):
    """The optimization must delete real setup, not hide it behind another API."""
    real_run = subprocess.run
    init_argv = []

    def recording_run(argv, *args, **kwargs):
        if list(argv[:2]) == ["bd", "init"]:
            init_argv.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    template = _AmbientBdTemplate.create(tmp_path / "template")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = template.copy_into(first_root)
    second = template.copy_into(second_root)

    assert len(init_argv) == 1, init_argv
    assert Path(first).is_dir() and Path(second).is_dir()


def test_decoy_template_copies_share_no_mutable_store_files(
        ambient_bd_template, tmp_path):
    """Per-case copies must not trade repeated setup for cross-test aliasing."""
    left_root = tmp_path / "left"
    right_root = tmp_path / "right"
    left_root.mkdir()
    right_root.mkdir()
    left = Path(ambient_bd_template.copy_into(left_root))
    right = Path(ambient_bd_template.copy_into(right_root))

    compared = 0
    for left_file in left.rglob("*"):
        if not left_file.is_file():
            continue
        right_file = right / left_file.relative_to(left)
        if right_file.is_file():
            compared += 1
            assert not os.path.samefile(left_file, right_file), left_file
    assert compared > 0, "copy-isolation probe compared no real bd files"

    left_config = left / "config.yaml"
    right_config = right / "config.yaml"
    template_config = ambient_bd_template.beads_dir / "config.yaml"
    before_right = right_config.read_bytes()
    before_template = template_config.read_bytes()
    left_config.write_text(left_config.read_text() + "# left-only mutation\n")
    assert right_config.read_bytes() == before_right
    assert template_config.read_bytes() == before_template


def test_copied_decoy_still_fires_on_an_unguarded_ambient_consumer(
        ambient_bd_template, tmp_path):
    """Plant the missing guard and require the copied ambient DB to catch it."""
    decoy_root = tmp_path / "decoy"
    own_root = tmp_path / "unguarded-site"
    decoy_root.mkdir()
    own_root.mkdir()
    ambient = ambient_bd_template.copy_into(decoy_root)
    cp = _run_under_ambient_beads_db(
        f'cd "{own_root}" && bd init --prefix=unguarded', ambient,
    )

    assert not (own_root / ".beads").exists(), (
        "the planted unguarded consumer built its own DB, so the ambient "
        "collision arm did not fire"
    )
    assert "already initialized" in cp.stdout.lower(), cp.stdout


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


# Each row: (id, file, selector, setup — shell that binds the site's own
# scratch-dir var(s) fresh, check — the file this site's OWN init must
# produce, capture_var — the shell var this site's own line captures bd's
# combined output into via `$(... 2>&1)`, or None when the site instead
# redirects straight to /dev/null). `setup` intentionally does NOT
# reproduce each file's surrounding fixture logic (tmux sessions, git
# repos, ...) — only what the extracted init line itself reads (its target
# directory variable(s)) — the extracted line is the thing under test, not
# the rest of the suite.
_DIRECT_SITES = [
    ("overlap-dispatch-e2e", "hooks/test/test-overlap-dispatch-e2e.sh", "--prefix=sable",
     'BEADS_ROOT="$(mktemp -d)/beads"; mkdir -p "$BEADS_ROOT"', "$BEADS_ROOT/.beads",
     "BD_INIT_OUT"),
    ("landing-pair-gate", "hooks/test/test-landing-pair-gate.sh", "--prefix=lpg",
     'BEADS_ROOT="$(mktemp -d)/beads"; mkdir -p "$BEADS_ROOT"', "$BEADS_ROOT/.beads",
     "INIT_OUT"),
    ("seat-sighting", "hooks/test/test-seat-sighting.sh", "--prefix=seat",
     'BEADS_ROOT="$(mktemp -d)/beads"; mkdir -p "$BEADS_ROOT"', "$BEADS_ROOT/.beads",
     "INIT_OUT"),
    ("bd-inline-body-guard", "hooks/test/test-bd-inline-body-guard.sh", "--prefix=ibg",
     'SCRATCH_BEADS_DIR="$(mktemp -d)"', "$SCRATCH_BEADS_DIR/.beads", None),
    ("worktree-placement-guard", "hooks/test/test-worktree-placement-guard.sh", "--prefix=wpg",
     'MAIN="$(mktemp -d)/main"; mkdir -p "$MAIN"', "$MAIN/.beads", None),
    ("tmux-e2e", "hooks/test/test-tmux-e2e.sh", "--prefix=e2e",
     'SCRATCH_BEADS_DIR="$(mktemp -d)"', "$SCRATCH_BEADS_DIR/.beads", None),
    ("sable-msg-sandbox", "hooks/test/test-sable-msg.sh", "--prefix=sbx",
     'SCRATCH_BEADS_DIR="$(mktemp -d)"', "$SCRATCH_BEADS_DIR/.beads", None),
    ("sable-msg-pretend-live", "hooks/test/test-sable-msg.sh", "--prefix=plv",
     'PRETEND_LIVE_DIR="$(mktemp -d)"', "$PRETEND_LIVE_DIR/.beads", None),
    ("worker-flag-done", "hooks/test/test-worker-flag-done.sh", "--prefix=wfd",
     'SCRATCH_BEADS_DIR="$(mktemp -d)"', "$SCRATCH_BEADS_DIR/.beads", None),
    ("tier-budget-bead", "hooks/test/test-tier-budget-bead.sh", "bd init --non-interactive",
     'REPO_DIR="$(mktemp -d)"; BD_HOME="$(mktemp -d)"', "$REPO_DIR/.beads/config.yaml", None),
    ("bead-description-gate", "hooks/test/test-bead-description-gate.sh", "ORIGIN_REPO_DIR",
     'ORIGIN_REPO_DIR="$(mktemp -d)"; ORIGIN_BD_HOME="$(mktemp -d)"',
     "$ORIGIN_REPO_DIR/.beads/config.yaml", None),
    ("edit-write-claim-reconciler", "hooks/test/test-edit-write-claim-reconciler.sh",
     "--prefix=task",
     'FIXTURE_DIR="$(mktemp -d)"; EWCR_BD_ROOT="$FIXTURE_DIR/real-bd"; mkdir -p "$EWCR_BD_ROOT"',
     "$EWCR_BD_ROOT/.beads", "EWCR_BD_INIT_OUT"),
    ("sable-test", "hooks/test/test-sable-test.sh", "--prefix=stint",
     'STUB_DIR="$(mktemp -d)"; SABLE_TEST_BD_ROOT="$STUB_DIR/real-bd"; mkdir -p "$SABLE_TEST_BD_ROOT"',
     "$SABLE_TEST_BD_ROOT/.beads", "SABLE_TEST_BD_INIT_OUT"),
    ("tdd-gate", "hooks/test/test-tdd-gate.sh", "--prefix=tddg",
     'STUB_DIR="$(mktemp -d)"; TDD_GATE_BD_ROOT="$STUB_DIR/real-bd"; mkdir -p "$TDD_GATE_BD_ROOT"',
     "$TDD_GATE_BD_ROOT/.beads", "TDD_GATE_BD_INIT_OUT"),
]

# The two `bd_in_sandbox() { ... }` wrapper sites: the guard lives in the
# function DEFINITION (every call through it must be isolated, not just
# `init`), so these extract the whole function body and call it.
_WRAPPER_SITES = [
    ("tier-budget-bead-wrapper", "hooks/test/test-tier-budget-bead.sh", 'bd "$@"',
     'REPO_DIR="$(mktemp -d)"; BD_HOME="$(mktemp -d)"',
     "bd_in_sandbox init --non-interactive", "$REPO_DIR/.beads/config.yaml"),
    ("snapshot-freeze-wrapper", "hooks/test/test-snapshot-freeze.sh", 'bd "$@"',
     'W5="$(mktemp -d)"; BD_HOME="$(mktemp -d)"',
     "bd_in_sandbox init --non-interactive", "$W5/.beads/config.yaml"),
]


def test_site_resolver_finds_every_declared_site_without_line_coordinates():
    for _site_id, path, selector, *_rest in _DIRECT_SITES:
        resolved = _resolve_site(path, selector)
        assert "env -u BEADS_DB " in resolved
    for _site_id, path, selector, *_rest in _WRAPPER_SITES:
        resolved = _resolve_site(path, selector, wrapper=True)
        assert "env -u BEADS_DB " in resolved


def test_site_resolver_fails_loudly_when_guarded_invocation_is_absent(tmp_path):
    no_guard = tmp_path / "no-guard.sh"
    no_guard.write_text("bd init --prefix=missing\n")
    with pytest.raises(AssertionError, match="found 0"):
        _resolve_site(no_guard, "--prefix=missing")


@pytest.mark.skipif(not HAVE_BD, reason="requires a real bd on PATH")
@pytest.mark.parametrize("site_id,path,selector,setup,check_path,capture_var", _DIRECT_SITES,
                         ids=[s[0] for s in _DIRECT_SITES])
def test_shipped_init_builds_its_own_db_despite_ambient_beads_db(
        tmp_path, ambient_bd_template,
        site_id, path, selector, setup, check_path, capture_var):
    """GREEN arm: the line as shipped isolates correctly."""
    ambient = _decoy_ambient_beads_db(tmp_path, ambient_bd_template)
    extracted = _resolve_site(path, selector)
    script = f'{setup}\n{extracted}\necho "OWN_DB_EXISTS=$([ -e "{check_path}" ] && echo yes || echo no)"'
    cp = _run_under_ambient_beads_db(script, ambient)
    assert "OWN_DB_EXISTS=yes" in cp.stdout, (
        f"{site_id}: the SHIPPED init line failed to build its own DB under an "
        f"ambient BEADS_DB — the guard is missing or broken: {cp.stdout!r}")


@pytest.mark.skipif(not HAVE_BD, reason="requires a real bd on PATH")
@pytest.mark.parametrize("site_id,path,selector,setup,check_path,capture_var", _DIRECT_SITES,
                         ids=[s[0] for s in _DIRECT_SITES])
def test_stripping_the_guard_reproduces_the_reported_collision(
        tmp_path, ambient_bd_template,
        site_id, path, selector, setup, check_path, capture_var):
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
    ambient = _decoy_ambient_beads_db(tmp_path, ambient_bd_template)
    unguarded = _strip_guard(_resolve_site(path, selector))

    script = f'{setup}\n{unguarded}\necho "OWN_DB_EXISTS=$([ -e "{check_path}" ] && echo yes || echo no)"'
    cp = _run_under_ambient_beads_db(script, ambient)
    assert "OWN_DB_EXISTS=no" in cp.stdout, (
        f"{site_id}: stripping the guard should have reproduced the reported "
        f"collision (own DB never built) — instead: {cp.stdout!r}. If this "
        f"assertion fails, the reproduction harness itself is unsound for "
        f"this site, not merely 'still green'.")

    diag_root = tmp_path / "diag"
    diag_root.mkdir()
    ambient2 = _decoy_ambient_beads_db(diag_root, ambient_bd_template)
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
@pytest.mark.parametrize("site_id,path,selector,setup,call,check_path", _WRAPPER_SITES,
                         ids=[s[0] for s in _WRAPPER_SITES])
def test_wrapper_shipped_definition_builds_its_own_db_despite_ambient_beads_db(
        tmp_path, ambient_bd_template,
        site_id, path, selector, setup, call, check_path):
    ambient = _decoy_ambient_beads_db(tmp_path, ambient_bd_template)
    extracted = _resolve_site(path, selector, wrapper=True)
    script = f'{setup}\n{extracted}\n{call}\necho "OWN_DB_EXISTS=$([ -e "{check_path}" ] && echo yes || echo no)"'
    cp = _run_under_ambient_beads_db(script, ambient)
    assert "OWN_DB_EXISTS=yes" in cp.stdout, (
        f"{site_id}: the SHIPPED wrapper failed to build its own DB under an "
        f"ambient BEADS_DB — the guard is missing or broken: {cp.stdout!r}")


@pytest.mark.skipif(not HAVE_BD, reason="requires a real bd on PATH")
@pytest.mark.parametrize("site_id,path,selector,setup,call,check_path", _WRAPPER_SITES,
                         ids=[s[0] for s in _WRAPPER_SITES])
def test_wrapper_stripping_the_guard_reproduces_the_reported_collision(
        tmp_path, ambient_bd_template,
        site_id, path, selector, setup, call, check_path):
    ambient = _decoy_ambient_beads_db(tmp_path, ambient_bd_template)
    unguarded = _strip_guard(_resolve_site(path, selector, wrapper=True))
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
