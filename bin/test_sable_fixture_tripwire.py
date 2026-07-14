#!/usr/bin/env python3
"""Unit tests for bin/sable-fixture-tripwire (SABLE-0ssz.2).

Pure-logic tests of the analyzer: command-position cd detection, quote masking
(so detector-test string args like `assert_deny "..." 'git push'` are never
mistaken for real ops), continuation joining, the real-repo-git python rule, the
inline tripwire-allow marker, and KNOWN_VIOLATIONS excusal. The subprocess /
real-repo composition lives in the integration variant.
"""
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

_LOADER = SourceFileLoader(
    "sable_fixture_tripwire", str(Path(__file__).resolve().parent / "sable-fixture-tripwire")
)
_SPEC = importlib.util.spec_from_loader("sable_fixture_tripwire", _LOADER)
tw = importlib.util.module_from_spec(_SPEC)
# Register before exec so the frozen dataclass can resolve its string annotations
# (dataclasses looks up cls.__module__ in sys.modules).
sys.modules["sable_fixture_tripwire"] = tw
_LOADER.exec_module(tw)


# --- mask_quotes -------------------------------------------------------------

def test_mask_quotes_single():
    assert tw.mask_quotes("assert 'git push'") == "assert 'XXXXXXXX'"

def test_mask_quotes_double():
    assert tw.mask_quotes('run "cd /real"') == 'run "XXXXXXXX"'

def test_mask_quotes_preserves_unquoted():
    assert tw.mask_quotes('cd "$dir"') == 'cd "XXXX"'


# --- find_unguarded_cd -------------------------------------------------------

def test_bare_cd_flagged():
    assert tw.find_unguarded_cd('cd "$dir"') == ['cd "$dir"']

def test_cd_guarded_by_or_exit():
    assert tw.find_unguarded_cd('cd "$dir" || exit 1') == []

def test_cd_guarded_by_or_return():
    assert tw.find_unguarded_cd('  cd "$dir" || return 1') == []

def test_cd_guarded_by_and_chain():
    assert tw.find_unguarded_cd('cd "$dir" && git status') == []

def test_cd_navigation_back_not_flagged():
    assert tw.find_unguarded_cd('cd - >/dev/null') == []

def test_cd_dotdot_not_flagged():
    assert tw.find_unguarded_cd('cd ..') == []

def test_cd_inside_guarded_subshell_flagged_or_not():
    # `( cd x || exit 1; ... )` — the || guards it.
    assert tw.find_unguarded_cd('( cd "$d" || exit 1; git init )') == []
    # `( cd x; ... )` with no guard IS an escape (subshell isolates CWD but a
    # failed cd still runs later cmds in the parent's CWD).
    assert tw.find_unguarded_cd('( cd "$d"; git init )') == ['( cd "$d"; git init )']

def test_cd_in_quoted_string_arg_not_flagged():
    # a detector-test string arg, not a real command
    assert tw.find_unguarded_cd('''is_case "runs cd /real then rm"''') == []

def test_cd_fixture_helper_call_not_matched():
    # `cd_fixture "$x"` is a function call, not a bare `cd`
    assert tw.find_unguarded_cd('cd_fixture "$FIXTURE_REPO"') == []

def test_cd_after_semicolon_command_position():
    assert tw.find_unguarded_cd('mkdir -p "$d"; cd "$d"') == ['mkdir -p "$d"; cd "$d"']

def test_comment_only_cd_not_flagged():
    assert tw.find_unguarded_cd('# then cd "$dir" into the fixture') == []


# --- continuation joining ----------------------------------------------------

def test_continuation_join_makes_and_guard_visible():
    text = '( cd "$V" \\\n    && echo x \\\n    && git push -q origin main )\n'
    assert tw.scan_shell_text(text, "f.sh") == []

def test_scan_shell_reports_start_line():
    text = 'echo a\ncd "$dir"\necho b\n'
    vs = tw.scan_shell_text(text, "f.sh")
    assert len(vs) == 1 and vs[0].line == 2 and vs[0].rule == "cd-unguarded"


# --- inline allow marker -----------------------------------------------------

def test_allow_marker_suppresses():
    text = 'cd "$dir"  # tripwire-allow: intentional escape repro\n'
    assert tw.scan_shell_text(text, "f.sh") == []


# --- python real-repo-git ----------------------------------------------------

_PY_REAL = (
    "from pathlib import Path\n"
    "import subprocess\n"
    "repo = Path(__file__).resolve().parent.parent\n"
    'subprocess.run(["git", "-C", str(repo), "branch", "-D", wt])\n'
)

def test_real_repo_git_flagged():
    vs = tw.scan_python_text(_PY_REAL, "test_x.py")
    assert any(v.rule == "real-repo-git" and v.line == 4 for v in vs)

def test_fixture_scoped_git_not_flagged():
    text = (
        "import subprocess\n"
        'subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "x"])\n'
    )
    assert tw.scan_python_text(text, "test_x.py") == []

def test_real_repo_read_only_not_flagged():
    # reading (rev-parse / status) the real repo is not a mutation
    text = (
        "from pathlib import Path\n"
        "repo = Path(__file__).resolve().parent.parent\n"
        'subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"])\n'
    )
    assert tw.scan_python_text(text, "test_x.py") == []

def test_embedded_shell_cd_in_python_flagged():
    text = 'script = f\'\'\'\ncd "{src}"\ndolt push origin main\n\'\'\'\n'
    vs = tw.scan_python_text(text, "test_x.py")
    assert any(v.rule == "cd-unguarded" for v in vs)


# --- excusal -----------------------------------------------------------------

def test_known_violation_excused():
    v = tw.Violation("bin/test_sable_dolt_push_integration.py", 274, "cd-unguarded", 'cd "{src}"')
    assert v.excused() is True

def test_unknown_violation_not_excused():
    v = tw.Violation("hooks/test/test-new.sh", 5, "cd-unguarded", 'cd "$dir"')
    assert v.excused() is False


# --- end-to-end main() on the real repo --------------------------------------

def test_main_clean_on_real_repo():
    # acceptance (b): passes on the audited-clean suite
    assert tw.main([]) == 0
