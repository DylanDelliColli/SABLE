#!/usr/bin/env python3
"""sable_ci_template — render the portable ci-verify workflow and classify a
target repo's CI provider for /sable-onboarding (SABLE-gn7a.2). Consumed by the
onboarding scanner (SABLE-gn7a.3) and skill (SABLE-gn7a.4).

Two responsibilities:

  1. RENDER. Substitute the three placeholders in templates/ci-verify-project.yml
     — {{INTEGRATION_BRANCH}} (the S6-confirmed integration branch),
     {{RUNTIME_SETUP}} (toolchain setup keyed to the stack detected by
     sable_stack_detect.py), and {{TEST_COMMAND}} (the confirmed testCommand,
     run verbatim) — and return the workflow YAML. The template's load-bearing
     trigger (push to ci-verify/**, the SABLE-ad21 lesson) and its COMMENTED
     optional blocks (git identity / default-branch pin — the SABLE-59zu/r1zs
     lessons) carry through untouched. The renderer NEVER emits SABLE-specific
     suite steps (no shell-run-set, no `pytest bin/`, no fixture-tripwire) —
     those belong only to this repo's own .github/workflows/ci-verify.yml.

  2. PROVIDER DETECTION. Classify a repo into exactly one of four outcomes so the
     skill's gated-apply flow knows whether it may write the workflow:
       existing-ci-verify  .github/workflows/ci-verify.yml already present ->
                           report it, NEVER overwrite.
       non-github-ci       a .gitlab-ci.yml / .circleci/ / Jenkinsfile /
                           azure-pipelines.yml is present -> report-only with a
                           named manual remedy (a line, not a written file).
       github-remote       a git remote URL matches github.com -> apply_ok.
       no-ci               no GitHub remote at all -> report-only.
     Only github-remote sets apply_ok; every other outcome is report-only.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple, Optional

# The checked-in skeleton, resolved relative to this module (bin/ -> repo root).
_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "ci-verify-project.yml"

# Render placeholders — exact tokens, replaced by literal string substitution so
# GitHub Actions' own ${{ github.ref }} expression (which also contains `{{`) is
# never touched.
_PH_INTEGRATION_BRANCH = "{{INTEGRATION_BRANCH}}"
_PH_RUNTIME_SETUP = "{{RUNTIME_SETUP}}"
_PH_TEST_COMMAND = "{{TEST_COMMAND}}"

# Target path the rendered workflow is written to, relative to a repo root.
CI_VERIFY_WORKFLOW_REL = ".github/workflows/ci-verify.yml"

# Step-indent for the job's `steps:` items (items sit two levels under `jobs:`).
_STEP_INDENT = 6


# ---------------------------------------------------------------------------
# (1) Render — placeholder substitution over the checked-in skeleton.
# ---------------------------------------------------------------------------

# Runtime toolchain setup keyed on the coarse stack tag (Candidate.kind emitted
# by sable_stack_detect.py). Each value is a list of step lines at step level
# (no leading indent; render() indents the whole block). A kind with no entry
# (make / none / unknown) renders a commented "add your setup" placeholder — the
# skeleton stays valid and the human is told where to fill in.
_RUNTIME_SETUP = {
    "python": [
        "- name: Set up Python",
        "  uses: actions/setup-python@v5",
        "  with:",
        "    python-version: '3.x'",
        "- name: Install dependencies",
        "  run: |",
        "    python -m pip install --upgrade pip",
        "    pip install -e . || pip install pytest",
    ],
    "js/pnpm": [
        "- name: Set up Node",
        "  uses: actions/setup-node@v4",
        "  with:",
        "    node-version: '20'",
        "- name: Install dependencies",
        "  run: |",
        "    corepack enable",
        "    pnpm install --frozen-lockfile",
    ],
    "js/yarn": [
        "- name: Set up Node",
        "  uses: actions/setup-node@v4",
        "  with:",
        "    node-version: '20'",
        "- name: Install dependencies",
        "  run: |",
        "    corepack enable",
        "    yarn install --frozen-lockfile",
    ],
    "js/npm": [
        "- name: Set up Node",
        "  uses: actions/setup-node@v4",
        "  with:",
        "    node-version: '20'",
        "- name: Install dependencies",
        "  run: npm ci",
    ],
    "go": [
        "- name: Set up Go",
        "  uses: actions/setup-go@v5",
        "  with:",
        "    go-version: 'stable'",
    ],
    "rust": [
        "- name: Set up Rust",
        "  run: rustup toolchain install stable --profile minimal",
    ],
}

_NO_RUNTIME_COMMENT = "# No language runtime detected — add setup steps here if your tests need them."


def runtime_setup_for(kind: Optional[str]) -> list:
    """Return the step lines (step level, unindented) that set up the toolchain
    for `kind` (a Candidate.kind tag). Unknown / falsy / make kinds return an
    empty list — render() then emits a commented placeholder in its place."""
    if not kind:
        return []
    return list(_RUNTIME_SETUP.get(kind, []))


def kind_for_command(detection, command: str) -> Optional[str]:
    """Given a sable_stack_detect.Detection (or any object exposing a
    `candidates` iterable of Candidate) and the CONFIRMED test command, return
    the kind of the candidate whose command matches — so render() sets up the
    runtime for the stack the confirmed command actually belongs to. None when
    no candidate matches (the human confirmed a hand-typed command)."""
    for cand in getattr(detection, "candidates", ()) or ():
        if getattr(cand, "command", None) == command:
            return getattr(cand, "kind", None)
    return None


def _indent(lines, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join((pad + ln) if ln else ln for ln in lines)


def _read_template(template_path: Optional[str]) -> str:
    path = Path(template_path) if template_path else _TEMPLATE_PATH
    return path.read_text(encoding="utf-8")


def render_workflow(*, integration_branch: str, test_command: str,
                    kind: Optional[str] = None,
                    runtime_setup: Optional[list] = None,
                    template_path: Optional[str] = None) -> str:
    """Render the ci-verify workflow for a target repo.

    `integration_branch` and `test_command` are the S6/execute-once confirmed
    values; `kind` selects the runtime setup (or pass `runtime_setup` explicitly
    to override). Returns the workflow YAML with every render placeholder
    resolved. The test command is single-line by contract (the .sable grammar
    rejects multi-line values) so it drops straight into the `run: |` block.
    """
    text = _read_template(template_path)

    setup_lines = runtime_setup if runtime_setup is not None else runtime_setup_for(kind)
    if setup_lines:
        setup_block = _indent(setup_lines, _STEP_INDENT)
    else:
        setup_block = _indent([_NO_RUNTIME_COMMENT], _STEP_INDENT)

    text = text.replace(_PH_RUNTIME_SETUP, setup_block)
    text = text.replace(_PH_INTEGRATION_BRANCH, integration_branch)
    text = text.replace(_PH_TEST_COMMAND, test_command)
    return text


# ---------------------------------------------------------------------------
# (2) Provider detection — which of four outcomes governs gated apply.
# ---------------------------------------------------------------------------

# Non-GitHub CI signals -> (path relative to repo, human label). A hit means the
# repo already has a CI system we do not drive; we report-only with a remedy.
_NON_GITHUB_CI = (
    (".gitlab-ci.yml", "GitLab CI"),
    (".circleci", "CircleCI"),
    ("Jenkinsfile", "Jenkins"),
    ("azure-pipelines.yml", "Azure Pipelines"),
)

# The four provider outcomes.
GITHUB_REMOTE = "github-remote"
NON_GITHUB_CI = "non-github-ci"
EXISTING_CI_VERIFY = "existing-ci-verify"
NO_CI = "no-ci"


class Provider(NamedTuple):
    """The CI-provider classification of a target repo."""
    kind: str                     # one of the four outcome constants above
    detail: str                   # human-readable report / remedy line
    apply_ok: bool                # True only for github-remote
    workflow_path: Optional[str]  # target/existing ci-verify.yml path, else None


def github_remote_url(repo_path: str) -> Optional[str]:
    """Return the first configured remote URL containing `github.com`, or None
    (no such remote, not a git repo, or git unavailable). Matches on the URL so
    it is agnostic to the remote name (origin, upstream, …)."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_path, "remote", "-v"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and "github.com" in parts[1]:
            return parts[1]
    return None


def _non_github_ci(repo_path: str):
    for name, label in _NON_GITHUB_CI:
        if os.path.exists(os.path.join(repo_path, name)):
            return name, label
    return None


def detect_provider(repo_path: str) -> Provider:
    """Classify `repo_path` into exactly one Provider outcome.

    Precedence (first match wins): an existing ci-verify.yml is reported and
    NEVER overwritten; then a non-GitHub CI system is report-only with a named
    manual remedy; then a github.com remote is apply_ok; otherwise no GitHub
    remote means report-only.
    """
    workflow_path = os.path.join(repo_path, CI_VERIFY_WORKFLOW_REL)
    if os.path.isfile(workflow_path):
        return Provider(
            EXISTING_CI_VERIFY,
            "%s already exists — reporting present; will NOT overwrite it."
            % CI_VERIFY_WORKFLOW_REL,
            False,
            workflow_path,
        )

    hit = _non_github_ci(repo_path)
    if hit:
        name, label = hit
        return Provider(
            NON_GITHUB_CI,
            "%s detected (%s); this repo already runs a CI we do not manage. "
            "Add an equivalent ci-verify job manually — see "
            "templates/ci-verify-project.yml." % (label, name),
            False,
            None,
        )

    url = github_remote_url(repo_path)
    if url:
        return Provider(
            GITHUB_REMOTE,
            "GitHub remote detected (%s); ci-verify can be applied at %s."
            % (url, CI_VERIFY_WORKFLOW_REL),
            True,
            workflow_path,
        )

    return Provider(
        NO_CI,
        "No GitHub remote found — report-only; push this repo to a GitHub "
        "remote for Actions to run ci-verify.",
        False,
        None,
    )
