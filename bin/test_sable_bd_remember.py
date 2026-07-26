#!/usr/bin/env python3
"""Unit tests for bin/sable-bd-remember (SABLE-tmbx1).

`bd create`/`bd update` already have a safe file/stdin escape hatch
(--body-file, --design-file, --stdin) and `bd note <id>` already has
--file/--stdin — this wrapper exists only because `bd remember` is the one
subcommand named in this repo's CLAUDE.md with no such path. These tests
cover arg parsing, content-fidelity through --file/--stdin (using REAL
metacharacter content, not an innocent fixture — see HAZARDOUS_INSIGHT), and
that the wrapper always invokes the real `bd` binary via a LIST argument,
never a shell string.
"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader("sable_bd_remember",
                          str(Path(__file__).resolve().parent / "sable-bd-remember"))
_SPEC = importlib.util.spec_from_loader("sable_bd_remember", _LOADER)
sable_bd_remember = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sable_bd_remember)

HAZARDOUS_INSIGHT = "see `hostname` and $(id) for the failing host — do not run this"


def test_parse_args_requires_exactly_one_source():
    with pytest.raises(SystemExit):
        sable_bd_remember.parse_args([])


def test_parse_args_rejects_multiple_sources():
    with pytest.raises(SystemExit):
        sable_bd_remember.parse_args(["inline text", "--file", "/tmp/x"])
    with pytest.raises(SystemExit):
        sable_bd_remember.parse_args(["inline text", "--stdin"])
    with pytest.raises(SystemExit):
        sable_bd_remember.parse_args(["--file", "/tmp/x", "--stdin"])


def test_parse_args_inline_alone_is_fine():
    ns = sable_bd_remember.parse_args([HAZARDOUS_INSIGHT, "--key", "k1"])
    assert ns.insight == HAZARDOUS_INSIGHT
    assert ns.key == "k1"


def test_resolve_insight_returns_inline_positional():
    ns = sable_bd_remember.parse_args([HAZARDOUS_INSIGHT])
    assert sable_bd_remember.resolve_insight(ns) == HAZARDOUS_INSIGHT


def test_resolve_insight_reads_hazardous_content_from_file_verbatim(tmp_path):
    path = tmp_path / "insight.txt"
    path.write_text(HAZARDOUS_INSIGHT, encoding="utf-8")
    ns = sable_bd_remember.parse_args(["--file", str(path)])
    assert sable_bd_remember.resolve_insight(ns) == HAZARDOUS_INSIGHT


def test_resolve_insight_reads_hazardous_content_from_stdin_dash():
    import io
    ns = sable_bd_remember.parse_args(["--file", "-"])
    assert sable_bd_remember.resolve_insight(ns, stdin=io.StringIO(HAZARDOUS_INSIGHT)) == HAZARDOUS_INSIGHT


def test_resolve_insight_stdin_flag_is_alias_for_file_dash():
    import io
    ns = sable_bd_remember.parse_args(["--stdin"])
    assert sable_bd_remember.resolve_insight(ns, stdin=io.StringIO(HAZARDOUS_INSIGHT)) == HAZARDOUS_INSIGHT


def test_main_invokes_bd_remember_with_a_list_argument_content_intact(tmp_path):
    # The core SABLE-tmbx1 assertion: the exact command handed to the runner
    # is a LIST (["bd", "remember", <content>, ...]) — never a shell string —
    # and <content> is byte-for-byte the hazardous text, backticks/$() and
    # all. A regression that reintroduced string-interpolation anywhere in
    # this path would corrupt or split the string and fail this.
    path = tmp_path / "insight.txt"
    path.write_text(HAZARDOUS_INSIGHT, encoding="utf-8")
    calls = []

    class FakeResult:
        returncode = 0

    def fake_runner(cmd):
        calls.append(cmd)
        return FakeResult()

    rc = sable_bd_remember.main(["--file", str(path), "--key", "tmbx1"], runner=fake_runner)
    assert rc == 0
    assert len(calls) == 1
    cmd = calls[0]
    assert isinstance(cmd, list)
    assert cmd == ["bd", "remember", HAZARDOUS_INSIGHT, "--key", "tmbx1"]


def test_main_without_key_omits_the_flag(tmp_path):
    path = tmp_path / "insight.txt"
    path.write_text("plain insight", encoding="utf-8")
    calls = []

    class FakeResult:
        returncode = 0

    def fake_runner(cmd):
        calls.append(cmd)
        return FakeResult()

    sable_bd_remember.main(["--file", str(path)], runner=fake_runner)
    assert calls[0] == ["bd", "remember", "plain insight"]


def test_main_returns_bd_returncode(tmp_path):
    path = tmp_path / "insight.txt"
    path.write_text("insight", encoding="utf-8")

    class FakeResult:
        returncode = 3

    rc = sable_bd_remember.main(["--file", str(path)], runner=lambda cmd: FakeResult())
    assert rc == 3


def test_main_default_runner_never_shells_out(monkeypatch, tmp_path):
    # Patches the name sable-bd-remember actually calls (`run`, imported via
    # `from subprocess import run`) rather than the subprocess module
    # attribute — the latter would NOT intercept the already-bound reference
    # and this test would fire a real `bd remember` against whatever DB the
    # test happens to run in. Never let that happen.
    path = tmp_path / "insight.txt"
    path.write_text(HAZARDOUS_INSIGHT, encoding="utf-8")
    calls = []

    class FakeResult:
        returncode = 0

    def spying_run(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResult()

    monkeypatch.setattr(sable_bd_remember, "run", spying_run)
    rc = sable_bd_remember.main(["--file", str(path)])
    assert rc == 0
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs.get("shell") is not True
    # the real bd invocation is a list, and args[0] is that list, not a string
    assert isinstance(args[0], list)
    assert args[0][:2] == ["bd", "remember"]
    assert args[0][2] == HAZARDOUS_INSIGHT


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
