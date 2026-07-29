#!/usr/bin/env python3
"""Unit tests for `sable-screen activation` — the landability screen
(SABLE-6eu9w).

EVERY FIXTURE IS A SCRATCH PREFIX. Not one test reads the developer's real
~/.local/bin or ~/.claude: SABLE-2avau records an installer silently targeting
the live ~/.claude and SABLE-k0nvp a pinning suite polluting the real ~/.local
twice, and a screen whose own tests install things is the same class of defect
as the one it screens for. The install-root seam exists so the fixture install
layout is the ONLY thing under test.

THE LOAD-BEARING TESTS, and why each one exists:

  * test_classification_is_resolved_not_enumerated — the regression guard for
    the failure this whole class of bug keeps producing. The fixture's symlink
    layout DISAGREES with the real machine's on purpose: `sable-doctor` is a
    live symlink in reality and is not installed at all in the fixture, while
    `not-a-real-sable-tool` is installed live in the fixture and exists nowhere
    in reality. The assertions follow the FIXTURE in both directions, so the
    test fails the moment anyone replaces the resolver with a hardcoded list of
    known hot-swap bins (SABLE-hadqx; and see SABLE-ev5i5, SABLE-e535u,
    SABLE-r69ho for three separate times that list rotted).

  * test_inert_footprint_is_landable_with_no_warning — the negative control,
    and it is load-bearing rather than decorative. A screen that warns on every
    dispatch trains managers to ignore it, which is worse than having no screen
    (the SABLE-r5pfw erosion). Asserted on the OUTPUT as well as the exit code:
    a landable bead must not print a path table either.

  * test_phantom_with_a_slash_is_not_inert — the concrete refutation. The
    hand-run this screen replaces decided path-ness by testing for a slash, and
    SABLE-p0grx really does declare the token `wk-runlock-instrument/land).`.
    A phantom matches no installed artifact, so classifying it would produce
    INERT and push the bead TOWARD landable — the error direction is PERMISSIVE
    for a screen whose entire job is predicting completion.

  * test_hot_swap_closure_* — the pair. A lib imported by a live-symlinked bin
    is activated by the pull even though nothing links the lib itself, AND a
    test file sitting in the same directory must stay INERT. Without the second
    half the closure leg would silently reclassify every bin/ bead as held.

Pairs, not single polarities, throughout: a check that cannot produce both
outcomes is indistinguishable from a check that cannot fire.

Full end-to-end coverage (real CLI, real scratch install prefix, real bd) is in
bin/test_activation_screen_integration.py.
"""
import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_screen", str(Path(__file__).resolve().parent / "sable-screen")
)
_SPEC = importlib.util.spec_from_loader("sable_screen", _LOADER)
ss = importlib.util.module_from_spec(_SPEC)
# See test_sable_screen.py: the module must be in sys.modules before
# exec_module, or dataclasses' cls.__module__ lookup finds nothing.
sys.modules["sable_screen"] = ss
_LOADER.exec_module(ss)


# --- fixture construction ---------------------------------------------------

def _tree(tmp_path, name="tree"):
    """A scratch 'working tree' with the shape this repo actually has: a bin/
    holding an entry-point script, the library it imports, and a test file."""
    tree = tmp_path / name
    (tree / "bin").mkdir(parents=True)
    (tree / "hooks" / "multi-manager").mkdir(parents=True)
    (tree / "docs").mkdir(parents=True)
    (tree / "bin" / "toolA").write_text("#!/usr/bin/env python3\nimport lib_a\n")
    (tree / "bin" / "toolB").write_text("#!/usr/bin/env python3\nprint('b')\n")
    (tree / "bin" / "toolC").write_text("#!/usr/bin/env python3\nprint('c')\n")
    (tree / "bin" / "lib_a.py").write_text("VALUE = 1\n")
    (tree / "bin" / "test_toolA.py").write_text("def test_x():\n    assert 1\n")
    (tree / "bin" / "sable-doctor").write_text("#!/usr/bin/env python3\n")
    (tree / "bin" / "not-a-real-sable-tool").write_text("#!/usr/bin/env python3\n")
    (tree / "hooks" / "multi-manager" / "guard.sh").write_text("#!/usr/bin/env bash\n")
    (tree / "docs" / "NOTES.md").write_text("# notes\n")
    return tree


def _prefix(tmp_path, tree, *, live=(), pinned=(), copied=(), name="prefix"):
    """A scratch install prefix. `live` entries symlink INTO the scratch tree
    (hot-swap), `pinned` symlink into a frozen snapshot dir outside it, and
    `copied` are real files."""
    prefix = tmp_path / name
    prefix.mkdir(exist_ok=True)
    snapshot = tmp_path / f"{name}-snapshot"
    snapshot.mkdir(exist_ok=True)
    for entry in live:
        os.symlink(tree / "bin" / entry, prefix / entry)
    for entry in pinned:
        (snapshot / entry).write_text("#!/usr/bin/env python3\n# frozen\n")
        os.symlink(snapshot / entry, prefix / entry)
    for entry in copied:
        (prefix / entry).write_text("#!/usr/bin/env python3\n# copy\n")
    return prefix


def _state(tree, *prefixes):
    scan = ss.scan_install_roots([str(p) for p in prefixes])
    roots = (os.path.realpath(str(tree)),)
    closure = ss.ClosureIndex(os.path.realpath(str(tree)),
                              ss.hot_swap_seeds(scan, roots))
    return scan, roots, closure


def _classify(rel, tree, scan, roots, closure=None):
    return ss.classify_repo_path(rel, os.path.realpath(str(tree)), scan, roots,
                                 closure)[0]


def _bead(footprint=None, *, bead_id="SABLE-test", description=None,
          metadata=None, title="a bead"):
    """A bd-shaped record. `footprint` writes the comma form the fleet
    convention requires; `description` overrides it verbatim."""
    if description is None:
        description = "Body text.\n"
        if footprint is not None:
            description += f"\n## File footprint\n{footprint}\n"
    return {"id": bead_id, "title": title, "description": description,
            "metadata": metadata}


def _verdict(bead, tree, scan, roots, closure=None, blocked=""):
    return ss.activation_for_bead(bead, os.path.realpath(str(tree)), scan,
                                  roots, blocked, closure)


# --- the four activation classes, resolved from a fixture -------------------

def test_hot_swap_path_is_classified_hot_swap(tmp_path):
    tree = _tree(tmp_path)
    prefix = _prefix(tmp_path, tree, live=["toolA"])
    scan, roots, closure = _state(tree, prefix)

    assert _classify("bin/toolA", tree, scan, roots, closure) == ss.HOT_SWAP

    v = _verdict(_bead("bin/toolA"), tree, scan, roots, closure)
    assert v.verdict == ss.HOLD_EXPECTED
    assert v.paths[0]["installed"] == [str(prefix / "toolA")]


def test_pin_managed_path_lands_inert_and_owes_a_repin(tmp_path):
    tree = _tree(tmp_path)
    prefix = _prefix(tmp_path, tree, pinned=["toolB"])
    scan, roots, closure = _state(tree, prefix)

    assert _classify("bin/toolB", tree, scan, roots, closure) == ss.PIN_MANAGED
    assert _verdict(_bead("bin/toolB"), tree, scan, roots,
                    closure).verdict == ss.LANDABLE_OWES_ACTIVATION


def test_copy_installed_path_lands_inert_and_owes_an_install(tmp_path):
    tree = _tree(tmp_path)
    prefix = _prefix(tmp_path, tree, copied=["toolC"])
    scan, roots, closure = _state(tree, prefix)

    assert _classify("bin/toolC", tree, scan, roots, closure) == ss.COPY_INSTALLED
    assert _verdict(_bead("bin/toolC"), tree, scan, roots,
                    closure).verdict == ss.LANDABLE_OWES_ACTIVATION


def test_pin_and_copy_are_distinguishable_from_each_other(tmp_path):
    """Both land inert, but the activation they owe is different — a re-pin
    versus an install — and the SABLE-3zjc1 ledger needs to know which."""
    tree = _tree(tmp_path)
    prefix = _prefix(tmp_path, tree, pinned=["toolB"], copied=["toolC"])
    scan, roots, closure = _state(tree, prefix)

    assert _classify("bin/toolB", tree, scan, roots, closure) != \
        _classify("bin/toolC", tree, scan, roots, closure)


def test_mirrored_install_layout_is_matched_by_whole_component_suffix(tmp_path):
    """hooks/ installs into <claude>/hooks/ as real files, so the artifact's
    installed-relative path is the repo path minus its leading component."""
    tree = _tree(tmp_path)
    hooks_root = tmp_path / "claude-hooks" / "multi-manager"
    hooks_root.mkdir(parents=True)
    (hooks_root / "guard.sh").write_text("#!/usr/bin/env bash\n")
    scan, roots, closure = _state(tree, tmp_path / "claude-hooks")

    assert _classify("hooks/multi-manager/guard.sh", tree, scan, roots,
                     closure) == ss.COPY_INSTALLED


# --- THE REGRESSION GUARD ---------------------------------------------------

def test_classification_is_resolved_not_enumerated(tmp_path):
    """The verdict follows the FIXTURE, not any knowledge of the real machine.

    Both directions are asserted because only the pair rules out a hardcoded
    list: a list-based implementation passes the first assertion (sable-doctor
    IS live in reality) and fails the second, and an implementation that simply
    says HOT-SWAP to everything passes the second and fails the first."""
    tree = _tree(tmp_path)
    # Deliberately inverted relative to the real machine, where sable-doctor is
    # a live symlink into the working tree and 'not-a-real-sable-tool' does not
    # exist at all.
    prefix = _prefix(tmp_path, tree, live=["not-a-real-sable-tool"])
    scan, roots, closure = _state(tree, prefix)

    assert _classify("bin/sable-doctor", tree, scan, roots, closure) == ss.INERT
    assert _classify("bin/not-a-real-sable-tool", tree, scan, roots,
                     closure) == ss.HOT_SWAP

    assert _verdict(_bead("bin/sable-doctor"), tree, scan, roots,
                    closure).verdict == ss.LANDABLE
    assert _verdict(_bead("bin/not-a-real-sable-tool"), tree, scan, roots,
                    closure).verdict == ss.HOLD_EXPECTED


def test_same_bead_flips_verdict_when_the_install_layout_changes(tmp_path):
    """One bead, one repo, two install layouts, two verdicts. The screen reads
    the install state at screen time, so re-pinning a bin changes the answer
    with no edit to this tool."""
    tree = _tree(tmp_path)
    bead = _bead("bin/toolA")

    live_scan, roots, live_closure = _state(
        tree, _prefix(tmp_path, tree, live=["toolA"], name="live"))
    pinned_scan, _, pinned_closure = _state(
        tree, _prefix(tmp_path, tree, pinned=["toolA"], name="pinned"))

    assert _verdict(bead, tree, live_scan, roots,
                    live_closure).verdict == ss.HOLD_EXPECTED
    assert _verdict(bead, tree, pinned_scan, roots,
                    pinned_closure).verdict == ss.LANDABLE_OWES_ACTIVATION


# --- the negative control ---------------------------------------------------

def test_inert_footprint_is_landable_with_no_warning(tmp_path):
    """LOAD-BEARING. Tests, CI scripts and docs activate nothing, and the
    screen must be SILENT about them — in the verdict, in the exit code, and in
    the rendered output."""
    tree = _tree(tmp_path)
    prefix = _prefix(tmp_path, tree, live=["toolA"])
    scan, roots, closure = _state(tree, prefix)

    v = _verdict(_bead("bin/test_toolA.py, docs/NOTES.md"), tree, scan, roots,
                 closure)
    assert v.verdict == ss.LANDABLE
    assert all(p["class"] == ss.INERT for p in v.paths)

    row = {"bead_id": v.bead_id, "title": "", "verdict": v.verdict,
           "section": v.section, "paths": [dict(p) for p in v.paths],
           "unparsed": [], "note": v.note}
    assert ss.activation_exit_code([row]) == 0
    text = ss.render_activation_text([row], _meta())
    assert "bin/test_toolA.py" not in text          # no path table at all
    assert "LANDABLE" in text


def test_uninstalled_path_is_inert_not_unknown(tmp_path):
    """INERT is a positive claim — 'this exists and activates nothing' — and it
    must stay distinguishable from COULD-NOT-ASSESS, which is a non-answer."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    assert _classify("bin/toolB", tree, scan, roots, closure) == ss.INERT
    assert _classify("bin/toolB", tree, scan, roots, closure) != \
        ss.PATH_UNASSESSED
    assert _verdict(_bead("bin/toolB"), tree, scan, roots,
                    closure).verdict == ss.LANDABLE


# --- worst case governs -----------------------------------------------------

def test_worst_case_governs(tmp_path):
    """One hot-swap path holds the whole branch, however many inert paths
    surround it."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    v = _verdict(_bead("bin/toolA, bin/test_toolA.py, docs/NOTES.md"), tree,
                 scan, roots, closure)
    assert v.verdict == ss.HOLD_EXPECTED
    classes = {p["path"]: p["class"] for p in v.paths}
    assert classes["bin/toolA"] == ss.HOT_SWAP
    # ...and the inert paths are still reported as inert, not smeared upward.
    assert classes["bin/test_toolA.py"] == ss.INERT
    assert classes["docs/NOTES.md"] == ss.INERT


def test_worst_case_governs_regardless_of_declaration_order(tmp_path):
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    for footprint in ("bin/toolA, docs/NOTES.md", "docs/NOTES.md, bin/toolA"):
        assert _verdict(_bead(footprint), tree, scan, roots,
                        closure).verdict == ss.HOLD_EXPECTED


def test_a_resolved_hot_swap_outranks_an_unassessable_sibling(tmp_path):
    """A definite finding must not be downgraded to 'we could not tell' just
    because some other token in the same section was unreadable."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    v = _verdict(_bead("bin/toolA, bin/never_created.py"), tree, scan, roots,
                 closure)
    assert v.verdict == ss.HOLD_EXPECTED


def test_an_unassessable_path_outranks_an_inert_one(tmp_path):
    """...and in the other direction, an unknown must never be absorbed into a
    landable verdict."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    v = _verdict(_bead("docs/NOTES.md, bin/never_created.py"), tree, scan,
                 roots, closure)
    assert v.verdict == ss.ACTIVATION_UNASSESSED


# --- the permissive direction: phantoms ------------------------------------

def test_phantom_with_a_slash_is_not_inert(tmp_path):
    """SABLE-p0grx's real token. Every slash-based 'is this a path' test
    accepts it; it matches no installed artifact; so classifying it would make
    the bead look MORE landable than it is."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    assert _classify("wk-runlock-instrument/land).", tree, scan, roots,
                     closure) == ss.PATH_UNASSESSED
    v = _verdict(_bead("wk-runlock-instrument/land)."), tree, scan, roots,
                 closure)
    assert v.verdict == ss.ACTIVATION_UNASSESSED
    assert v.verdict != ss.LANDABLE


def test_a_bead_id_scraped_from_a_relates_paragraph_is_not_inert(tmp_path):
    """The dominant phantom generator is a bead id inside the footprint
    section, which the tokenizer keeps only if it looks path-shaped. Either way
    the outcome must not be 'landable'."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    v = _verdict(_bead("bin/toolB, relates to SABLE-jz9gg and SABLE-80vat"),
                 tree, scan, roots, closure)
    assert v.verdict == ss.ACTIVATION_UNASSESSED


def test_a_token_the_tokenizer_rejects_is_reported_not_swallowed(tmp_path):
    """A bare 'Makefile' is not path-shaped, so the borrowed tokenizer drops
    it. Dropping it silently would narrow the declaration with nothing behind
    it — the SABLE-zx2yv shape, on the write side this time."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    v = _verdict(_bead("bin/toolB, Makefile"), tree, scan, roots, closure)
    assert "Makefile" in v.unparsed
    assert v.verdict == ss.ACTIVATION_UNASSESSED


# --- the section trichotomy -------------------------------------------------

def test_absent_section_is_no_declaration_not_a_finding(tmp_path):
    """A bead may legitimately touch no tracked file, and the correct spelling
    is NO SECTION AT ALL. It dispatches normally and must not warn."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    v = _verdict(_bead(None), tree, scan, roots, closure)
    assert v.verdict == ss.NO_FOOTPRINT
    assert v.section == ss.SECTION_ABSENT
    assert ss.activation_exit_code([{"verdict": v.verdict}]) == 0


def test_present_but_prose_section_is_unreadable_not_absent(tmp_path):
    """SABLE-tfzc5's live case: writing '(none — ...)' inside the section
    creates a present-but-prose section, which is the unreadable state. Three
    inputs, not two.

    The fixture text is the shape that actually MISPARSES, verified against
    sable_footprint_lib rather than assumed: the lib strips balanced
    parentheticals, so a simple '(none — ...)' aside vanishes cleanly, and it
    is a NESTED parenthetical that leaves prose tokens behind — which is
    exactly what tfzc5 carried, and why it parsed to fragments like 'it'."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    v = _verdict(
        _bead("(none — a record of repo-local git config state "
              "(see SABLE-tfzc5); it changes no tracked file)"),
        tree, scan, roots, closure)
    assert v.section == ss.SECTION_UNREADABLE
    assert "it" in v.unparsed
    assert v.verdict == ss.ACTIVATION_UNASSESSED
    assert v.verdict != ss.NO_FOOTPRINT
    assert v.verdict != ss.LANDABLE


def test_bare_prose_section_is_unreadable_not_landable(tmp_path):
    """The commonest form of the same input defect: a sentence where a
    comma-separated path list belongs. It must not read as 'declared, and
    empty', which would be landable."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    v = _verdict(_bead("none — this bead touches no tracked file, it only "
                       "records state"), tree, scan, roots, closure)
    assert v.section == ss.SECTION_UNREADABLE
    assert v.verdict == ss.ACTIVATION_UNASSESSED


def test_explicitly_empty_section_is_a_real_answer(tmp_path):
    """The fleet's spelling for 'declared, and empty' is a lone 'none' line.
    That is an answer, so it is landable — distinct from both an absent section
    and an unreadable one."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    v = _verdict(_bead("none"), tree, scan, roots, closure)
    assert v.section == ss.SECTION_EMPTY
    assert v.verdict == ss.LANDABLE


def test_structured_field_is_used_verbatim_over_prose(tmp_path):
    """Precedence mirrors sable_footprint_lib.declared_footprint(): when
    metadata.footprint_writes is present the prose parser is not consulted at
    all, so a junk prose section cannot override a clean structured one."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    bead = _bead("Makefile and some prose",
                 metadata={"footprint_writes": "bin/toolA"})
    v = _verdict(bead, tree, scan, roots, closure)
    assert v.section == ss.SECTION_FIELD
    assert v.unparsed == ()
    assert v.verdict == ss.HOLD_EXPECTED


def test_tokenization_is_borrowed_from_sable_footprint_lib():
    """A screen with its own private token filter is the SABLE-ev5i5
    three-mirror disease. Pin the borrowing itself: the section reader this
    module calls must be the lib's, so whatever the lib decides (including
    SABLE-g0elq's path_tokens() once it lands) is what this screen inherits."""
    import sable_footprint_lib as fp_lib

    calls = []
    original = fp_lib._collect_section

    def spy(description, heading):
        calls.append(heading)
        return original(description, heading)

    fp_lib._collect_section = spy
    try:
        state, entries, _ = ss._footprint_section(_bead("bin/toolA"))
    finally:
        fp_lib._collect_section = original

    assert calls == [fp_lib._FOOTPRINT_HEADING]
    assert state == ss.SECTION_DECLARED
    assert entries == frozenset({"bin/toolA"})


# --- the live import closure ------------------------------------------------

def test_hot_swap_closure_catches_a_lib_imported_by_a_live_symlink(tmp_path):
    """Nothing installs bin/lib_a.py, so no artifact resolves to it — but
    bin/toolA is a live symlink and imports it, so the pull activates it."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    assert _classify("bin/lib_a.py", tree, scan, roots, closure) == \
        ss.HOT_SWAP_CLOSURE
    assert _verdict(_bead("bin/lib_a.py"), tree, scan, roots,
                    closure).verdict == ss.HOLD_EXPECTED


def test_hot_swap_closure_leaves_a_neighbouring_test_file_inert(tmp_path):
    """The other half of the pair. bin/test_toolA.py sits in the same
    directory as a live symlink and is imported by nothing, so it stays INERT.
    Without this, the closure leg would report every bin/ bead as held and the
    negative control would be dead."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    assert _classify("bin/test_toolA.py", tree, scan, roots, closure) == ss.INERT


def test_hot_swap_closure_is_empty_when_nothing_is_live(tmp_path):
    """Same repo, same imports, no live symlink — so the closure has no seed
    and the lib is inert. The leg is driven by the resolved install state, not
    by the import graph alone."""
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, pinned=["toolA"]))

    assert _classify("bin/lib_a.py", tree, scan, roots, closure) == ss.INERT


def test_hot_swap_closure_is_transitive(tmp_path):
    """A lib reached only through another lib is still live."""
    tree = _tree(tmp_path)
    (tree / "bin" / "lib_a.py").write_text("import lib_deep\n")
    (tree / "bin" / "lib_deep.py").write_text("VALUE = 2\n")
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    assert _classify("bin/lib_deep.py", tree, scan, roots, closure) == \
        ss.HOT_SWAP_CLOSURE


def test_hot_swap_closure_follows_shell_source_lines(tmp_path):
    tree = _tree(tmp_path)
    (tree / "bin" / "hook.sh").write_text(
        '#!/usr/bin/env bash\nsource "$(dirname "$0")/lib-shared.sh"\n')
    (tree / "bin" / "lib-shared.sh").write_text("shared() { :; }\n")
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["hook.sh"]))

    assert _classify("bin/lib-shared.sh", tree, scan, roots, closure) == \
        ss.HOT_SWAP_CLOSURE


# --- the silent-instrument contract ----------------------------------------

def test_unreadable_install_root_is_could_not_assess_not_landable(tmp_path):
    """An unreadable install prefix is NOT 'nothing is installed'. Skipped
    under a uid that ignores directory permissions (root), where the fixture
    cannot produce the failing polarity at all — a check that cannot fire must
    not be reported as passing."""
    tree = _tree(tmp_path)
    prefix = _prefix(tmp_path, tree, live=["toolA"])
    locked = prefix / "locked"
    locked.mkdir()
    (locked / "toolZ").write_text("#\n")
    os.chmod(locked, 0o000)
    try:
        scan = ss.scan_install_roots([str(prefix)])
        if not scan.unreadable:
            pytest.skip("this uid can read a 0o000 directory; the unreadable "
                        "polarity cannot be produced here")
        blocked = ss.scan_problem(scan, (os.path.realpath(str(tree)),))
        assert blocked
        v = _verdict(_bead("bin/test_toolA.py"), tree, scan,
                     (os.path.realpath(str(tree)),), blocked=blocked)
        assert v.verdict == ss.ACTIVATION_UNASSESSED
    finally:
        os.chmod(locked, 0o700)


def test_no_readable_install_root_at_all_is_could_not_assess(tmp_path):
    tree = _tree(tmp_path)
    scan = ss.scan_install_roots([str(tmp_path / "does-not-exist")])
    blocked = ss.scan_problem(scan, (os.path.realpath(str(tree)),))

    assert blocked
    assert _verdict(_bead("bin/toolA"), tree, scan,
                    (os.path.realpath(str(tree)),),
                    blocked=blocked).verdict == ss.ACTIVATION_UNASSESSED


def test_an_empty_but_readable_prefix_is_a_real_answer(tmp_path):
    """The paired control for the two tests above: an install prefix that
    exists and is readable and happens to be empty really does mean 'nothing is
    installed', and must NOT degrade into could-not-assess."""
    tree = _tree(tmp_path)
    empty = tmp_path / "empty-prefix"
    empty.mkdir()
    scan = ss.scan_install_roots([str(empty)])
    roots = (os.path.realpath(str(tree)),)

    assert ss.scan_problem(scan, roots) == ""
    assert _verdict(_bead("bin/toolA"), tree, scan, roots).verdict == ss.LANDABLE


def test_unenumerable_worktrees_is_could_not_assess(tmp_path):
    """Without the tree list, hot-swap and pin-managed are indistinguishable —
    and guessing would downgrade a hold to a landable."""
    tree = _tree(tmp_path)
    scan = ss.scan_install_roots([str(_prefix(tmp_path, tree, live=["toolA"]))])

    assert ss.scan_problem(scan, ()) != ""
    assert _verdict(_bead("bin/toolA"), tree, scan, (),
                    blocked=ss.scan_problem(scan, ())).verdict == \
        ss.ACTIVATION_UNASSESSED


def test_a_degraded_instrument_still_reports_no_declaration_honestly(tmp_path):
    """A bead that declares nothing had nothing to assess, so a broken
    instrument does not change its answer."""
    tree = _tree(tmp_path)
    scan = ss.scan_install_roots([str(tmp_path / "nope")])

    assert _verdict(_bead(None), tree, scan, (), blocked="broken").verdict == \
        ss.NO_FOOTPRINT


def test_worktree_roots_returns_none_when_git_cannot_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("SABLE_SCREEN_GIT", "false")
    assert ss.worktree_roots(str(tmp_path)) is None


def test_worktree_roots_reads_real_git(tmp_path):
    """The positive control for the probe above — it must be able to produce
    BOTH outcomes."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    roots = ss.worktree_roots(str(repo))
    assert roots and os.path.realpath(str(repo)) in roots


# --- directory entries ------------------------------------------------------

def test_directory_entry_is_classified_by_what_is_inside_it(tmp_path):
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    v = _verdict(_bead("bin/"), tree, scan, roots, closure)
    assert v.verdict == ss.HOLD_EXPECTED
    assert {p["path"] for p in v.paths} >= {"bin/toolA", "bin/test_toolA.py"}


def test_inert_directory_entry_stays_landable(tmp_path):
    tree = _tree(tmp_path)
    scan, roots, closure = _state(tree, _prefix(tmp_path, tree, live=["toolA"]))

    assert _verdict(_bead("docs/"), tree, scan, roots,
                    closure).verdict == ss.LANDABLE


# --- exit codes and rendering ----------------------------------------------

def _meta(**over):
    meta = {"install_roots_scanned": ["/scratch/prefix"],
            "install_roots_absent": [], "install_roots_unreadable": [],
            "installed_artifacts": 3, "worktrees": ["/scratch/tree"],
            "blocked": ""}
    meta.update(over)
    return meta


def test_exit_code_zero_for_landable_and_no_declaration():
    assert ss.activation_exit_code([{"verdict": ss.LANDABLE},
                                    {"verdict": ss.NO_FOOTPRINT},
                                    {"verdict": ss.LANDABLE_OWES_ACTIVATION}]) == 0


def test_exit_code_one_for_hold_expected():
    assert ss.activation_exit_code([{"verdict": ss.LANDABLE},
                                    {"verdict": ss.HOLD_EXPECTED}]) == 1


def test_exit_code_two_outranks_one():
    """'The screen could not do its job' must not hide behind a verdict that
    looks decided."""
    assert ss.activation_exit_code([{"verdict": ss.HOLD_EXPECTED},
                                    {"verdict": ss.ACTIVATION_UNASSESSED}]) == 2
    assert ss.activation_exit_code([{"verdict": "not-found"}]) == 2


def test_render_names_the_installed_artifact_that_forces_the_hold():
    row = {"bead_id": "SABLE-x", "title": "t", "verdict": ss.HOLD_EXPECTED,
           "section": ss.SECTION_DECLARED,
           "paths": [{"path": "bin/toolA", "class": ss.HOT_SWAP,
                      "installed": ["/scratch/prefix/toolA"],
                      "reason": "resolves into a live working tree"}],
           "unparsed": [], "note": "landing ACTIVATES an installed artifact"}
    text = ss.render_activation_text([row], _meta())

    assert "SABLE-x" in text
    assert "bin/toolA" in text
    assert "/scratch/prefix/toolA" in text


def test_render_surfaces_a_degraded_instrument_loudly():
    row = {"bead_id": "SABLE-x", "title": "t", "verdict": ss.ACTIVATION_UNASSESSED,
           "section": ss.SECTION_DECLARED, "paths": [], "unparsed": [],
           "note": "no readable install root"}
    text = ss.render_activation_text([row], _meta(blocked="no readable install root"))

    assert "INSTRUMENT DEGRADED" in text
