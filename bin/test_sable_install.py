#!/usr/bin/env python3
"""Unit + integration tests for install.sh's settings.json contract (SABLE-gxsji).

WHAT WENT WRONG, because it decides the shape of every test below. install.sh
promised in source that it did not edit settings.json. Its step-6 delegate,
bin/sable-orchestration-install, merged hook rows into that same file on every
ordinary run. On 2026-07-23 an ordinary install merged a PreToolUse row — an
interceptor that runs BEFORE EVERY TOOL CALL OF EVERY AGENT — with nobody
having decided to turn it on and nothing in the install output naming it.

*** THE DETECTION WAS AN ACCIDENT OF THE PAYLOAD BEING DEFECTIVE. *** That hook
could not load its own library, so it announced COULD-NOT-ASSESS on every
command. A working payload emits nothing, and would still be intercepting every
tool call in the fleet, unnoticed. So the tests here are not "does the merge
work" — they are "can an install change settings.json without saying so", and
the load-bearing ones are the NEGATIVE CONTROLS: a reporter that always claims
"changed" passes every positive case vacuously, and a reporter that always
claims "unchanged" is exactly the pre-fix behaviour wearing a green light.

TWO HALVES, DELIBERATELY IN ONE FILE. The bead's declared file footprint is
install.sh + this file, so the usual repo split (test_x.py / test_x_integration.py)
would put half the deliverable outside the footprint. Unit tests source the
settings-guard block out of install.sh between its sentinels; integration tests
run the REAL install.sh into a sandboxed HOME with no mocks at all.

*** THE SANDBOX IS THE MOST DANGEROUS PART OF THIS SUITE AND IT IS CHECKED
BEFORE ANYTHING RUNS. *** install.sh derives every destination from $HOME, and
this suite's subject IS settings.json — an unsandboxed run would rewire the
running fleet's tool-call interception while workers are live. Precedent is not
theoretical (SABLE-2avau: an installer silently targeting the live ~/.claude;
SABLE-k0nvp: a suite polluting the real ~/.local twice; SABLE-33hw3: --dir
sandboxes only the bin dir). So: HOME and CLAUDE_USER_DIR are both exported to
a tmp_path, the installer is made to STATE its own target dir under --dry-run
before any writing run, and a fingerprint tripwire on the real ~/.claude and
~/.local/bin brackets every integration test. A test that cannot prove it is
sandboxed does not run.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"
DELEGATE = REPO / "bin" / "sable-orchestration-install"

BEGIN_SENTINEL = "# --- BEGIN settings-guard (SABLE-gxsji) ---"
END_SENTINEL = "# --- END settings-guard (SABLE-gxsji) ---"

# Captured at import, while HOME is still the operator's real one. Every
# integration test asserts these are untouched.
REAL_HOME = Path(os.path.expanduser("~"))
REAL_SETTINGS = REAL_HOME / ".claude" / "settings.json"
REAL_LOCAL_BIN = REAL_HOME / ".local" / "bin"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="install.sh is bash and its reporter is python3; neither is in this clean room",
)


# --------------------------------------------------------------------------
# Sourcing the guard out of install.sh
# --------------------------------------------------------------------------

def install_sh_source():
    return INSTALL_SH.read_text(encoding="utf-8")


def guard_block():
    """The sentinel-delimited settings-guard block, verbatim.

    Sourcing the real block (rather than re-implementing it here) is the point:
    a copy in the test file would keep passing after the shipped code changed,
    which is the same drift that produced this bead.
    """
    src = install_sh_source()
    assert BEGIN_SENTINEL in src, f"{INSTALL_SH} lost its {BEGIN_SENTINEL!r} sentinel"
    assert END_SENTINEL in src, f"{INSTALL_SH} lost its {END_SENTINEL!r} sentinel"
    body = src.split(BEGIN_SENTINEL, 1)[1].split(END_SENTINEL, 1)[0]
    assert "settings_report_mutations()" in body, "guard block no longer defines the reporter"
    return body


@pytest.fixture(scope="module")
def guard_sh(tmp_path_factory):
    path = tmp_path_factory.mktemp("guard") / "settings-guard.sh"
    path.write_text("#!/usr/bin/env bash\nset -u\n" + guard_block(), encoding="utf-8")
    return path


def call_guard(guard_sh, func, *args):
    """Run one guard function; return (returncode, stdout)."""
    script = f'source "{guard_sh}"; {func} "$@"'
    proc = subprocess.run(
        ["bash", "-c", script, "_", *[str(a) for a in args]],
        capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout


def report(guard_sh, tmp_path, pre, post, label="settings.json"):
    """settings_report_mutations over two in-memory settings docs.

    `pre`/`post` are dicts, or None to mean "the file is absent".
    """
    pre_path = tmp_path / "pre.json"
    post_path = tmp_path / "post.json"
    for path, doc in ((pre_path, pre), (post_path, post)):
        if doc is None:
            if path.exists():
                path.unlink()
        elif isinstance(doc, str):
            path.write_text(doc, encoding="utf-8")
        else:
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return call_guard(guard_sh, "settings_report_mutations", pre_path, post_path, label)


def hooks_doc(*rows, **extra):
    """Build a settings doc. Each row is (event, matcher, command, timeout)."""
    hooks = {}
    for event, matcher, command, timeout in rows:
        blocks = hooks.setdefault(event, [])
        block = next((b for b in blocks if b["matcher"] == matcher), None)
        if block is None:
            block = {"matcher": matcher, "hooks": []}
            blocks.append(block)
        block["hooks"].append({"type": "command", "command": command, "timeout": timeout})
    doc = {"hooks": hooks}
    doc.update(extra)
    return doc


GUARD_ROW = ("PreToolUse", "Bash", "bash /home/x/.claude/hooks/multi-manager/inline-body-guard.sh", 3000)
BEAD_ROW = ("PostToolUse", "Bash", "bash /home/x/.claude/hooks/bead-on-failure.sh", 5000)


# --------------------------------------------------------------------------
# UNIT — the mutation reporter, both polarities
# --------------------------------------------------------------------------

def test_identical_settings_report_no_mutation(guard_sh, tmp_path):
    """NEGATIVE CONTROL, and the load-bearing one.

    An install that legitimately changes nothing must not claim it changed
    something. Without this, a reporter that prints "ADDED ..." unconditionally
    passes every positive assertion in this file.
    """
    doc = hooks_doc(GUARD_ROW, BEAD_ROW)
    rc, out = report(guard_sh, tmp_path, doc, doc)
    assert rc == 1, f"reporter claimed a mutation between identical files:\n{out}"
    assert out.strip() == "", out


def test_reserialized_settings_report_no_mutation(guard_sh, tmp_path):
    """The comparison must be SEMANTIC, not byte-for-byte.

    The delegate rewrites the whole file with json.dumps(indent=2) on every run,
    so a byte comparison would report a mutation every single time and the
    "changed nothing" outcome would be unreachable — a check that cannot return
    negative is indistinguishable from one that is broken.
    """
    doc = hooks_doc(GUARD_ROW, BEAD_ROW)
    compact = json.dumps(doc, separators=(",", ":"))
    rc, out = report(guard_sh, tmp_path, compact, json.dumps(doc, indent=4) + "\n\n")
    assert rc == 1, f"reporter mistook re-serialization for a change:\n{out}"


def test_added_hook_row_is_reported_and_named(guard_sh, tmp_path):
    rc, out = report(guard_sh, tmp_path, hooks_doc(BEAD_ROW), hooks_doc(BEAD_ROW, GUARD_ROW))
    assert rc == 0, out
    assert "ADDED" in out
    # Naming the command is the whole requirement: the 2026-07-23 install
    # merged exactly this row and said nothing at all.
    assert "inline-body-guard.sh" in out, out
    assert "PreToolUse[Bash]" in out, out
    assert "REMOVED" not in out, out


def test_removed_hook_row_is_reported_and_named(guard_sh, tmp_path):
    """A SILENT REMOVAL IS WORSE THAN A SILENT ADDITION.

    An addition at least has a payload that might misbehave loudly enough to be
    noticed. A removal has no payload at all — nothing fires to announce an
    absence, and a guard that silently stops being wired is indistinguishable
    from a guard that never fires because nothing violated it. If it is not
    reported here it is unreportable anywhere.
    """
    rc, out = report(guard_sh, tmp_path, hooks_doc(BEAD_ROW, GUARD_ROW), hooks_doc(GUARD_ROW))
    assert rc == 0, out
    assert "REMOVED" in out, out
    assert "bead-on-failure.sh" in out, out
    assert "ADDED" not in out, out


def test_modified_hook_row_is_reported(guard_sh, tmp_path):
    event, matcher, command, _ = GUARD_ROW
    rc, out = report(
        guard_sh, tmp_path,
        hooks_doc((event, matcher, command, 3000)),
        hooks_doc((event, matcher, command, 60000)),
    )
    assert rc == 0, out
    assert "MODIFIED" in out, out
    assert "60000" in out, out


def test_created_and_deleted_settings_file_are_reported(guard_sh, tmp_path):
    rc, out = report(guard_sh, tmp_path, None, hooks_doc(GUARD_ROW), label="/tmp/x/settings.json")
    assert rc == 0 and "CREATED" in out and "/tmp/x/settings.json" in out, out

    rc, out = report(guard_sh, tmp_path, hooks_doc(GUARD_ROW), None, label="/tmp/x/settings.json")
    assert rc == 0 and "DELETED" in out, out


def test_non_hook_keys_are_reported(guard_sh, tmp_path):
    """settings.json is not only hooks. env/permissions/model changes count too."""
    rc, out = report(guard_sh, tmp_path, hooks_doc(GUARD_ROW), hooks_doc(GUARD_ROW, env={"A": "1"}))
    assert rc == 0 and "ADDED" in out and "env" in out, out

    rc, out = report(guard_sh, tmp_path, hooks_doc(GUARD_ROW, env={"A": "1"}), hooks_doc(GUARD_ROW))
    assert rc == 0 and "REMOVED" in out and "env" in out, out

    rc, out = report(guard_sh, tmp_path, hooks_doc(GUARD_ROW, env={"A": "1"}), hooks_doc(GUARD_ROW, env={"A": "2"}))
    assert rc == 0 and "MODIFIED" in out and "env" in out, out


def test_unmodelled_hooks_shape_still_reports(guard_sh, tmp_path):
    """An unreadable diff is not a clean one.

    A hand-edited settings.json whose 'hooks' value is not a dict of lists
    falls outside the row model. It must fall through to a coarse report rather
    than silently reading as unchanged.
    """
    rc, out = report(guard_sh, tmp_path, {"hooks": "off"}, {"hooks": "on"})
    assert rc == 0, out
    assert "MODIFIED" in out and "hooks" in out, out


def test_rotating_backup_names_do_not_collide_within_one_second(guard_sh, tmp_path):
    """THE SINGLE-SLOT BACKUP IS THE REASON THIS DEFECT'S HISTORY IS UNKNOWABLE.

    The delegate writes `<file>.bak`, same name every install, so each run
    destroys the previous copy. Measured on the live host: the pre-window backup
    that proved the 2026-07-23 wiring did not pre-exist was already gone three
    days later, overwritten by a later ordinary install. date(1) resolves to one
    second and two installs fit inside one second, so a rotation that can still
    clobber its predecessor is not a rotation.
    """
    target = tmp_path / "settings.json"
    target.write_text("{}", encoding="utf-8")
    seen = []
    for _ in range(3):
        rc, out = call_guard(guard_sh, "settings_rotating_path", target, "bak")
        assert rc == 0, out
        path = Path(out.strip())
        assert not path.exists(), f"rotating path collided with an existing file: {path}"
        path.write_text("{}", encoding="utf-8")  # claim it, as the real caller does
        seen.append(path)
    assert len(set(seen)) == 3, seen
    assert all(str(p).startswith(str(target) + ".bak.") for p in seen), seen


# --------------------------------------------------------------------------
# UNIT — the promise and the code that keeps it, asserted TOGETHER
# --------------------------------------------------------------------------

def _flag_default(name):
    match = re.search(rf"^{name}=(\S+)\s*$", install_sh_source(), re.M)
    assert match, f"install.sh no longer sets a default for {name}"
    return match.group(1)


def test_contract_string_and_the_flag_default_agree(guard_sh):
    """THE DRIFT DETECTOR, in both directions.

    This bead exists because a promise in source and the behaviour beneath it
    drifted apart silently for months. A test that checked only the behaviour
    would leave the promise free to rot the other way. So: the contract string
    and the default that implements it are asserted in one place, and either one
    changing without the other fails here.
    """
    src = install_sh_source()
    match = re.search(r'^SETTINGS_CONTRACT="([^"]+)"', src, re.M)
    assert match, "install.sh no longer states SETTINGS_CONTRACT"
    contract = match.group(1)

    assert "does NOT write" in contract, contract
    assert "--merge-settings" in contract, contract

    # The promise says the installer does not write settings.json. The only
    # thing that can make that true is the opt-in defaulting to off.
    assert _flag_default("MERGE_SETTINGS") == "0", (
        "SETTINGS_CONTRACT promises install.sh does NOT write settings.json, but "
        "MERGE_SETTINGS defaults to on. Change one to match the other — the whole "
        "point of SABLE-gxsji is that these two must not disagree."
    )
    assert "--merge-settings) MERGE_SETTINGS=1" in src, \
        "the consent flag no longer turns the write on — the promise is now unfalsifiable"


def test_the_delegate_runs_inside_the_guard(guard_sh):
    """Structural: capture must bracket the delegate call, settle must follow it.

    Without the ordering assertion the two calls could both be present and still
    fence nothing.
    """
    src = install_sh_source()
    capture = src.index("    settings_guard_capture\n")
    delegate = src.index('bash "${REPO_DIR}/bin/sable-orchestration-install"')
    settle = src.index("    settings_guard_settle\n")
    assert capture < delegate < settle, (capture, delegate, settle)


def test_help_documents_the_consent_flag():
    proc = subprocess.run(["bash", str(INSTALL_SH), "--help"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "--merge-settings" in proc.stdout, proc.stdout
    assert "print-only" in proc.stdout, proc.stdout


# --------------------------------------------------------------------------
# The live-fleet tripwire, and proof that it can fire
# --------------------------------------------------------------------------

def fingerprint(path):
    """(size, sha256) of a file, or None when absent. Content, not mtime."""
    path = Path(path)
    if not path.is_file():
        return None
    data = path.read_bytes()
    return (len(data), hashlib.sha256(data).hexdigest())


def dir_fingerprint(path):
    """Sorted (name, symlink-target-or-None) pairs, or None when absent."""
    path = Path(path)
    if not path.is_dir():
        return None
    return sorted(
        (entry.name, os.readlink(entry) if entry.is_symlink() else None)
        for entry in path.iterdir()
    )


def test_the_tripwire_can_produce_both_outcomes(tmp_path):
    """A check that cannot fire is indistinguishable from a condition never met.

    The tripwire below is what stands between this suite and rewiring the live
    fleet, so it does not get to be trusted on the strength of never having
    complained.
    """
    target = tmp_path / "settings.json"
    target.write_text('{"hooks": {}}', encoding="utf-8")
    before = fingerprint(target)
    assert fingerprint(target) == before, "tripwire flagged an untouched file"
    target.write_text('{"hooks": {"PreToolUse": []}}', encoding="utf-8")
    assert fingerprint(target) != before, "tripwire missed a real change"

    assert fingerprint(tmp_path / "absent.json") is None

    d = tmp_path / "bin"
    d.mkdir()
    empty = dir_fingerprint(d)
    (d / "sable-x").write_text("", encoding="utf-8")
    assert dir_fingerprint(d) != empty, "dir tripwire missed a new entry"


# --------------------------------------------------------------------------
# INTEGRATION — the real install.sh, real files, sandboxed HOME, no mocks
# --------------------------------------------------------------------------

integration = pytest.mark.skipif(
    shutil.which("bd") is None and shutil.which("bd.exe") is None,
    reason="install.sh step 1 hard-requires bd on PATH; not available in this clean room",
)


@pytest.fixture
def live_tripwire():
    """Bracket a test with fingerprints of the operator's REAL install roots.

    If a sandbox leak ever lets install.sh reach ~/.claude/settings.json, this
    fails the test that caused it and names the file — rather than the fleet
    discovering it when a hook it never consented to starts intercepting tool
    calls.
    """
    before = (fingerprint(REAL_SETTINGS), dir_fingerprint(REAL_LOCAL_BIN))
    yield
    after = (fingerprint(REAL_SETTINGS), dir_fingerprint(REAL_LOCAL_BIN))
    assert after[0] == before[0], f"SANDBOX LEAK: {REAL_SETTINGS} was modified by this test"
    assert after[1] == before[1], f"SANDBOX LEAK: {REAL_LOCAL_BIN} was modified by this test"


@pytest.fixture
def sandbox(tmp_path, live_tripwire):
    """A throwaway HOME, PROVEN in effect before any writing run happens."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".local" / "bin").mkdir(parents=True)

    if home == REAL_HOME or str(home).startswith(str(REAL_HOME) + os.sep):
        pytest.skip("could not place the sandbox outside the real HOME; refusing to run")

    # The installer's own statement of where it is about to write, taken from a
    # run that writes nothing. SABLE-33hw3: a partial sandbox reads exactly like
    # a whole one until the moment it writes to the wrong place.
    proc = run_install(home, "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"Target dir: {home}/.claude" in proc.stdout, (
        "SANDBOX NOT IN EFFECT — install.sh reports a target dir outside the sandbox:\n"
        + proc.stdout
    )
    assert str(REAL_HOME / ".claude") not in proc.stdout, proc.stdout
    return home


def run_install(home, *args):
    env = {
        **os.environ,
        "HOME": str(home),
        # install.sh passes --user to the delegate, which resolves its own base
        # from CLAUDE_USER_DIR before falling back to $HOME/.claude. Pin both:
        # an ambient CLAUDE_USER_DIR would otherwise escape the sandbox through
        # the delegate while install.sh itself stayed contained.
        "CLAUDE_USER_DIR": str(home / ".claude"),
        "SABLE_REPO_DIR": str(REPO),
    }
    return subprocess.run(
        # --from-here: this suite runs from a linked worktree, which install.sh
        # refuses by default (SABLE-s6qk).
        ["bash", str(INSTALL_SH), "--from-here", *args],
        env=env, capture_output=True, text=True, timeout=600,
    )


def settings_of(home):
    return home / ".claude" / "settings.json"


def hook_commands(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        h.get("command", "")
        for blocks in data.get("hooks", {}).values() if isinstance(blocks, list)
        for b in blocks if isinstance(b, dict)
        for h in b.get("hooks", []) or [] if isinstance(h, dict)
    ]


def seed_settings(home, doc):
    path = settings_of(home)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def rotating_backups(home):
    return sorted((home / ".claude").glob("settings.json.bak.*"))


def strip_ansi(text):
    return re.sub(r"\033\[[0-9;]*m", "", text)


@integration
def test_default_install_does_not_write_settings_json(sandbox):
    """THE CONTRACT TEST: the promise printed and the behaviour observed, together.

    One test asserts (1) settings.json is byte-for-byte what it was, (2) the
    install SAID what it would have changed and named the rows, and (3) the
    contract string install.sh carries in source is the one it actually printed.
    Checking behaviour alone would let the promise rot in the other direction,
    which is precisely how this defect survived.
    """
    seeded = hooks_doc(BEAD_ROW)
    path = seed_settings(sandbox, seeded)
    before = fingerprint(path)

    proc = run_install(sandbox)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = strip_ansi(proc.stdout)

    assert fingerprint(path) == before, (
        "install.sh WROTE settings.json on an ordinary run — the promise is broken again"
    )
    assert hook_commands(path) == [BEAD_ROW[2]], hook_commands(path)

    contract = re.search(r'^SETTINGS_CONTRACT="([^"]+)"', install_sh_source(), re.M).group(1)
    assert contract in out, "install.sh did not print the contract it claims in source"

    # It must SAY SO, and NAME what it would have changed. The 2026-07-23
    # install printed nothing at all about this.
    assert "WOULD have changed settings.json" in out, out
    assert "inline-body-guard.sh" in out, out
    assert "ADDED" in out, out

    proposed = sorted((sandbox / ".claude").glob("settings.json.proposed.*"))
    assert len(proposed) == 1, proposed
    assert str(proposed[0]) in out, "the parked would-be result was not named in the output"
    assert any("inline-body-guard.sh" in c for c in hook_commands(proposed[0]))


@integration
def test_merge_settings_applies_the_write_and_names_every_addition(sandbox):
    path = seed_settings(sandbox, hooks_doc(BEAD_ROW))
    before = fingerprint(path)

    proc = run_install(sandbox, "--merge-settings")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = strip_ansi(proc.stdout)

    assert fingerprint(path) != before, "--merge-settings did not apply the merge"
    commands = hook_commands(path)
    assert any("inline-body-guard.sh" in c for c in commands), commands
    assert BEAD_ROW[2] in commands, "the merge clobbered a pre-existing entry"

    assert "CHANGED your settings.json" in out, out
    # Every added row is named, not just counted.
    added = {
        line.split()[-1].rsplit("/", 1)[-1]
        for line in out.splitlines() if line.strip().startswith("ADDED")
    }
    new_rows = {c.rsplit("/", 1)[-1] for c in commands} - {BEAD_ROW[2].rsplit("/", 1)[-1]}
    assert new_rows and new_rows <= added, (new_rows - added)


@integration
def test_an_install_that_changes_nothing_reports_no_mutation(sandbox):
    """NEGATIVE CONTROL against the real installer, not a fixture.

    A checker that always says "settings unchanged" passes the positive case
    vacuously; a checker that always says "changed" passes this one only by
    accident. Both directions are exercised here: the first run must report a
    change, the second must not.
    """
    seed_settings(sandbox, hooks_doc(BEAD_ROW))

    first = strip_ansi(run_install(sandbox, "--merge-settings").stdout)
    assert "CHANGED your settings.json" in first, first

    path = settings_of(sandbox)
    before = fingerprint(path)
    second = strip_ansi(run_install(sandbox, "--merge-settings").stdout)

    assert "unchanged — this install added, removed and modified nothing" in second, second
    assert "CHANGED your settings.json" not in second, second
    assert fingerprint(path) == before, "the idempotent re-run still rewrote the file"


@integration
def test_a_removed_entry_is_reported_not_only_additions(sandbox):
    """The strictly more dangerous half, against the real removal path.

    inbox-injection.sh is on the installer's retired list, so a real install
    genuinely strips its row. "Added" alone would leave this invisible.
    """
    retired = ("PreToolUse", "Bash", "bash /home/x/.claude/hooks/multi-manager/inbox-injection.sh", 3000)
    path = seed_settings(sandbox, hooks_doc(BEAD_ROW, retired))

    out = strip_ansi(run_install(sandbox, "--merge-settings").stdout)

    assert "REMOVED" in out, out
    assert "inbox-injection.sh" in out, out
    assert not any("inbox-injection.sh" in c for c in hook_commands(path)), hook_commands(path)
    assert BEAD_ROW[2] in hook_commands(path), "an unrelated entry was removed too"


@integration
def test_successive_installs_do_not_destroy_prior_backups(sandbox):
    """Rotation, not a single slot.

    The delegate's `<file>.bak` is one slot reused forever, so the evidence that
    would reveal how often an unconsented change happened is destroyed by the
    same operation whose rate we are trying to measure. Two runs must leave two
    recoverable snapshots.
    """
    path = seed_settings(sandbox, hooks_doc(BEAD_ROW))
    original = path.read_bytes()

    assert run_install(sandbox).returncode == 0
    assert run_install(sandbox).returncode == 0

    snapshots = rotating_backups(sandbox)
    assert len(snapshots) == 2, [p.name for p in snapshots]
    assert all(p.read_bytes() == original for p in snapshots), \
        "a rotated snapshot does not hold the pre-install content"

    out = strip_ansi(run_install(sandbox).stdout)
    assert str(rotating_backups(sandbox)[-1]) in out, \
        "the rollback copy was written but not named in the output — it must be discoverable"


@integration
def test_dry_run_writes_no_settings_file_at_all(sandbox):
    path = settings_of(sandbox)
    assert not path.exists()
    proc = run_install(sandbox, "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not path.exists(), "--dry-run created a settings.json"
    assert rotating_backups(sandbox) == []


@integration
def test_plant_and_fail_the_silent_merge_makes_the_contract_test_go_red(sandbox):
    """PLANT-AND-FAIL (SABLE-5lli.7), kept permanently.

    The pre-fix behaviour is still reachable in one line: run the delegate
    DIRECTLY, without install.sh's guard around it. That is exactly what an
    ordinary install used to do. Restoring it must turn the contract assertion
    from green to red — otherwise the assertion above is passing for some reason
    other than the fix, and we would not know.
    """
    path = seed_settings(sandbox, hooks_doc(BEAD_ROW))
    before = fingerprint(path)

    # Green leg: the guarded path leaves the file alone.
    assert run_install(sandbox).returncode == 0
    assert fingerprint(path) == before, "control leg failed before the plant was even applied"

    # Planted leg: the unguarded delegate, i.e. the defect.
    proc = subprocess.run(
        ["bash", str(DELEGATE), "--user"],
        env={**os.environ, "HOME": str(sandbox), "CLAUDE_USER_DIR": str(sandbox / ".claude"),
             "SABLE_REPO_DIR": str(REPO)},
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    assert fingerprint(path) != before, (
        "PLANT DID NOT BITE: the unguarded delegate left settings.json unchanged, so the "
        "green leg above proves nothing about the guard"
    )
    assert any("inline-body-guard.sh" in c for c in hook_commands(path)), \
        "the planted silent merge did not wire the interceptor it historically wired"
