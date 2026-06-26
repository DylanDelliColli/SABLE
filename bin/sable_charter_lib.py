"""sable_charter_lib — Discovery artifact layer (SABLE-7v1r.1).

Schemas + read/write for the two durable Discovery artifacts (see
PLANNING-MODES-DESIGN.md):

  * the per-survivor **charter** — Full's FRAMING input, office-hours' design-doc
    sections minus engineering;
  * the session **decision record** — candidate verdicts (go / no-go / reshape)
    with the no-go rationales kept verbatim, the relitigation-killer.

Both are COMMITTED markdown under ``<repo>/.claude/sable/charters/`` — the
come-back-to record, NOT ephemeral like ``.claude/sable/state/`` (so this lib has
no gitignore step). Path resolution mirrors bin/sable-mode's resolve_state_path:
``SABLE_CHARTERS_DIR`` override -> repo main-worktree charters dir -> HOME
fallback. Logic lives here (importable) so the thin ``sable-charter`` bin and
pytest share one implementation.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# --- naming + path resolution ----------------------------------------------

def slugify(title: str) -> str:
    """Filesystem-safe slug from a candidate title."""
    s = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return s or "untitled"


def charters_dir(base: str | None = None) -> Path:
    """Where Discovery artifacts live, resolved per-repo. Mirrors sable-mode's
    resolve_state_path but lands on ``.../charters`` (committed, not ignored)."""
    override = os.environ.get("SABLE_CHARTERS_DIR")
    if override:
        return Path(override)
    base = base or os.getcwd()
    try:
        r = subprocess.run(
            ["git", "-C", base, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True,
        )
        common = r.stdout.strip()
        if r.returncode == 0 and common:
            cpath = Path(common)
            if not cpath.is_absolute():
                cpath = Path(base) / common
            root = cpath.parent.resolve()
            return root / ".claude" / "sable" / "charters"
    except Exception:
        pass
    return Path(os.environ.get("HOME", "")) / ".claude" / "sable" / "charters"


# --- markdown frontmatter helpers ------------------------------------------

def _render_frontmatter(pairs) -> str:
    lines = ["---"]
    for k, v in pairs:
        lines.append(f"{k}: {'' if v is None else v}")
    lines.append("---")
    return "\n".join(lines)


def _split_frontmatter(text: str):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        if ":" in lines[i]:
            k, _, v = lines[i].partition(":")
            meta[k.strip()] = v.strip()
        i += 1
    body = "\n".join(lines[i + 1:]) if i < len(lines) else ""
    return meta, body


def _none_if_empty(s: str):
    return s if s else None


def _parse_sections(body: str):
    """Map ``## Heading`` -> stripped body. ``### `` (candidate subsections) are
    not matched, so they stay inside their parent section."""
    sections = {}
    cur = None
    buf: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if cur is not None:
                sections[cur] = "\n".join(buf).strip()
            cur = m.group(1).strip()
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        sections[cur] = "\n".join(buf).strip()
    return sections


# --- charter schema --------------------------------------------------------

_CHARTER_SECTIONS = [
    ("Problem Statement", "problem_statement"),
    ("Demand Evidence", "demand_evidence"),
    ("Status Quo", "status_quo"),
    ("Target User and Narrowest Wedge", "target_user_and_wedge"),
    ("Why Now", "why_now"),
    ("Product Approaches", "product_approaches"),
    ("Recommended Product Shape", "recommended_shape"),
    ("Success Metric", "success_metric"),
    ("Non-Goals", "non_goals"),
    ("Open Questions", "open_questions"),
]


@dataclass
class Charter:
    slug: str
    title: str
    decision_record: str | None = None
    epic_intention: str | None = None
    created: str | None = None
    problem_statement: str = ""
    demand_evidence: str = ""
    status_quo: str = ""
    target_user_and_wedge: str = ""
    why_now: str = ""
    product_approaches: str = ""
    recommended_shape: str = ""
    success_metric: str = ""
    non_goals: str = ""
    open_questions: str = ""

    def to_markdown(self) -> str:
        fm = _render_frontmatter([
            ("kind", "charter"),
            ("slug", self.slug),
            ("title", self.title),
            ("decision_record", self.decision_record),
            ("epic_intention", self.epic_intention),
            ("created", self.created),
        ])
        parts = [fm, ""]
        for heading, attr in _CHARTER_SECTIONS:
            parts += [f"## {heading}", "", getattr(self, attr).strip(), ""]
        return "\n".join(parts).rstrip() + "\n"

    @classmethod
    def from_markdown(cls, text: str) -> "Charter":
        meta, body = _split_frontmatter(text)
        secs = _parse_sections(body)
        kwargs = {
            "slug": meta.get("slug", ""),
            "title": meta.get("title", ""),
            "decision_record": _none_if_empty(meta.get("decision_record", "")),
            "epic_intention": _none_if_empty(meta.get("epic_intention", "")),
            "created": _none_if_empty(meta.get("created", "")),
        }
        for heading, attr in _CHARTER_SECTIONS:
            kwargs[attr] = secs.get(heading, "").strip()
        return cls(**kwargs)


# --- decision record schema ------------------------------------------------

@dataclass
class Candidate:
    title: str
    verdict: str           # go | no-go | reshape
    rationale: str = ""
    charter: str | None = None


@dataclass
class DecisionRecord:
    session: str
    title: str | None = None
    created: str | None = None
    candidates: list = field(default_factory=list)

    def to_markdown(self) -> str:
        fm = _render_frontmatter([
            ("kind", "decision"),
            ("session", self.session),
            ("title", self.title),
            ("created", self.created),
        ])
        parts = [fm, "", "## Candidates", ""]
        for c in self.candidates:
            parts += [
                f"### {c.title}",
                "",
                f"- verdict: {c.verdict}",
                f"- charter: {c.charter if c.charter else '(none)'}",
                "",
                c.rationale.strip(),
                "",
            ]
        return "\n".join(parts).rstrip() + "\n"

    @classmethod
    def from_markdown(cls, text: str) -> "DecisionRecord":
        meta, body = _split_frontmatter(text)
        candidates = []
        for block in re.split(r"(?m)^###\s+", body)[1:]:
            lines = block.splitlines()
            title = lines[0].strip()
            verdict = ""
            charter = None
            rationale: list[str] = []
            for ln in lines[1:]:
                vm = re.match(r"^-\s*verdict:\s*(.+?)\s*$", ln)
                cm = re.match(r"^-\s*charter:\s*(.+?)\s*$", ln)
                if vm:
                    verdict = vm.group(1).strip()
                elif cm:
                    val = cm.group(1).strip()
                    charter = None if val == "(none)" else val
                else:
                    rationale.append(ln)
            candidates.append(Candidate(
                title=title, verdict=verdict,
                rationale="\n".join(rationale).strip(), charter=charter,
            ))
        return cls(
            session=meta.get("session", ""),
            title=_none_if_empty(meta.get("title", "")),
            created=_none_if_empty(meta.get("created", "")),
            candidates=candidates,
        )


# --- write / locate --------------------------------------------------------

def write_charter(charter: Charter, base: str | None = None) -> Path:
    d = charters_dir(base)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{charter.slug}.md"
    p.write_text(charter.to_markdown())
    return p


def write_decision_record(record: DecisionRecord, base: str | None = None) -> Path:
    d = charters_dir(base)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{slugify(record.session)}-decisions.md"
    p.write_text(record.to_markdown())
    return p


def locate(slug: str, base: str | None = None) -> Path | None:
    p = charters_dir(base) / f"{slug}.md"
    return p if p.exists() else None


def list_charters(base: str | None = None) -> list:
    d = charters_dir(base)
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.md") if not p.name.endswith("-decisions.md"))
