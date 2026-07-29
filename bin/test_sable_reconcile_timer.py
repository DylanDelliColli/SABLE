#!/usr/bin/env python3
"""Unit tests for bin/sable-reconcile-timer (SABLE-jfg6.5 / D3 TIMER LEG).

Pure functions only — repo-resolution precedence, config-file parsing, and
argv construction — no subprocess, no bd, no git. The real end-to-end (a
process invocation with no tmux server, no live panes, that still scans and
files a for-chuck bead) lives in the S5-U1/S5-E1 additions to
bin/test_sable_reconcile_handoffs_integration.py.
"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_reconcile_timer", str(Path(__file__).resolve().parent / "sable-reconcile-timer")
)
_SPEC = importlib.util.spec_from_loader("sable_reconcile_timer", _LOADER)
srt = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(srt)


# ===========================================================================
# repo_from_config — config-file parsing
# ===========================================================================

def test_repo_from_config_missing_file_returns_none(tmp_path):
    assert srt.repo_from_config(tmp_path / "nope.conf") is None


def test_repo_from_config_bare_path_line(tmp_path):
    cfg = tmp_path / "reconcile-timer.conf"
    cfg.write_text("/home/op/repo\n")
    assert srt.repo_from_config(cfg) == "/home/op/repo"


def test_repo_from_config_key_value_line_form(tmp_path):
    cfg = tmp_path / "reconcile-timer.conf"
    cfg.write_text("repo=/home/op/other-repo\n")
    assert srt.repo_from_config(cfg) == "/home/op/other-repo"


def test_repo_from_config_skips_blank_and_comment_lines(tmp_path):
    cfg = tmp_path / "reconcile-timer.conf"
    cfg.write_text("\n# a comment\n\n/home/op/repo\n")
    assert srt.repo_from_config(cfg) == "/home/op/repo"


def test_repo_from_config_all_blank_or_comment_returns_none(tmp_path):
    cfg = tmp_path / "reconcile-timer.conf"
    cfg.write_text("# nothing here\n\n")
    assert srt.repo_from_config(cfg) is None


# ===========================================================================
# resolve_repo — CLI > env > config precedence; NEVER falls back to PWD
# ===========================================================================

def test_resolve_repo_cli_wins_over_env_and_config(tmp_path):
    cfg = tmp_path / "reconcile-timer.conf"
    cfg.write_text("/from/config\n")
    got = srt.resolve_repo("/from/cli", cfg, env={"SABLE_RECONCILE_REPO": "/from/env"})
    assert got == "/from/cli"


def test_resolve_repo_env_wins_over_config(tmp_path):
    cfg = tmp_path / "reconcile-timer.conf"
    cfg.write_text("/from/config\n")
    got = srt.resolve_repo(None, cfg, env={"SABLE_RECONCILE_REPO": "/from/env"})
    assert got == "/from/env"


def test_resolve_repo_falls_through_to_config(tmp_path):
    cfg = tmp_path / "reconcile-timer.conf"
    cfg.write_text("/from/config\n")
    got = srt.resolve_repo(None, cfg, env={})
    assert got == "/from/config"


def test_resolve_repo_none_when_nothing_resolves(tmp_path):
    # NOT the caller's PWD, NOT $TMUX_PANE — an unresolvable repo is None, full stop.
    got = srt.resolve_repo(None, tmp_path / "absent.conf", env={})
    assert got is None


def test_resolve_repo_never_consults_tmux_pane_env(tmp_path):
    # A pane-context var must never leak into repo resolution — only the
    # documented SABLE_RECONCILE_REPO key counts.
    got = srt.resolve_repo(None, tmp_path / "absent.conf", env={"TMUX_PANE": "%3"})
    assert got is None


# ===========================================================================
# build_handoffs_argv — forwards flags to sable-reconcile-handoffs
# ===========================================================================

def test_build_handoffs_argv_minimal():
    argv = srt.build_handoffs_argv("/repo", remote="origin", age_min=None, dry_run=False)
    assert argv[-4:] == ["--repo", "/repo", "--remote", "origin"]
    assert "--age-min" not in argv
    assert "--dry-run" not in argv


def test_build_handoffs_argv_forwards_age_min_and_dry_run():
    argv = srt.build_handoffs_argv("/repo", remote="upstream", age_min=5.0, dry_run=True)
    assert "--age-min" in argv and argv[argv.index("--age-min") + 1] == "5.0"
    assert "--dry-run" in argv
    assert "--remote" in argv and argv[argv.index("--remote") + 1] == "upstream"


# ===========================================================================
# CLI parsing — --interval-min default + env override, --once usage error
# ===========================================================================

def test_main_errors_when_repo_unresolvable(tmp_path, monkeypatch, capsys):
    # argparse's parser.error() prints usage + message to stderr then raises
    # SystemExit(2) — it never returns, so main() itself never gets to `return 2`.
    monkeypatch.delenv("SABLE_RECONCILE_REPO", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        srt.main(["--once", "--config", str(tmp_path / "absent.conf")])
    assert exc_info.value.code == 2
    assert "no repo resolvable" in capsys.readouterr().err


def test_main_interval_min_default_is_15(monkeypatch):
    monkeypatch.delenv("SABLE_RECONCILE_INTERVAL_MIN", raising=False)
    parser_default = srt.DEFAULT_INTERVAL_MIN
    assert parser_default == 15.0


def test_main_once_runs_a_single_sweep_and_returns_its_rc(tmp_path, monkeypatch):
    calls = []

    def fake_run_once(repo, *, remote, age_min, dry_run):
        calls.append((repo, remote, age_min, dry_run))
        return 0

    monkeypatch.setattr(srt, "run_once", fake_run_once)
    rc = srt.main(["--once", "--repo", "/repo", "--remote", "origin"])
    assert rc == 0
    assert calls == [("/repo", "origin", None, False)]


# ===========================================================================
# SABLE-5xz68 DEFECT TWO — multi-repo: this host runs more than one fleet, so
# one repo per timer is one protected fleet and N-1 unprotected ones.
# ===========================================================================

def test_repos_from_config_returns_every_line_not_just_the_first(tmp_path):
    cfg = tmp_path / "reconcile-timer.conf"
    cfg.write_text("# fleets\n/home/op/a\nrepo=/home/op/b\n\n/home/op/c\n")
    assert srt.repos_from_config(cfg) == ["/home/op/a", "/home/op/b", "/home/op/c"]


def test_repo_from_config_still_returns_the_first_line(tmp_path):
    # back-compat: the single-repo accessor is the head of the list
    cfg = tmp_path / "reconcile-timer.conf"
    cfg.write_text("/home/op/a\n/home/op/b\n")
    assert srt.repo_from_config(cfg) == "/home/op/a"


def test_resolve_repos_cli_is_repeatable(tmp_path):
    got = srt.resolve_repos(["/a", "/b"], tmp_path / "absent.conf", env={})
    assert got == ["/a", "/b"]


def test_resolve_repos_env_is_pathsep_separated(tmp_path):
    got = srt.resolve_repos(None, tmp_path / "absent.conf",
                            env={"SABLE_RECONCILE_REPO": "/a:/b"})
    assert got == ["/a", "/b"]


def test_resolve_repos_falls_through_to_all_config_lines(tmp_path):
    cfg = tmp_path / "reconcile-timer.conf"
    cfg.write_text("/a\n/b\n")
    assert srt.resolve_repos(None, cfg, env={}) == ["/a", "/b"]


def test_resolve_repos_empty_when_nothing_resolves(tmp_path):
    assert srt.resolve_repos(None, tmp_path / "absent.conf", env={}) == []


def test_main_once_sweeps_every_repo_even_when_one_fails(monkeypatch):
    # A failure in fleet A must NOT skip fleet B — an unswept fleet is an
    # unprotected fleet, and the rc must still report the failure.
    calls = []

    def fake_run_once(repo, *, remote, age_min, dry_run):
        calls.append(repo)
        return 4 if repo == "/a" else 0

    monkeypatch.setattr(srt, "run_once", fake_run_once)
    rc = srt.main(["--once", "--repo", "/a", "--repo", "/b"])
    assert calls == ["/a", "/b"]
    assert rc == 4


# ===========================================================================
# SABLE-5xz68 DEFECT ONE — a staged unit file is not a schedule. These pin the
# post-install check that must fail LOUDLY when nothing is actually scheduled,
# and (the vacuous-green guard) when something is scheduled for the wrong repo.
# ===========================================================================

_UNIT = (
    "[Service]\n"
    "ExecStart=%h/.local/bin/sable-reconcile-timer --once --repo /home/op/a --repo /home/op/b\n"
)
_CRON = (
    "# SABLE reconciliation floor sweep\n"
    '*/15 * * * * PATH="$PATH:$HOME/.local/bin" sable-reconcile-timer --once '
    '--repo /home/op/a >> "$HOME/.cache/sable/reconcile-timer.log" 2>&1\n'
)


def test_parse_scheduled_repos_reads_every_repo_flag_off_an_exec_line():
    assert srt.parse_scheduled_repos(_UNIT) == ["/home/op/a", "/home/op/b"]


def test_parse_scheduled_repos_reads_the_cron_line():
    assert srt.parse_scheduled_repos(_CRON) == ["/home/op/a"]


def test_parse_scheduled_repos_ignores_lines_not_invoking_the_timer():
    assert srt.parse_scheduled_repos("*/5 * * * * some-other-tool --repo /x\n") == []


def test_parse_scheduled_repos_ignores_commented_cron_lines():
    assert srt.parse_scheduled_repos("# sable-reconcile-timer --once --repo /x\n") == []


def test_schedule_problems_reports_no_schedule_when_nothing_is_installed():
    # The exact SABLE-5xz68 defect-one state: units staged on disk, nothing
    # scheduled anywhere. Must be a problem, not silence.
    problems = srt.schedule_problems(systemd_active=False, unit_text="",
                                     crontab_text="", expected_repos=["/home/op/a"])
    assert problems
    assert any("NO SCHEDULE" in p for p in problems)


def test_schedule_problems_reports_no_schedule_even_with_no_expected_repos():
    problems = srt.schedule_problems(systemd_active=False, unit_text="",
                                     crontab_text="", expected_repos=[])
    assert any("NO SCHEDULE" in p for p in problems)


def test_schedule_problems_clean_when_systemd_timer_covers_every_repo():
    problems = srt.schedule_problems(systemd_active=True, unit_text=_UNIT,
                                     crontab_text="",
                                     expected_repos=["/home/op/a", "/home/op/b"])
    assert problems == []


def test_schedule_problems_clean_when_cron_covers_the_repo():
    problems = srt.schedule_problems(systemd_active=False, unit_text="",
                                     crontab_text=_CRON, expected_repos=["/home/op/a"])
    assert problems == []


def test_schedule_problems_flags_a_schedule_that_targets_the_wrong_repo():
    # DEFECT TWO as a live state: installed, running, protecting nothing here.
    # This is the MORE dangerous state — it must never read as clean.
    problems = srt.schedule_problems(systemd_active=True, unit_text=_UNIT,
                                     crontab_text="",
                                     expected_repos=["/home/op/costing-comparison"])
    assert problems
    assert any("/home/op/costing-comparison" in p for p in problems)
    assert not any("NO SCHEDULE" in p for p in problems)


def test_schedule_problems_normalizes_trailing_slashes_and_dot_segments():
    problems = srt.schedule_problems(systemd_active=True, unit_text=_UNIT,
                                     crontab_text="",
                                     expected_repos=["/home/op/./a/"])
    assert problems == []


def test_schedule_problems_ignores_an_inactive_systemd_unit():
    # A unit file copied into ~/.config/systemd/user but never enabled is not
    # a schedule — the exact 'looks installed' trap.
    problems = srt.schedule_problems(systemd_active=False, unit_text=_UNIT,
                                     crontab_text="", expected_repos=["/home/op/a"])
    assert any("NO SCHEDULE" in p for p in problems)
