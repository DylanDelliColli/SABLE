#!/usr/bin/env python3
"""Tests for the /sable-onboarding trio: the stack + .sable library
(bin/sable_stack_detect.py, SABLE-gn7a.1), the ci-verify render + provider
detection (bin/sable_ci_template.py, SABLE-gn7a.2), and the read-only SCANNER
(bin/sable-onboard, SABLE-gn7a.3). Columbo S4 detection matrix, the grammar
CRITICALs, and the scanner's S1/S2/S6 matrix (registry consistency, per-prereq
report, git-state + default-branch detection, zero-writes).

Integration character (per CLAUDE.md unit+integration requirement): detection
runs against a REAL temp filesystem; execute_once runs REAL `sh -c` subprocesses;
test_scan_contract_matches_lib_identity_resolvers sources and calls the REAL
bash resolvers in hooks/multi-manager/lib-identity.sh; and the scanner's
git-state / default-branch / ci-provider tests run against REAL temp git repos
(real `git` subprocesses), so accept/reject and detection are proven against the
actual tools rather than copies of them.
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent
_REPO_ROOT = _BIN.parent
sys.path.insert(0, str(_BIN))

import sable_stack_detect as ssd  # noqa: E402
import sable_ci_template as sct  # noqa: E402

# bin/sable-onboard has a hyphen and no .py extension — load it by path.
_ONB_LOADER = SourceFileLoader("sable_onboard", str(_BIN / "sable-onboard"))
_ONB_SPEC = importlib.util.spec_from_loader("sable_onboard", _ONB_LOADER)
onb = importlib.util.module_from_spec(_ONB_SPEC)
_ONB_LOADER.exec_module(onb)


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


# ===========================================================================
# sable-onboard — the read-only scanner (SABLE-gn7a.3)
#
# Columbo S1 (zero-writes) / S2 (registry + report/json/exit unit matrix) /
# S6 (git-state + default-branch detection). Fixtures build REAL temp git repos
# so git-state, default-branch, and ci-provider detection run against the real
# `git` binary; the binary-presence and install-scope probes are exercised
# through the injected Env so they stay hermetic (no dependency on what happens
# to be on PATH, nor on a real ~/.claude install).
# ===========================================================================

def _commit(repo, *, email="t@t.com", name="t", msg="init"):
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=" + email, "-c", "user.name=" + name,
         "commit", "-q", "-m", msg)


def make_onboard_repo(tmp_path, *, name="proj", branch="work"):
    """A fully-onboarded fixture repo — every filesystem-observable prerequisite
    satisfied (CLAUDE.md prime block, .beads workspace, valid .sable, portable
    committed settings wiring, an existing ci-verify.yml). A REAL git repo so
    git-state and ci-provider probes run against real git. Mirrors the shape of
    bin/test_sable_doctor.py's make_repo. Binary presence and install-scope are
    satisfied via the injected Env (see _green_env), not this tree."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)

    (repo / "CLAUDE.md").write_text(
        "# Project\n\n## Prime Directive\nAll work flows through beads.\n",
        encoding="utf-8",
    )

    beads = repo / ".beads"
    beads.mkdir()
    (beads / "config.yaml").write_text("db: proj.db\n", encoding="utf-8")
    (beads / "metadata.json").write_text("{}\n", encoding="utf-8")

    (repo / ".sable").write_text(
        "testCommand=pytest -q\nintegrationBranch=%s\n" % branch, encoding="utf-8")

    claude = repo / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [
                    {"type": "command",
                     "command": "bash ${CLAUDE_PROJECT_DIR}/.claude/hooks/multi-manager/mode-interlock.sh"},
                ]},
            ],
        },
    }), encoding="utf-8")

    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci-verify.yml").write_text("name: ci-verify\n", encoding="utf-8")

    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _commit(repo)
    return repo


def _green_env():
    """An Env where every binary resolves and sable-doctor reports clean — so a
    make_onboard_repo scan is all-green without touching the host PATH or a real
    install."""
    return onb.Env(
        which=lambda name: "/usr/bin/" + name,
        run_doctor=lambda repo: (True, "sable-doctor --project: install matches repo HEAD."),
    )


# --- D2: the single CHECKS registry drives all four outputs -----------------

def test_checks_registry_every_id_in_all_four_outputs(tmp_path):
    """Shotgun-Surgery guard: every id in the ONE registry must surface in the
    human report, the --json payload, the exit-code derivation list, AND be
    addressable via --check — no second enumeration drops or renames one."""
    repo = make_onboard_repo(tmp_path)
    env = _green_env()

    results = onb.run_checks(str(repo), env=env)
    report = onb.render_text(str(repo), results, onb.git_state(str(repo)))
    payload = onb.build_payload(str(repo), results, onb.git_state(str(repo)))
    json_ids = {c["id"] for c in payload["checks"]}
    # exit-derivation consumes the SAME result list every id is in
    exit_ids = {r.id for r in results}

    for check in onb.CHECKS:
        assert check.id in report, "%s missing from text report" % check.id
        assert check.id in json_ids, "%s missing from json payload" % check.id
        assert check.id in exit_ids, "%s missing from exit-derivation list" % check.id
        scoped = onb.run_checks(str(repo), only=check.id, env=env)
        assert [r.id for r in scoped] == [check.id], "%s not --check-addressable" % check.id

    # the four outputs enumerate EXACTLY the registry — no extras, none dropped
    assert exit_ids == {c.id for c in onb.CHECKS}
    assert json_ids == {c.id for c in onb.CHECKS}


def test_report_names_present_missing_remedy_per_prereq(tmp_path):
    """For each prereq the report names present-vs-missing and, when missing,
    prints the registry remedy verbatim."""
    repo = make_onboard_repo(tmp_path)
    (repo / ".sable").unlink()  # break exactly one prereq -> a named gap
    env = _green_env()

    results = onb.run_checks(str(repo), env=env)
    report = onb.render_text(str(repo), results, onb.git_state(str(repo)))

    sable = next(r for r in results if r.id == "sable-contract")
    assert sable.status == onb.STATUS_GAP
    assert "sable-contract" in report
    assert sable.remedy in report            # remedy shown for the missing prereq
    assert "GAP" in report                   # missing is labelled

    # a satisfied prereq is named present, and its remedy is NOT dangled
    beads = next(r for r in results if r.id == "beads-workspace")
    assert beads.status == onb.STATUS_OK
    assert "OK" in report
    assert beads.remedy not in report


def test_json_exit0_all_green_exit1_any_gap(tmp_path):
    """exit 0 / ok:true when all-green; a single required gap flips exit to 1
    and ok:false — both derived from the same result list."""
    repo = make_onboard_repo(tmp_path)
    env = _green_env()

    green = onb.run_checks(str(repo), env=env)
    assert onb.exit_code_for(green) == 0
    assert onb.build_payload(str(repo), green, None)["ok"] is True
    assert all(r.status != onb.STATUS_GAP for r in green)

    (repo / "CLAUDE.md").write_text("# no marker here\n", encoding="utf-8")
    gapped = onb.run_checks(str(repo), env=env)
    assert onb.exit_code_for(gapped) == 1
    payload = onb.build_payload(str(repo), gapped, None)
    assert payload["ok"] is False
    assert payload["exit_code"] == 1


def test_already_onboarded_repo_reports_all_green_proposes_nothing(tmp_path):
    """A repo that is already fully set up scans all-green and proposes no
    remedy — every check OK, zero gaps, and the report says so."""
    repo = make_onboard_repo(tmp_path)
    env = _green_env()

    results = onb.run_checks(str(repo), env=env)
    assert all(r.status == onb.STATUS_OK for r in results)
    assert onb.exit_code_for(results) == 0

    report = onb.render_text(str(repo), results, onb.git_state(str(repo)))
    assert "all green" in report
    assert "GAP" not in report               # nothing proposed


# --- .sable line-shape (shape, not presence) --------------------------------

@pytest.mark.parametrize("line,accepts", [
    ("testCommand=pytest -q", True),
    ("integrationBranch=main", True),
    (" testCommand=pytest", False),          # leading space
    ("testCommand = pytest", False),         # space around '='
    ("# testCommand=pytest", False),         # comment
    ("TestCommand=pytest", False),           # wrong case
])
def test_sable_line_shape_accept_reject_matrix(tmp_path, line, accepts):
    """The sable-contract check validates line SHAPE via sable_stack_detect:
    a grammar-valid line is OK, a malformed one is a GAP even though .sable
    exists."""
    repo = make_onboard_repo(tmp_path)
    (repo / ".sable").write_text(line + "\n", encoding="utf-8")
    env = _green_env()

    result = onb.run_checks(str(repo), only="sable-contract", env=env)[0]
    if accepts:
        assert result.status == onb.STATUS_OK
    else:
        assert result.status == onb.STATUS_GAP
        assert "malformed" in result.detail


def test_prime_block_and_settings_wiring_checked_independently_of_doctor(tmp_path):
    """The two doctor-blind checks: even with sable-doctor reporting CLEAN, an
    absent prime block and non-portable settings wiring each still report a GAP
    — they are not masked by a green install-scope."""
    repo = make_onboard_repo(tmp_path)
    # doctor stays clean...
    env = _green_env()
    # ...but the prime block is gone and the hook wiring leaks an absolute path
    (repo / "CLAUDE.md").write_text("# just a heading, no directive\n", encoding="utf-8")
    (repo / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command",
             "command": "bash /home/someone/.claude/hooks/multi-manager/mode-interlock.sh"},
        ]}]},
    }), encoding="utf-8")

    results = {r.id: r for r in onb.run_checks(str(repo), env=env)}

    assert results["install-scope"].status == onb.STATUS_OK          # doctor clean
    assert results["claude-md-prime-block"].status == onb.STATUS_GAP  # still caught
    assert results["settings-wiring"].status == onb.STATUS_GAP        # still caught
    assert "CLAUDE_PROJECT_DIR" in results["settings-wiring"].detail


# --- --check scoping --------------------------------------------------------

def test_check_flag_scopes_to_single_check_id(tmp_path):
    """--check runs (and exit-derives from) exactly one check id."""
    repo = make_onboard_repo(tmp_path)
    env = _green_env()

    scoped = onb.run_checks(str(repo), only="bin:bd", env=env)
    assert len(scoped) == 1
    assert scoped[0].id == "bin:bd"

    rc = onb.main(["--repo", str(repo), "--check", "beads-workspace"])
    assert rc == 0  # beads workspace present in the fixture


def test_check_unknown_id_errors_nonzero_no_crash(tmp_path):
    """An unknown --check id errors non-zero cleanly (no traceback); the
    library raises KeyError for a caller to handle."""
    repo = make_onboard_repo(tmp_path)

    rc = onb.main(["--repo", str(repo), "--check", "no-such-check"])
    assert rc != 0
    assert rc == 2

    with pytest.raises(KeyError):
        onb.run_checks(str(repo), only="no-such-check", env=_green_env())


# --- D7: default-branch resolution ------------------------------------------

def test_default_branch_from_origin_head_ref(tmp_path):
    """origin/HEAD's symbolic-ref is authoritative: it names the default branch
    directly."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("x\n", encoding="utf-8")
    _commit(repo)
    _git(repo, "remote", "add", "origin", "https://github.com/acme/w.git")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    assert onb.default_branch(str(repo)) == "main"


def test_default_branch_origin_head_unset_falls_back_to_membership(tmp_path):
    """With origin present but its HEAD unset, resolution falls back to the
    conservative {main, master} membership signal."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    (repo / "f").write_text("x\n", encoding="utf-8")
    _commit(repo)
    _git(repo, "remote", "add", "origin", "https://github.com/acme/w.git")
    # origin/HEAD deliberately never set

    assert onb.default_branch(str(repo)) == "master"


def test_default_branch_never_consults_init_defaultbranch(tmp_path):
    """init.defaultBranch is a decoy — it describes what a FRESH git init would
    name a branch, never what THIS repo's default is (SABLE-r1zs). It must be
    ignored entirely."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "init.defaultBranch", "decoy-not-used")
    (repo / "f").write_text("x\n", encoding="utf-8")
    _commit(repo)
    # no remote -> membership; the decoy branch does not exist

    resolved = onb.default_branch(str(repo))
    assert resolved == "main"
    assert resolved != "decoy-not-used"


# --- D7: HEAD-state detection -----------------------------------------------

def test_detached_head_named_remedy_not_head_string(tmp_path):
    """A detached HEAD yields a named remedy and NEVER reports the literal
    string 'HEAD' as the current branch."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("x\n", encoding="utf-8")
    _commit(repo)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE, text=True, check=True).stdout.strip()
    _git(repo, "checkout", "-q", sha)  # detach

    state = onb.git_state(str(repo))
    assert state.head_state == onb.HEAD_DETACHED
    assert state.current_branch is None
    assert state.current_branch != "HEAD"
    assert state.branch_remedy and state.branch_remedy.strip()


def test_unborn_branch_named_remedy(tmp_path):
    """An unborn branch (git init, no commit yet) is detected with a named
    remedy — rev-parse HEAD fails though symbolic-ref succeeds."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")

    state = onb.git_state(str(repo))
    assert state.head_state == onb.HEAD_UNBORN
    assert state.current_branch == "main"
    assert state.branch_remedy and state.branch_remedy.strip()


def test_no_remote_confirms_local_ci_report_only(tmp_path):
    """Detection output side: a real git repo with NO remote and no ci-verify.yml
    makes the ci-verify check report-only (not a gap) — nothing to apply
    locally."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")

    result = onb.run_checks(str(repo), only="ci-verify", env=_green_env())[0]
    assert result.status == onb.STATUS_REPORT_ONLY
    assert onb.exit_code_for([result]) == 0    # report-only never flips exit


def test_multiple_remotes_asks(tmp_path):
    """Multiple remotes raise the 'ask' signal — the scanner detects, the skill
    asks which remote is canonical."""
    repo = make_onboard_repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://github.com/acme/w.git")
    _git(repo, "remote", "add", "upstream", "https://github.com/other/w.git")

    state = onb.git_state(str(repo))
    assert state.remote_state == onb.MULTIPLE_REMOTES
    assert state.asks is True


# --- S1: zero-writes ---------------------------------------------------------

def _tree_snapshot(root: Path):
    """sha256 of every file under root except .git internals — the scanner must
    not create, modify, or delete any working-tree file (S1 zero-writes)."""
    snap = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.relative_to(root).parts:
            snap[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def test_scan_performs_zero_writes(tmp_path):
    """A full scan (checks + git-state + both renderers) mutates nothing in the
    target tree — the read-only S1 contract, asserted by a before/after
    snapshot."""
    repo = make_onboard_repo(tmp_path)
    env = _green_env()

    before = _tree_snapshot(repo)
    results = onb.run_checks(str(repo), env=env)
    state = onb.git_state(str(repo))
    onb.render_text(str(repo), results, state)
    onb.build_payload(str(repo), results, state)
    after = _tree_snapshot(repo)

    assert before == after
