#!/usr/bin/env python3
"""Unit tests for the identifier-decay detector (SABLE-x9vby).

The detector's whole value is a two-directional claim, so every test here is
paired: an INSTRUCTIONAL referral to a retiring identifier must flag, and a
PROVENANCE mention of the same identifier must not. A one-directional suite
would pass happily on a detector that flags every relate-link, which is the
banner-blindness failure mode this hook has to avoid.

The known-positive rehearsal (test_known_positive_jejx3_names_gz3v2) pins the
mandatory validation: the sweep must independently surface the real case chuck
found by hand — SABLE-gz3v2 <- SABLE-jejx3 — using that bead's VERBATIM note
line. An instrument that reports a comfortable number without detecting the
case it was built for is a dead grep.
"""
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sable_identifier_decay_lib import (  # noqa: E402
    CITATION, INSTRUCTION, KNOWN_LIMIT, format_flags, format_unassessed,
    identifier_variants, is_instructional, is_provenance, sentence_spans,
    sweep, sweep_bead,
)

CLI = Path(__file__).resolve().parent / "sable-identifier-decay"

# In-process import of the extensionless CLI script (same pattern as
# test_sable_probe.py / test_sable_vacuous_guard_scan.py) so bd_timeout() and
# load_open_beads()'s retry logic can be exercised directly, without paying a
# subprocess per case.
_LOADER = SourceFileLoader("sable_identifier_decay_cli", str(CLI))
_SPEC = importlib.util.spec_from_loader("sable_identifier_decay_cli", _LOADER)
idd_cli = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(idd_cli)


def bead(bead_id, *, title="a bead", description="", notes=""):
    return {"id": bead_id, "title": title, "description": description, "notes": notes}


# --------------------------------------------------------------------------
# The core two-directional claim
# --------------------------------------------------------------------------

def test_flags_instructional_referrer_not_provenance():
    """Both directions in one corpus: the instruction flags, the relate-link
    does not, and the detector distinguishes them within the SAME sweep."""
    corpus = [
        bead("SABLE-instr", notes="verify SABLE-X's suppression still holds after your change"),
        bead("SABLE-prov", notes="RELATES: SABLE-X"),
    ]
    flagged = {f.referrer_id for f in sweep(["SABLE-X"], corpus)}
    assert "SABLE-instr" in flagged, "instructional referrer must flag"
    assert "SABLE-prov" not in flagged, "a bare RELATES link must NOT flag"


def test_provenance_line_is_not_flagged_even_carrying_instructional_words():
    """The provenance exclusion is decided by the LINE SHAPE, not by the absence
    of instructional vocabulary — otherwise 'RELATES: SABLE-X (must-fix sibling)'
    would flag and every richly-annotated link becomes noise."""
    corpus = [bead("SABLE-prov2", notes="RELATES: SABLE-X (the must-fix sibling)")]
    assert sweep(["SABLE-X"], corpus) == []


def test_non_instructional_mention_is_not_flagged():
    """A bead that merely NARRATES the identifier is not carrying an instruction
    about it; flagging it is pure noise with no decay behind it."""
    corpus = [bead("SABLE-narr", notes="SABLE-X landed on the integration branch yesterday.")]
    assert sweep(["SABLE-X"], corpus) == []


@pytest.mark.parametrize("line", [
    "you must verify SABLE-X before landing",
    "HARD REQUIREMENT: migrate the hold recorded on SABLE-X",
    "DO NOT CLOSE until SABLE-X is merged",
    "assert SABLE-X's suppression still holds",
    "this hold needs SABLE-X to land first",
])
def test_instructional_vocabulary_is_tuned_loose(line):
    """Tuned LOOSE on purpose: a false flag costs one read, a miss costs an agent
    honouring a dead instruction indefinitely. These five shapes all flag."""
    assert sweep(["SABLE-X"], [bead("SABLE-r", notes=line)]), line


# --------------------------------------------------------------------------
# Mandatory validation: the case this instrument was built for
# --------------------------------------------------------------------------

# VERBATIM from SABLE-jejx3's notes (line 5 of that field) — the requirement
# that survived lincoln's entirely correct close of SABLE-gz3v2 and then pointed
# at a closed bead with nothing to migrate. Note it names the identifier BARE
# ("gz3v2"), which is why bare-suffix matching is not optional.
JEJX3_NOTE_LINE = (
    "SO THE FIX FOR THIS BEAD CAN DESTROY ITS OWN MITIGATION. Any change to "
    "predicate 3's semantics may silently drop gz3v2's suppression while "
    "wk-tree-claim-target is still held, at which point the floor resumes "
    "auto-filing the INVERTED 'merge me' handoff. HARD REQUIREMENT: verify "
    "gz3v2's suppression still holds after your change, or migrate the hold to "
    "your new first-class mechanism IN THE SAME CHANGE."
)


def test_known_positive_jejx3_names_gz3v2():
    """The sweep must independently surface the hand-found instance."""
    corpus = [bead("SABLE-jejx3", title="HELD as a first-class third outcome",
                   notes=JEJX3_NOTE_LINE)]
    flags = sweep(["SABLE-gz3v2"], corpus)
    assert flags, "the sweep MUST detect the case it was built for (gz3v2 <- jejx3)"
    assert flags[0].referrer_id == "SABLE-jejx3"
    assert "HARD REQUIREMENT" in flags[0].line


def test_known_positive_would_be_missed_without_bare_suffix_matching():
    """Negative control on the instrument itself: full-id-only matching misses
    the motivating case, which is why identifier_variants() exists."""
    corpus = [bead("SABLE-jejx3", notes=JEJX3_NOTE_LINE)]
    assert sweep(["SABLE-gz3v2"], corpus, bare_suffix=False) == []


# --------------------------------------------------------------------------
# Matching boundaries
# --------------------------------------------------------------------------

def test_identifier_variants_expands_bead_ids_but_not_branch_names():
    assert identifier_variants("SABLE-gz3v2") == ["SABLE-gz3v2", "gz3v2"]
    assert identifier_variants("wk-dep-merge-guard", bare_suffix=False) == ["wk-dep-merge-guard"]


def test_short_suffixes_do_not_expand():
    """A 3-char suffix collides with ordinary words; expanding it would drown
    the flag in noise even under a loose tuning."""
    assert identifier_variants("SABLE-abc") == ["SABLE-abc"]


def test_mention_must_be_a_delimited_token():
    """Substring matching would flag SABLE-x9vby2 for SABLE-x9vby."""
    corpus = [bead("SABLE-r", notes="you must verify SABLE-x9vby2 first")]
    assert sweep(["SABLE-x9vby"], corpus) == []


def test_closing_bead_does_not_flag_itself():
    """A bead's instructions about itself are not decay — it is going away."""
    corpus = [bead("SABLE-X", notes="you must verify SABLE-X before closing")]
    assert sweep(["SABLE-X"], corpus) == []


def test_branch_name_seam_flags_a_hold_instruction():
    """Promote-time seam: deleting a merged branch retires the branch NAME, and
    a hold keyed to that name decays the instant it stops resolving."""
    corpus = [bead("SABLE-3nymz",
                   notes="DO NOT MERGE wk-dep-merge-guard while this hold stands")]
    flags = sweep(["wk-dep-merge-guard"], corpus, bare_suffix=False)
    assert [f.referrer_id for f in flags] == ["SABLE-3nymz"]


def test_all_scanned_fields_are_swept():
    for field in ("title", "description", "notes"):
        corpus = [bead("SABLE-r", **{field: "must verify SABLE-X"} if field != "title"
                       else {"title": "must verify SABLE-X"})]
        flags = sweep(["SABLE-X"], corpus)
        assert [f.field for f in flags] == [field], field


def test_flag_records_field_and_line_number():
    corpus = [bead("SABLE-r", notes="intro line\nanother line\nmust verify SABLE-X here")]
    (flag,) = sweep(["SABLE-X"], corpus)
    assert (flag.field, flag.line_no) == ("notes", 3)
    assert flag.line == "must verify SABLE-X here"


def test_multiple_retiring_identifiers_are_all_swept():
    corpus = [bead("SABLE-r", notes="must verify SABLE-A\nand migrate SABLE-B")]
    flags = sweep(["SABLE-A", "SABLE-B"], corpus)
    assert {f.identifier for f in flags} == {"SABLE-A", "SABLE-B"}


def test_predicates_are_independently_correct():
    assert is_instructional("you must do the thing")
    assert not is_instructional("this landed yesterday")
    assert is_provenance("RELATES: SABLE-X")
    assert is_provenance("  - blocked by: SABLE-X")
    assert not is_provenance("the reference implementation must verify SABLE-X")


def test_sweep_bead_is_the_single_bead_unit():
    flags = sweep_bead("SABLE-X", bead("SABLE-r", notes="must verify SABLE-X"))
    assert len(flags) == 1 and flags[0].referrer_id == "SABLE-r"


# --------------------------------------------------------------------------
# Reporting: silence means clean, and could-not-assess never looks clean
# --------------------------------------------------------------------------

def test_no_flags_renders_nothing():
    """Rare enough to be read is a property of the SILENT case too — a clean
    sweep must add zero lines to the closer's screen."""
    assert format_flags([], ["SABLE-X"]) == ""


def test_report_names_the_referrer_the_line_and_the_known_limit():
    flags = sweep(["SABLE-X"], [bead("SABLE-r", title="the referrer",
                                     notes="must verify SABLE-X")])
    report = format_flags(flags, ["SABLE-X"])
    assert "SABLE-r" in report
    assert "must verify SABLE-X" in report
    assert "notes:1" in report
    assert KNOWN_LIMIT in report, "a detector whose limits are undocumented gets trusted past them"


def test_long_reports_truncate_but_never_silently():
    """A hub bead can leave twenty-plus referrals; twenty-plus is not readable.
    The cap must state the TRUE total and how to see the rest — a silent cap
    reads as 'everything is covered' when it isn't.

    SABLE-l662t: the named remedy is now `--all`. The bare command it used to
    print reproduced this very truncation, so following it looped."""
    corpus = [bead(f"SABLE-r{i:02d}", notes="must verify SABLE-X") for i in range(25)]
    flags = sweep(["SABLE-X"], corpus)
    assert len(flags) == 25
    report = format_flags(flags, ["SABLE-X"])
    assert "leaves 25 open instructions" in report, "the head states the TRUE total"
    assert "15 more not shown" in report
    assert "sable-identifier-decay --all SABLE-X" in report, "names how to see the rest"
    assert "SABLE-r00" in report and "SABLE-r24" not in report


def test_known_limit_names_the_undetectable_case():
    assert "SABLE-3nymz" in KNOWN_LIMIT
    assert "code path" in KNOWN_LIMIT.lower()


def test_unassessed_report_is_loud_and_never_reads_as_clean():
    msg = format_unassessed(["SABLE-X"], "bd is not on PATH")
    assert "COULD NOT ASSESS" in msg
    assert "bd is not on PATH" in msg
    assert "NOT a clean result" in msg
    assert "fail-open" in msg.lower()


# --------------------------------------------------------------------------
# SABLE-wr6zp: budget derivation + retry-once-on-timeout
#
# The check's whole value is being available at the exact moment (burst
# load) it is most needed. These tests pin the two halves of the fix: the
# budget is no longer a hand-picked literal that a burst can outrun, and a
# transient timeout no longer immediately reports could-not-assess without
# a second try. The regression guard (a bd that hangs BOTH times must still
# report could-not-assess, never a false clean) is what stops a future "just
# raise the number again" patch from silently reintroducing the original
# false-clean failure at the next load level.
# --------------------------------------------------------------------------

def _write_bd_stub(bin_dir: Path, script_body: str) -> None:
    stub = bin_dir / "bd"
    stub.write_text(f"#!/usr/bin/env bash\n{script_body}\n")
    stub.chmod(0o755)


def _prepend_path(monkeypatch, bin_dir: Path) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def test_budget_is_derived_not_hardcoded(tmp_path, monkeypatch):
    """Mirrors _impact_timeout's own SSOT tests (SABLE-jd5fj.9): the budget
    reads .github/ci/test-tiers.sh's merge_preview budget instead of the old
    hardcoded 20."""
    monkeypatch.delenv("SABLE_IDREF_TIMEOUT", raising=False)
    ci_dir = tmp_path / ".github" / "ci"
    ci_dir.mkdir(parents=True)
    (ci_dir / "test-tiers.sh").write_text("#!/usr/bin/env bash\necho 12345\n")
    (ci_dir / "test-tiers.sh").chmod(0o755)
    assert idd_cli.bd_timeout(str(tmp_path)) == 12345.0


def test_budget_override_still_wins_over_the_ssot(tmp_path, monkeypatch):
    ci_dir = tmp_path / ".github" / "ci"
    ci_dir.mkdir(parents=True)
    (ci_dir / "test-tiers.sh").write_text("#!/usr/bin/env bash\necho 12345\n")
    (ci_dir / "test-tiers.sh").chmod(0o755)
    monkeypatch.setenv("SABLE_IDREF_TIMEOUT", "42")
    assert idd_cli.bd_timeout(str(tmp_path)) == 42.0


def test_budget_falls_back_to_pre_fix_constant_without_an_ssot(tmp_path, monkeypatch):
    """No .github/ci/test-tiers.sh at all (e.g. a repo-less caller) must not
    raise — it gets the old 20s, exactly today's behaviour."""
    monkeypatch.delenv("SABLE_IDREF_TIMEOUT", raising=False)
    assert idd_cli.bd_timeout(str(tmp_path)) == 20.0


def test_budget_falls_back_on_an_unparseable_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SABLE_IDREF_TIMEOUT", "not-a-number")
    assert idd_cli.bd_timeout(str(tmp_path)) == 20.0


def test_timeout_reports_could_not_assess_not_clean(tmp_path, monkeypatch):
    """Regression guard on today's good behaviour: a stubbed bd that hangs on
    EVERY attempt must produce COULD NOT ASSESS, never an empty clean result.
    This is the property that must not regress while fixing the timing."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_bd_stub(bin_dir, "sleep 30")
    _prepend_path(monkeypatch, bin_dir)
    monkeypatch.setenv("SABLE_IDREF_TIMEOUT", "0.1")

    beads, reason = idd_cli.load_open_beads(None)

    assert beads == []
    assert reason is not None and "timed out" in reason
    assert "retried once" in reason, "must report having retried, not just the first failure"


def test_timeout_retries_once_before_reporting(tmp_path, monkeypatch):
    """Stub bd to hang once then succeed; assert exactly one retry and a
    real verdict, not could-not-assess."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_count = tmp_path / "calls"
    _write_bd_stub(bin_dir, f"""
COUNT_FILE={call_count}
N=$( [ -f "$COUNT_FILE" ] && cat "$COUNT_FILE" || echo 0 )
N=$((N + 1))
echo "$N" > "$COUNT_FILE"
if [ "$N" -eq 1 ]; then
  sleep 30
fi
echo '[]'
""")
    _prepend_path(monkeypatch, bin_dir)
    monkeypatch.setenv("SABLE_IDREF_TIMEOUT", "0.2")

    beads, reason = idd_cli.load_open_beads(None)

    assert reason is None, f"a retry that succeeds must not report could-not-assess: {reason}"
    assert beads == []
    assert call_count.read_text().strip() == "2", "must retry exactly once, not more, not zero"


def test_missing_bd_is_not_retried(tmp_path, monkeypatch):
    """FileNotFoundError is not a transient spike — retrying it wastes the
    interactive close/promote's time on a binary that will not appear."""
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("SABLE_IDREF_TIMEOUT", "5")

    beads, reason = idd_cli.load_open_beads(None)

    assert beads == []
    assert reason == "bd is not on PATH"


# --------------------------------------------------------------------------
# CLI surface (no bd required: these paths never reach a query)
# --------------------------------------------------------------------------

def _cli(*args, env=None):
    return subprocess.run([sys.executable, str(CLI), *args], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)


def test_cli_with_no_identifiers_is_silent_and_clean():
    cp = _cli()
    assert cp.returncode == 0 and cp.stdout.strip() == ""


def test_cli_help_states_the_known_limit():
    cp = _cli("--help")
    assert cp.returncode == 0
    assert "code path" in cp.stdout.lower()


def test_cli_accepts_all_and_renders_every_flag(tmp_path):
    """--all is the remedy the truncation line names; it must exist and it must
    render past MAX_RENDERED."""
    corpus = [bead(f"SABLE-r{i:02d}", notes="must verify SABLE-X") for i in range(25)]
    out = _cli_against_stub_store(tmp_path, corpus, ["--all", "SABLE-X"]).stdout
    assert "SABLE-r24" in out
    assert "more not shown" not in out


# --------------------------------------------------------------------------
# SABLE-l662t: the SCOPE of the match is not the property being enforced
#
# Measured live 2026-07-24: closing the completed 9-child epic SABLE-be4lo
# reported 61 hits of which approximately ZERO were real decay. Three
# independent defects stacked, and they fail DIFFERENTLY, so they get three
# separate fixes and three separate pairs of tests here.
#
# Every one of these is paired with a NEGATIVE CONTROL, because the failure
# mode of this whole bead is a checker that stops firing: a blinded detector
# passes every "no false positives" test and is strictly worse than the noise
# it removed. If a control below does not bite when the corresponding fix is
# reverted to "match everything", the fix is unverified.
# --------------------------------------------------------------------------

# ---- LAYER 1: prefix collision -------------------------------------------

def test_retiring_a_parent_does_not_flag_child_references():
    """Retiring SABLE-be4lo retires NOTHING of SABLE-be4lo.1's — the children
    were closed on their own schedule, days apart. 44 of the 50 measured hits
    were this: a DIFFERENT identifier that happens to share a prefix."""
    corpus = [
        bead("SABLE-c1", notes="you must verify SABLE-be4lo.1 before landing"),
        bead("SABLE-c2", notes="DO NOT CLOSE until SABLE-be4lo.9 is merged"),
    ]
    assert sweep(["SABLE-be4lo"], corpus) == []


def test_retiring_a_parent_still_flags_a_bare_reference_to_the_parent():
    """NEGATIVE CONTROL for layer 1: the fix must NARROW the match, not disable
    it. A bare reference to the epic itself is exactly the case the detector
    exists for and must survive."""
    corpus = [bead("SABLE-r", notes="you must verify SABLE-be4lo before landing")]
    assert [f.referrer_id for f in sweep(["SABLE-be4lo"], corpus)] == ["SABLE-r"]


def test_retiring_a_child_still_flags_that_child():
    """The other half of the same control: when the CHILD is the thing being
    retired, references to it are real decay and must flag."""
    corpus = [bead("SABLE-r", notes="you must verify SABLE-be4lo.1 before landing")]
    assert [f.referrer_id for f in sweep(["SABLE-be4lo.1"], corpus)] == ["SABLE-r"]


def test_child_exclusion_also_covers_the_bare_suffix_form():
    """Bead ids are written bare as often as fully (that is why bare-suffix
    matching exists), so `be4lo.3` must be excluded on the same grounds as
    `SABLE-be4lo.3` — otherwise the noise just re-enters through the other door."""
    corpus = [bead("SABLE-r", notes="you must verify be4lo.3 before landing")]
    assert sweep(["SABLE-be4lo"], corpus) == []


def test_a_sentence_ending_period_is_not_a_child_suffix():
    """The exclusion is `.<digit>`, not `.` — otherwise every mention that ends
    a sentence would be silently dropped, which is the blinding failure."""
    corpus = [bead("SABLE-r", notes="you must verify SABLE-be4lo. Then land it.")]
    assert [f.referrer_id for f in sweep(["SABLE-be4lo"], corpus)] == ["SABLE-r"]


# ---- LAYER 2: citation vs instruction ------------------------------------

def test_a_citation_is_not_reported_as_an_instruction():
    """"This bug was observed on X" is true forever and cannot be satisfied or
    unsatisfied; a hold naming X as a live precondition can. Only the second is
    decay. Both are still REPORTED — separating is safe, suppressing is not."""
    corpus = [
        bead("SABLE-cite", description=(
            "The checker misfired here. This was first observed on SABLE-X during "
            "the 2026-07-24 drain. We must check the tier budget before the next run.")),
        bead("SABLE-instr", notes="hold_until: SABLE-X must land on the integration branch"),
    ]
    flags = sweep(["SABLE-X"], corpus)
    kinds = {f.referrer_id: f.kind for f in flags}
    assert kinds == {"SABLE-cite": CITATION, "SABLE-instr": INSTRUCTION}
    counted = [f.referrer_id for f in flags if f.kind == INSTRUCTION]
    assert counted == ["SABLE-instr"], "only the instruction counts as decay"


def test_the_citation_class_does_not_blind_a_genuine_instruction():
    """NEGATIVE CONTROL for layer 2, the SABLE-fzn14 shape: a real instruction
    naming a retired identifier as still-live, sitting inside an otherwise
    narrative paragraph. This is the case the detector caught for real the same
    day the false-positive flood was measured; it must still be an INSTRUCTION."""
    corpus = [bead("SABLE-r", description=(
        "Background: the drain ran clean on 2026-07-24 and nothing regressed. "
        "HARD REQUIREMENT: verify SABLE-fzn14's misverdict guard still holds "
        "before dispatching."))]
    flags = sweep(["SABLE-fzn14"], corpus)
    assert [(f.referrer_id, f.kind) for f in flags] == [("SABLE-r", INSTRUCTION)]


def test_an_instructional_word_in_a_different_sentence_does_not_make_an_instruction():
    """The mechanism, stated directly: co-location on the same physical LINE was
    the proxy, and a bead description is one line per PARAGRAPH — in the live
    corpus the instructional token sat up to 427 characters and several
    sentences away from the mention. Co-location now means the same sentence."""
    line = "SABLE-X shipped last Tuesday. Separately, you must migrate the hold."
    (flag,) = sweep(["SABLE-X"], [bead("SABLE-r", notes=line)])
    assert flag.kind == CITATION


def test_sentence_spans_cover_the_line_exactly():
    """The splitter is load-bearing for layer 2, so it is pinned directly: every
    character of the line lands in exactly one span, at its true offset."""
    line = "First one. Second one; third one! Fourth."
    spans = sentence_spans(line)
    assert [t for _, t in spans] == ["First one.", "Second one;", "third one!", "Fourth."]
    for start, text in spans:
        assert line[start:start + len(text)] == text


def test_citations_alone_do_not_raise_the_decay_banner_but_are_not_hidden():
    """The measured live case, in miniature: an identifier whose only open
    referrals are historical citations must not fire the ⚠ decay banner (there
    is nothing to fix), and must not vanish either — the count and the way to
    read them are stated."""
    corpus = [bead(f"SABLE-c{i}", description=(
        "This was observed on SABLE-X last week. We must check it again someday."))
        for i in range(6)]
    flags = sweep(["SABLE-X"], corpus)
    assert len(flags) == 6 and {f.kind for f in flags} == {CITATION}
    report = format_flags(flags, ["SABLE-X"])
    assert "⚠" not in report, "zero instructions must not read as a warning"
    assert "6" in report and "citation" in report.lower()
    assert "--all" in report, "non-actionable does not mean unreadable"


def test_a_report_counts_instructions_and_states_the_citations_separately():
    corpus = [bead("SABLE-instr", notes="hold_until: SABLE-X must land first")]
    corpus += [bead(f"SABLE-c{i}", description=(
        "This was observed on SABLE-X last week. We must check it again someday."))
        for i in range(4)]
    report = format_flags(sweep(["SABLE-X"], corpus), ["SABLE-X"])
    assert "leaves 1 open instruction still naming it" in report
    assert "SABLE-instr" in report
    assert "4" in report and "citation" in report.lower()


# ---- LAYER 3: the remedy that looped -------------------------------------

def _cli_against_stub_store(tmp_path, corpus, argv):
    """Run the REAL CLI against a stub `bd` that answers with `corpus`. Real
    argument parsing, real rendering, real process — only the store is a
    fixture, which is what makes this a unit test rather than the integration
    one."""
    root = tmp_path / f"stub{len(list(tmp_path.iterdir()))}"
    root.mkdir()
    store = root / "beads.json"
    store.write_text(json.dumps(corpus))
    bin_dir = root / "bin"
    bin_dir.mkdir()
    _write_bd_stub(bin_dir, f'cat {shlex.quote(str(store))}')
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.pop("SABLE_IDREF_TIMEOUT", None)
    cp = subprocess.run([sys.executable, str(CLI), *argv], text=True, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert cp.returncode == 0, f"rc={cp.returncode} stderr={cp.stderr}"
    return cp


def test_the_printed_see_them_all_command_actually_shows_them_all(tmp_path):
    """LAYER 3, EXECUTED rather than asserted by wording: parse the command the
    tool prints in its own truncation line, RUN that exact command, and require
    its output to contain every hit.

    Against the pre-fix tool this fails: the printed command was the bare
    invocation that had just truncated, so following it reproduced the same ten
    lines and the same message. A remedy that loops spends the reader's trust
    before their attention."""
    corpus = [bead(f"SABLE-r{i:02d}", notes="must verify SABLE-X") for i in range(25)]

    first = _cli_against_stub_store(tmp_path, corpus, ["SABLE-X"])
    # POSITIVE CONTROL: the default run really is truncated, so the comparison
    # below is measuring a real difference and not an empty one.
    assert "more not shown" in first.stdout
    assert "SABLE-r24" not in first.stdout

    m = re.search(r"see them all with:\s*(.+?)\s*$", first.stdout, re.M)
    assert m, f"the truncation line must still name a remedy: {first.stdout!r}"
    remedy = shlex.split(m.group(1))
    assert remedy[0] == "sable-identifier-decay", remedy
    assert remedy[1:] != ["SABLE-X"], (
        "the remedy must not be the very command that just truncated")

    second = _cli_against_stub_store(tmp_path, corpus, remedy[1:])
    missing = [f"SABLE-r{i:02d}" for i in range(25) if f"SABLE-r{i:02d}" not in second.stdout]
    assert not missing, f"the printed remedy did not show: {missing}"
    assert "more not shown" not in second.stdout, "the remedy must terminate, not re-truncate"


@pytest.mark.parametrize("n_instructions", [0, 1, 25])
def test_no_printed_command_carries_trailing_punctuation(n_instructions):
    """Every rendering path that prints a command must print a PASTABLE one.
    A trailing period is the same defect as a looping remedy, one keystroke
    smaller: the reader follows the instruction exactly and it fails."""
    corpus = [bead(f"SABLE-i{i:02d}", notes="hold_until: SABLE-X must land first")
              for i in range(n_instructions)]
    corpus += [bead(f"SABLE-c{i}", description=(
        "This was observed on SABLE-X last week. We must check it again someday."))
        for i in range(3)]
    report = format_flags(sweep(["SABLE-X"], corpus), ["SABLE-X"])
    for line in report.splitlines():
        if "sable-identifier-decay" in line:
            assert not line.rstrip().endswith((".", ",", ";", ":")), line


def test_the_remedy_reproduces_the_branch_seam_invocation(tmp_path):
    """A remedy that drops --branch would print a command that sweeps for a
    DIFFERENT thing than the report it appears under."""
    corpus = [bead(f"SABLE-r{i:02d}", notes="DO NOT MERGE wk-foo while this hold stands")
              for i in range(12)]
    out = _cli_against_stub_store(tmp_path, corpus, ["--branch", "wk-foo"]).stdout
    m = re.search(r"see them all with:\s*(.+?)\s*$", out, re.M)
    assert m, out
    remedy = shlex.split(m.group(1))
    assert "--branch" in remedy and "--all" in remedy and "wk-foo" in remedy


def test_cli_reports_could_not_assess_when_bd_is_absent(tmp_path):
    """Discipline 7, the failure direction: no bd on PATH must produce a LOUD
    could-not-assess and exit 3 — never the silence of a clean sweep."""
    empty_path = tmp_path / "emptybin"
    empty_path.mkdir()
    import os
    env = dict(os.environ)
    env["PATH"] = str(empty_path)
    cp = subprocess.run([sys.executable, str(CLI), "SABLE-X"], text=True, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert cp.returncode == 3, cp.stderr
    assert "COULD NOT ASSESS" in cp.stdout
    assert "NOT a clean result" in cp.stdout
