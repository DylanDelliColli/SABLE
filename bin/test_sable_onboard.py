#!/usr/bin/env python3
"""Tests for bin/sable_stack_detect.py — the /sable-onboarding stack + .sable
library (SABLE-gn7a.1). Columbo S4 detection matrix plus the grammar CRITICALs.

Integration character (per CLAUDE.md unit+integration requirement): detection
runs against a REAL temp filesystem; execute_once runs REAL `sh -c` subprocesses;
and test_scan_contract_matches_lib_identity_resolvers sources and calls the REAL
bash resolvers in hooks/multi-manager/lib-identity.sh, so the module's accept/
reject is proven against the actual one-parser grammar rather than a copy of it.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent
_REPO_ROOT = _BIN.parent
sys.path.insert(0, str(_BIN))

import sable_stack_detect as ssd  # noqa: E402
import sable_ci_template as sct  # noqa: E402


# ===========================================================================
# (2) Stack detection — columbo S4 matrix
# ===========================================================================

def _touch(d: Path, name: str, body: str = ""):
    (d / name).write_text(body, encoding="utf-8")


def test_detect_command_keyed_on_lockfile_not_bare_manifest(tmp_path):
    """CRITICAL pitfall: a pnpm repo must yield `pnpm test`, never `npm test`,
    even though package.json is present. Keying is on the LOCKFILE."""
    _touch(tmp_path, "package.json", '{"scripts": {"test": "vitest"}}')
    _touch(tmp_path, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")

    det = ssd.detect_stack(str(tmp_path))

    assert det.commands == ["pnpm test"]
    assert "npm test" not in det.commands


def test_detect_command_keyed_on_package_manager_field_without_lockfile(tmp_path):
    """Absent a lockfile, corepack's `packageManager` field keys the manager —
    still never a bare-manifest npm default."""
    _touch(tmp_path, "package.json", '{"packageManager": "yarn@4.1.0"}')

    det = ssd.detect_stack(str(tmp_path))

    assert det.commands == ["yarn test"]


def test_bare_package_json_yields_no_js_candidate(tmp_path):
    """A package.json with no lockfile and no packageManager field is the exact
    case pre-push gets wrong; here it produces NO candidate (skill asks)."""
    _touch(tmp_path, "package.json", '{"name": "x", "scripts": {"test": "x"}}')

    det = ssd.detect_stack(str(tmp_path))

    assert det.commands == []
    assert det.signal == ssd.NONE


def test_detect_command_python_go_rust_make(tmp_path):
    """Non-JS stacks: python configs -> pytest, go.mod -> go test, Cargo.toml
    -> cargo test, a Makefile `test:` target -> make test."""
    py = tmp_path / "py"
    py.mkdir()
    _touch(py, "pyproject.toml", "[tool.pytest.ini_options]\n")
    assert ssd.detect_stack(str(py)).commands == ["pytest"]

    for cfg in ("pytest.ini", "setup.cfg"):
        d = tmp_path / cfg.replace(".", "_")
        d.mkdir()
        _touch(d, cfg, "[pytest]\n")
        assert ssd.detect_stack(str(d)).commands == ["pytest"]

    go = tmp_path / "go"
    go.mkdir()
    _touch(go, "go.mod", "module x\n")
    assert ssd.detect_stack(str(go)).commands == ["go test ./..."]

    rust = tmp_path / "rust"
    rust.mkdir()
    _touch(rust, "Cargo.toml", "[package]\nname = \"x\"\n")
    assert ssd.detect_stack(str(rust)).commands == ["cargo test"]

    mk = tmp_path / "mk"
    mk.mkdir()
    _touch(mk, "Makefile", ".PHONY: test\ntest: deps\n\tpytest\n")
    assert ssd.detect_stack(str(mk)).commands == ["make test"]


def test_makefile_without_test_target_yields_nothing(tmp_path):
    """A Makefile whose only target is `build` (and a `.PHONY: test` decl that
    is NOT a rule) must not falsely surface `make test`."""
    _touch(tmp_path, "Makefile", ".PHONY: build\nbuild:\n\tgcc x.c\n")

    assert ssd.detect_stack(str(tmp_path)).commands == []


def test_no_detectable_framework_yields_explicit_ask(tmp_path):
    """An empty repo returns the explicit NONE signal — not a silent empty that
    could be mistaken for 'detection did not run'."""
    det = ssd.detect_stack(str(tmp_path))

    assert det.candidates == ()
    assert det.detected is False
    assert det.signal == ssd.NONE
    assert ssd.NONE == "none"


def test_multistack_repo_surfaces_all_candidates(tmp_path):
    """detect-and-ask, never pick-dominant: a repo carrying pnpm + go + rust +
    a Makefile test target surfaces EVERY candidate, none suppressed."""
    _touch(tmp_path, "package.json", '{"name": "x"}')
    _touch(tmp_path, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    _touch(tmp_path, "go.mod", "module x\n")
    _touch(tmp_path, "Cargo.toml", "[package]\nname = \"x\"\n")
    _touch(tmp_path, "Makefile", "test:\n\tgo test ./...\n")

    cmds = set(ssd.detect_stack(str(tmp_path)).commands)

    assert cmds == {"pnpm test", "go test ./...", "cargo test", "make test"}


# ===========================================================================
# (1) .sable grammar CRITICALs
# ===========================================================================

def test_written_sable_is_exactly_confirmed_single_line(tmp_path):
    """CRITICAL (priority<=1 regression class): the persisted testCommand= line
    is byte-for-byte `testCommand=<confirmed>` with no reflow, escaping, or
    stray bytes — the resolver must read back exactly what was confirmed."""
    confirmed = "python3 -m pytest -q && npm run lint"
    ok = ssd.ExecuteResult(confirmed, 0, False, "")
    sable = tmp_path / ".sable"

    ssd.write(str(sable), test_command=confirmed, execute_result=ok)

    assert sable.read_bytes() == b"testCommand=" + confirmed.encode() + b"\n"
    # And the real resolver reads back the identical value.
    assert _shell_resolve("sable_resolve_test_command", str(tmp_path)) == confirmed


def test_build_sable_emits_both_keys_single_line_each():
    content = ssd.build_sable(test_command="pytest -q", integration_branch="tmux-only")
    assert content == "testCommand=pytest -q\nintegrationBranch=tmux-only\n"


def test_build_sable_rejects_multiline_value():
    with pytest.raises(ValueError):
        ssd.build_sable(test_command="pytest\nrm -rf /")


def test_validate_classifies_each_line():
    text = (
        "testCommand=npm test\n"      # accept
        "# a comment\n"               # reject
        " integrationBranch=main\n"   # reject: leading space
        "integrationBranch=tmux-only\n"  # accept
        "TestCommand=nope\n"          # reject: wrong case
    )
    verdicts = [(lc.verdict, lc.key, lc.value) for lc in ssd.validate(text)]
    assert verdicts == [
        ("accept", "testCommand", "npm test"),
        ("reject", None, None),
        ("reject", None, None),
        ("accept", "integrationBranch", "tmux-only"),
        ("reject", None, None),
    ]


# ===========================================================================
# (3) Execute-once gates the write
# ===========================================================================

def test_execute_once_passing_command_allows_write(tmp_path):
    res = ssd.execute_once("true", cwd=str(tmp_path))
    assert res.ok is True
    assert res.exit_code == 0

    sable = tmp_path / ".sable"
    content = ssd.write(str(sable), test_command="true", execute_result=res)

    assert content == "testCommand=true\n"
    assert sable.read_text() == "testCommand=true\n"


def test_execute_once_failing_command_blocks_write(tmp_path):
    res = ssd.execute_once("false", cwd=str(tmp_path))
    assert res.ok is False
    assert res.exit_code != 0

    sable = tmp_path / ".sable"
    with pytest.raises(ssd.WriteRefused) as exc:
        ssd.write(str(sable), test_command="false", execute_result=res)

    # exit surfaced, and nothing written.
    assert exc.value.exit_code == res.exit_code
    assert str(res.exit_code) in str(exc.value)
    assert not sable.exists()


def test_write_refuses_without_execute_result(tmp_path):
    sable = tmp_path / ".sable"
    with pytest.raises(ssd.WriteRefused):
        ssd.write(str(sable), test_command="pytest")
    assert not sable.exists()


def test_execute_once_output_folds_stderr():
    res = ssd.execute_once("echo out; echo err 1>&2")
    assert "out" in res.output and "err" in res.output


# ===========================================================================
# CRITICAL: shared-fixture contract vs the REAL lib-identity.sh resolvers
# ===========================================================================

_LIB_IDENTITY = _REPO_ROOT / "hooks" / "multi-manager" / "lib-identity.sh"


def _shell_resolve(func: str, repo_path: str) -> str:
    """Invoke the REAL bash resolver `func` (from lib-identity.sh) on repo_path,
    isolated from session env and global/system git config so only the .sable
    file's contribution is observed. Returns stdout verbatim (resolvers
    `printf '%s'` with no trailing newline)."""
    script = "source '%s'; %s '%s'" % (_LIB_IDENTITY, func, repo_path)
    env = {k: v for k, v in os.environ.items()
           if k not in ("SABLE_TEST_COMMAND", "SABLE_INTEGRATION_BRANCH",
                        "SABLE_BASE_BRANCH")}
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    proc = subprocess.run(
        ["bash", "-c", script], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    return proc.stdout


# Shared fixture-line set (columbo): valid line, leading-space key, space around
# `=`, comment line, wrong-case key, multi-line (first-wins), and missing file.
# These are the SAME line categories test-lib-identity.sh exercises against the
# resolvers; here each is asserted to agree with the module line-for-line.
def _fixtures(key: str, value: str, second: str):
    cap = key[0].upper() + key[1:]
    return [
        # (label, file_body | None-for-missing, expect_accept, expect_value)
        ("valid line", "%s=%s" % (key, value), True, value),
        ("leading-space key", " %s=%s" % (key, value), False, None),
        ("space around =", "%s = %s" % (key, value), False, None),
        ("comment line", "#%s=%s" % (key, value), False, None),
        ("wrong-case key", "%s=%s" % (cap, value), False, None),
        ("multi-line first-wins", "%s=%s\n%s=%s" % (key, value, key, second),
         True, value),
        ("missing file", None, False, None),
    ]


# (key, resolver func, fallback the resolver prints when no .sable value matches)
_KEY_MATRIX = [
    ("testCommand", "sable_resolve_test_command", "", "npm test", "vitest"),
    ("integrationBranch", "sable_resolve_integration_branch", "main",
     "tmux-only", "release"),
]


@pytest.mark.parametrize("key,func,fallback,value,second", _KEY_MATRIX)
def test_scan_contract_matches_lib_identity_resolvers(
    tmp_path, key, func, fallback, value, second
):
    """CRITICAL: for every shared fixture line, the module's accept/reject (via
    parse_file) must agree with what the REAL resolver returns — an accepted
    line resolves to exactly the module's value; a rejected line leaves the
    resolver on its fallback (empty for testCommand, `main` for
    integrationBranch)."""
    for i, (label, body, expect_accept, expect_value) in enumerate(
        _fixtures(key, value, second)
    ):
        repo = tmp_path / ("f%d" % i)
        repo.mkdir()
        sable = repo / ".sable"
        if body is not None:
            sable.write_text(body + "\n", encoding="utf-8")

        module_val = ssd.parse_file(str(sable), key)
        shell_val = _shell_resolve(func, str(repo))

        if expect_accept:
            assert module_val == expect_value, "%s: module parse mismatch" % label
            assert shell_val == expect_value, "%s: resolver mismatch" % label
            assert module_val == shell_val, "%s: module vs resolver" % label
        else:
            assert module_val is None, "%s: module should reject" % label
            # Rejected line -> resolver falls through to its fallback.
            assert shell_val == fallback, "%s: resolver should fall back" % label


# ===========================================================================
# sable_ci_template — render + provider detection (SABLE-gn7a.2)
#
# Integration character: the render tests exercise the REAL checked-in template
# at templates/ci-verify-project.yml (not a fixture copy), and the provider
# tests run against REAL git repos + real filesystem CI-config layouts. A
# yaml-guarded test loads the rendered output through a REAL YAML parser so any
# indentation error in the substituted runtime block reds the gate.
# ===========================================================================

_TEMPLATE = _REPO_ROOT / "templates" / "ci-verify-project.yml"

# SABLE-specific suite steps that a portable, rendered workflow must NEVER carry
# — they belong only to THIS repo's own .github/workflows/ci-verify.yml. These
# are the exact step-command fragments the bead names; the template's header
# comment may still *describe* the gate (e.g. name sable-merge-gate) — what must
# never appear is an actual step running one of these SABLE-only suites.
_SABLE_ONLY_STEPS = (
    "shell-run-set",
    "pytest bin/",
    "fixture-tripwire",
)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# --- render -----------------------------------------------------------------

def test_generated_workflow_triggers_on_ci_verify_glob():
    """The rendered trigger keeps the load-bearing ci-verify/** glob (SABLE-ad21)
    and the workflow_dispatch escape hatch, and substitutes the confirmed
    integration branch — with no render placeholders left behind."""
    out = sct.render_workflow(
        integration_branch="main", test_command="pytest -q", kind="python")

    assert "- 'ci-verify/**'" in out
    assert "- 'main'" in out            # integration branch substituted
    assert "workflow_dispatch" in out
    # every render placeholder is resolved (GitHub's own ${{ github.ref }} stays)
    assert "{{INTEGRATION_BRANCH}}" not in out
    assert "{{RUNTIME_SETUP}}" not in out
    assert "{{TEST_COMMAND}}" not in out


def test_generated_workflow_runs_confirmed_testcommand():
    """The confirmed testCommand is run verbatim, and the rendered output carries
    NONE of this repo's SABLE-specific suite steps."""
    cmd = "python3 -m pytest -q && npm run lint"
    out = sct.render_workflow(
        integration_branch="tmux-only", test_command=cmd, kind="python")

    assert cmd in out
    assert "{{TEST_COMMAND}}" not in out
    for banned in _SABLE_ONLY_STEPS:
        assert banned not in out, "rendered workflow leaked SABLE-only step: %s" % banned


def test_generated_workflow_optional_blocks_present_but_commented():
    """The git-identity and default-branch-pin optional blocks (SABLE-59zu/r1zs)
    survive rendering, and every one of their content lines stays commented — a
    portable repo opts in, it does not inherit them live."""
    out = sct.render_workflow(
        integration_branch="main", test_command="pytest", kind="python")
    lines = out.splitlines()

    content = [l for l in lines
               if "user.email" in l or "user.name" in l or "init.defaultBranch" in l]
    assert content, "optional git-identity / default-branch blocks missing"
    for l in content:
        assert l.lstrip().startswith("#"), "optional block line not commented: %r" % l

    assert any("Configure git identity" in l and l.lstrip().startswith("#") for l in lines)
    assert any("Pin default branch" in l and l.lstrip().startswith("#") for l in lines)


def test_template_static_shape_has_required_contract():
    """The CHECKED-IN template itself carries the non-negotiable contract:
    name, ci-verify/** trigger, workflow_dispatch, concurrency+cancel,
    contents:read permission, checkout fetch-depth 0, both render anchors, and
    the commented optional blocks — and no SABLE-only suite steps."""
    text = _TEMPLATE.read_text(encoding="utf-8")

    assert "name: ci-verify" in text
    assert "- 'ci-verify/**'" in text
    assert "{{INTEGRATION_BRANCH}}" in text
    assert "workflow_dispatch" in text

    assert "concurrency:" in text
    assert "cancel-in-progress: true" in text

    assert "permissions:" in text
    assert "contents: read" in text

    assert "actions/checkout@v4" in text
    assert "fetch-depth: 0" in text

    assert "{{RUNTIME_SETUP}}" in text
    assert "{{TEST_COMMAND}}" in text

    lines = text.splitlines()
    assert any("init.defaultBranch" in l and l.lstrip().startswith("#") for l in lines)
    assert any("user.email" in l and l.lstrip().startswith("#") for l in lines)

    for banned in _SABLE_ONLY_STEPS:
        assert banned not in text, "template carries a SABLE-only step: %s" % banned


def test_runtime_setup_keyed_to_detected_stack():
    """Runtime setup is keyed to the stack: python -> setup-python, each JS
    manager -> its own install, go -> setup-go, rust -> rustup; an unknown /
    none kind renders a commented placeholder, never a broken step."""
    assert "actions/setup-python@v5" in sct.render_workflow(
        integration_branch="main", test_command="t", kind="python")

    pnpm = sct.render_workflow(integration_branch="main", test_command="t", kind="js/pnpm")
    assert "pnpm install --frozen-lockfile" in pnpm and "actions/setup-node@v4" in pnpm

    assert "npm ci" in sct.render_workflow(
        integration_branch="main", test_command="t", kind="js/npm")
    assert "actions/setup-go@v5" in sct.render_workflow(
        integration_branch="main", test_command="t", kind="go")
    assert "rustup toolchain install" in sct.render_workflow(
        integration_branch="main", test_command="t", kind="rust")

    none_out = sct.render_workflow(integration_branch="main", test_command="t", kind=None)
    assert "No language runtime detected" in none_out


def test_kind_for_command_selects_matching_candidate():
    """kind_for_command picks the kind of the candidate whose command the human
    confirmed, and returns None for a hand-typed command with no match."""
    det = ssd.Detection((
        ssd.Candidate("pnpm test", "pnpm-lock.yaml", "js/pnpm"),
        ssd.Candidate("go test ./...", "go.mod", "go"),
    ))
    assert sct.kind_for_command(det, "go test ./...") == "go"
    assert sct.kind_for_command(det, "pnpm test") == "js/pnpm"
    assert sct.kind_for_command(det, "make bespoke") is None


@pytest.mark.parametrize("kind", ["python", "js/pnpm", "js/yarn", "js/npm", "go", "rust", None])
def test_rendered_workflow_is_valid_yaml(kind):
    """Every runtime block indents to valid YAML: parse the rendered output and
    assert the whole contract survives (trigger, dispatch, concurrency,
    permissions, checkout fetch-depth 0, and a step running the confirmed
    command). Guarded on PyYAML — the clean-room installs only pytest."""
    yaml = pytest.importorskip("yaml")
    cmd = "pytest -q && echo done"
    out = sct.render_workflow(
        integration_branch="release", test_command=cmd, kind=kind)
    doc = yaml.safe_load(out)

    assert doc["name"] == "ci-verify"
    # YAML 1.1 parses the bare key `on:` as boolean True — accept either.
    on = doc.get("on", doc.get(True))
    assert "ci-verify/**" in on["push"]["branches"]
    assert "release" in on["push"]["branches"]
    assert "workflow_dispatch" in on

    assert doc["concurrency"]["cancel-in-progress"] is True
    assert doc["permissions"]["contents"] == "read"

    steps = doc["jobs"]["verify"]["steps"]
    assert any(
        str(s.get("uses", "")).startswith("actions/checkout")
        and s.get("with", {}).get("fetch-depth") == 0
        for s in steps
    )
    assert any(cmd in (s.get("run") or "") for s in steps)


# --- provider detection -----------------------------------------------------

def test_non_github_provider_reports_line_not_file(tmp_path):
    """A repo already carrying non-GitHub CI (GitLab here) is report-only: a
    named manual remedy line, apply_ok False, and NO workflow path to write."""
    _touch(tmp_path, ".gitlab-ci.yml", "stages: [test]\n")

    prov = sct.detect_provider(str(tmp_path))

    assert prov.kind == sct.NON_GITHUB_CI
    assert prov.apply_ok is False
    assert prov.workflow_path is None            # a line, not a file
    assert "GitLab" in prov.detail
    assert "manually" in prov.detail             # named manual remedy


def test_existing_ci_detected_and_reported_not_overwritten(tmp_path):
    """An existing .github/workflows/ci-verify.yml is reported present and is
    NEVER overwritten — detect_provider only classifies; the file is byte-for-
    byte untouched afterward."""
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    sentinel = "name: ci-verify\n# hand-authored — do not touch\n"
    (wf / "ci-verify.yml").write_text(sentinel, encoding="utf-8")

    prov = sct.detect_provider(str(tmp_path))

    assert prov.kind == sct.EXISTING_CI_VERIFY
    assert prov.apply_ok is False
    assert prov.workflow_path == str(wf / "ci-verify.yml")
    assert (wf / "ci-verify.yml").read_text(encoding="utf-8") == sentinel


def test_no_remote_ci_goes_report_only(tmp_path):
    """A real git repo with NO remote classifies as no-ci: report-only,
    apply_ok False, nothing to write."""
    _git(tmp_path, "init", "-q")

    prov = sct.detect_provider(str(tmp_path))

    assert prov.kind == sct.NO_CI
    assert prov.apply_ok is False
    assert prov.workflow_path is None


def test_github_remote_is_apply_ok(tmp_path):
    """A git repo whose remote URL matches github.com is the one apply_ok
    outcome, and it names the target workflow path."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/acme/widget.git")

    prov = sct.detect_provider(str(tmp_path))

    assert prov.kind == sct.GITHUB_REMOTE
    assert prov.apply_ok is True
    assert prov.workflow_path == os.path.join(str(tmp_path), sct.CI_VERIFY_WORKFLOW_REL)


def test_existing_ci_verify_wins_over_github_remote(tmp_path):
    """Precedence: an existing ci-verify.yml is reported (never overwrite) even
    when a github.com remote is also present — never-overwrite trumps apply."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/acme/widget.git")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci-verify.yml").write_text("name: ci-verify\n", encoding="utf-8")

    prov = sct.detect_provider(str(tmp_path))

    assert prov.kind == sct.EXISTING_CI_VERIFY
    assert prov.apply_ok is False
