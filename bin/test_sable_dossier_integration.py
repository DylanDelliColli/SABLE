#!/usr/bin/env python3
"""Integration tests for bin/sable-dossier (SABLE-lykc.1).

Real composition: a real temp git repo, real state JSONs on disk at the
canonical <root>/.claude/sable/state/planning/<epic-id>/ location, and the
actual CLI script executed via subprocess — no mocking.
"""
import json
import subprocess
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent / "sable-dossier"

FRAMING = {
    "stories": [{"id": "S1", "title": "user can resize login cards"}],
    "success_metric": "resize round-trips in under 200ms",
}

TEST_STRATEGY = {
    "stories": [
        {
            "id": "S1",
            "title": "user can resize login cards",
            "impl_beads": [{"id": "EPIC-1.1", "title": "resize handler"}],
            "cases": [
                {"name": "rejects invalid size enum", "layer": "UNIT", "status": "planned"},
                {"name": "full flow: click to persist", "layer": "E2E", "status": "gap"},
            ],
        }
    ],
    "coverage": {"covered": 1, "total": 2},
}


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    state = repo / ".claude" / "sable" / "state" / "planning" / "EPIC-1"
    state.mkdir(parents=True)
    (state / "framing.json").write_text(json.dumps(FRAMING))
    (state / "test-strategy.json").write_text(json.dumps(TEST_STRATEGY))
    return repo, state


def run_cli(args, cwd, env_home):
    return subprocess.run(
        [sys.executable, str(BIN), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(env_home)},
    )


def test_cli_renders_from_repo_cwd(tmp_path):
    repo, state = make_repo(tmp_path)
    r = run_cli(["EPIC-1", "--highlight", "test-strategy"], cwd=repo, env_home=tmp_path)
    assert r.returncode == 0, r.stderr
    out = Path(r.stdout.strip())
    assert out == state / "dossier.html"
    assert out.exists()
    text = out.read_text()
    assert "user can resize login cards" in text
    assert "rejects invalid size enum" in text
    assert "awaiting signoff" in text
    assert "not yet produced" in text  # research/architecture/decomposition absent


def test_cli_explicit_state_dir_and_out(tmp_path):
    repo, state = make_repo(tmp_path)
    out_path = tmp_path / "elsewhere" / "page.html"
    r = run_cli(
        ["EPIC-1", "--state-dir", str(state), "--out", str(out_path)],
        cwd=tmp_path,
        env_home=tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert Path(r.stdout.strip()) == out_path
    assert "full flow: click to persist" in out_path.read_text()


def test_cli_bad_highlight_rejected(tmp_path):
    repo, _ = make_repo(tmp_path)
    r = run_cli(["EPIC-1", "--highlight", "bogus"], cwd=repo, env_home=tmp_path)
    assert r.returncode != 0


def test_cli_empty_state_dir_still_renders(tmp_path):
    repo = tmp_path / "repo2"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    r = run_cli(["EPIC-9"], cwd=repo, env_home=tmp_path)
    assert r.returncode == 0, r.stderr
    text = Path(r.stdout.strip()).read_text()
    assert text.count("not yet produced") == 5
