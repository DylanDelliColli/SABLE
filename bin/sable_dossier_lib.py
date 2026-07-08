#!/usr/bin/env python3
"""sable_dossier_lib — render the planning dossier (SABLE-lykc.1).

The /sable-plan Full-tier flow has five gated substages; each producer drops a
JSON deliverable into ``<repo>/.claude/sable/state/planning/<epic-id>/`` and the
gate renders ALL deliverables produced so far into one self-contained HTML page
(the "dossier") that Lincoln publishes via the Artifact tool before asking for
signoff. This module is the canonical schema contract — producers (Lincoln,
sherlock, gaudi, columbo, victor) write JSON matching the shapes below; the
renderer tolerates missing files, malformed JSON (per-section error box), and
unknown/absent keys, so partial or drifted data degrades instead of crashing.

Deliverable schemas (all keys optional unless the renderer can't say anything
without them; unknown extra keys are ignored):

framing.json        (Lincoln, FRAMING gate)
  { "stories": [{"id": "S1", "title": str, "acceptance": str?}],
    "non_goals": [str], "success_metric": str, "wedge": str }

research.json       (sherlock, RESEARCH gate)
  { "findings": [{"title": str, "kind": "prior_art"|"pitfall"|"unknown",
                  "summary": str, "sources": [str],
                  "derisk_status": "open"|"resolved"}],
    "recommendation": str }

architecture.json   (gaudi, ARCHITECTURE gate)
  { "decisions": [{"title": str, "contract": str, "rationale": str,
                   "alternatives_rejected": [str]}],
    "smell_risks": [str], "deferred": [{"finding": str, "why": str}],
    "status": "ready"|"needs-follow-up" }

test-strategy.json  (columbo, TEST-STRATEGY gate — the story-by-test matrix)
  { "epic": str, "sha": str, "stories_source": "framing"|"derived",
    "stories": [{"id": "S1", "title": str,
                 "impl_beads": [{"id": str, "title": str}],
                 "cases": [{"name": str, "layer": "UNIT"|"E2E"|"EVAL",
                            "status": "planned"|"gap", "bead": str?,
                            "category": int?}]}],
    "unmapped_beads": [{"id": str, "title": str}],
    "findings": {"resolved": [str], "deferred": [str]},
    "layer_mix": {"unit": int, "e2e": int, "eval": int},
    "coverage": {"covered": int, "total": int} }

decomposition.json  (Lincoln + victor, DECOMPOSITION gate)
  { "children": [{"id": str, "title": str, "type": str,
                  "deps": [str], "ready": bool}],
    "swarm_validate": {"ok": bool, "output": str},
    "victor_summary": str }

Path resolution mirrors sable-mode's resolve_state_path / sable_charter_lib's
charters_dir: ``SABLE_PLANNING_DIR`` override -> repo main-worktree state dir ->
HOME fallback.
"""
from __future__ import annotations

import html
import json
import os
import subprocess
from pathlib import Path

SUBSTAGES = ["framing", "research", "architecture", "test-strategy", "decomposition"]

_TITLES = {
    "framing": "Framing",
    "research": "Research",
    "architecture": "Architecture",
    "test-strategy": "Test strategy",
    "decomposition": "Decomposition",
}


class LoadError:
    """A substage file that exists but couldn't be parsed."""

    def __init__(self, message: str):
        self.message = message


def planning_dir(epic_id: str, base: str | None = None) -> Path:
    """Per-repo planning state dir for one epic. Mirrors sable-mode's
    resolve_state_path but lands on ``.../state/planning/<epic-id>``."""
    override = os.environ.get("SABLE_PLANNING_DIR")
    if override:
        return Path(override) / epic_id
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
            return root / ".claude" / "sable" / "state" / "planning" / epic_id
    except Exception:
        pass
    return (
        Path(os.environ.get("HOME", ""))
        / ".claude" / "sable" / "state" / "planning" / epic_id
    )


def load_state(state_dir: str | Path) -> dict:
    """Per substage: parsed dict, None (file absent), or LoadError."""
    state_dir = Path(state_dir)
    state: dict = {}
    for name in SUBSTAGES:
        path = state_dir / f"{name}.json"
        if not path.exists():
            state[name] = None
            continue
        try:
            state[name] = json.loads(path.read_text())
        except Exception as e:
            state[name] = LoadError(f"{name}.json: invalid JSON ({e})")
    return state


# --- rendering ----------------------------------------------------------------

def _e(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _items(data, key):
    """data.get(key) coerced to a list of dicts/strings; never throws."""
    if not isinstance(data, dict):
        return []
    v = data.get(key)
    return v if isinstance(v, list) else []


def _chip(text, cls="chip"):
    return f'<span class="{cls}">{_e(text)}</span>'


def _render_framing(d: dict) -> str:
    out = []
    stories = _items(d, "stories")
    if stories:
        rows = "".join(
            f'<li><b>{_e(s.get("id", "?"))}</b> — {_e(s.get("title", ""))}'
            + (f'<div class="sub">{_e(s["acceptance"])}</div>' if isinstance(s, dict) and s.get("acceptance") else "")
            + "</li>"
            for s in stories if isinstance(s, dict)
        )
        out.append(f"<h3>User stories</h3><ul>{rows}</ul>")
    if d.get("success_metric"):
        out.append(f'<p><b>Success metric:</b> {_e(d["success_metric"])}</p>')
    if d.get("wedge"):
        out.append(f'<p><b>Wedge:</b> {_e(d["wedge"])}</p>')
    ng = _items(d, "non_goals")
    if ng:
        out.append("<p><b>Non-goals:</b> " + ", ".join(_e(x) for x in ng) + "</p>")
    return "".join(out)


def _render_research(d: dict) -> str:
    out = []
    for f in _items(d, "findings"):
        if not isinstance(f, dict):
            continue
        status = f.get("derisk_status", "")
        badge = _chip(status, "chip ok" if status == "resolved" else "chip warn") if status else ""
        kind = _chip(f.get("kind", "")) if f.get("kind") else ""
        out.append(
            f'<div class="card">{kind} <b>{_e(f.get("title", ""))}</b> {badge}'
            f'<div class="sub">{_e(f.get("summary", ""))}</div></div>'
        )
    if d.get("recommendation"):
        out.append(f'<p><b>Recommendation:</b> {_e(d["recommendation"])}</p>')
    return "".join(out)


def _render_architecture(d: dict) -> str:
    out = []
    for dec in _items(d, "decisions"):
        if not isinstance(dec, dict):
            continue
        alts = ", ".join(_e(a) for a in dec.get("alternatives_rejected", []) if a) \
            if isinstance(dec.get("alternatives_rejected"), list) else ""
        out.append(
            f'<div class="card"><b>{_e(dec.get("title", ""))}</b>'
            f'<div class="sub"><code>{_e(dec.get("contract", ""))}</code></div>'
            f'<div class="sub">{_e(dec.get("rationale", ""))}</div>'
            + (f'<div class="sub">Rejected: {alts}</div>' if alts else "")
            + "</div>"
        )
    risks = _items(d, "smell_risks")
    if risks:
        out.append("<p><b>Smell risks:</b> " + ", ".join(_e(r) for r in risks) + "</p>")
    deferred = _items(d, "deferred")
    if deferred:
        rows = "".join(
            f'<li>{_e(x.get("finding", x) if isinstance(x, dict) else x)}'
            + (f' — {_e(x["why"])}' if isinstance(x, dict) and x.get("why") else "")
            + "</li>"
            for x in deferred
        )
        out.append(f"<h3>Deferred</h3><ul>{rows}</ul>")
    if d.get("status"):
        out.append(f'<p><b>Status:</b> {_e(d["status"])}</p>')
    return "".join(out)


def _render_case(c: dict) -> str:
    status = c.get("status", "planned")
    cls = "case gap" if status == "gap" else "case"
    layer = _chip(c.get("layer", "?"), "chip layer")
    bead = f' <span class="sub">{_e(c["bead"])}</span>' if c.get("bead") else ""
    gap = ' <span class="gapmark">GAP</span>' if status == "gap" else ""
    return f'<li class="{cls}">{layer} {_e(c.get("name", ""))}{bead}{gap}</li>'


def _render_test_strategy(d: dict) -> str:
    out = []
    cov = d.get("coverage") if isinstance(d.get("coverage"), dict) else {}
    mix = d.get("layer_mix") if isinstance(d.get("layer_mix"), dict) else {}
    header = []
    if cov:
        header.append(_chip(f'coverage {cov.get("covered", "?")}/{cov.get("total", "?")}', "chip stat"))
    if mix:
        header.append(_chip(f'UNIT {mix.get("unit", 0)}', "chip layer"))
        header.append(_chip(f'E2E {mix.get("e2e", 0)}', "chip layer"))
        header.append(_chip(f'EVAL {mix.get("eval", 0)}', "chip layer"))
    if d.get("stories_source") == "derived":
        header.append(_chip("stories derived, not from framing", "chip warn"))
    if header:
        out.append('<div class="statrow">' + " ".join(header) + "</div>")

    for s in _items(d, "stories"):
        if not isinstance(s, dict):
            continue
        cases = [c for c in s.get("cases", []) if isinstance(c, dict)] \
            if isinstance(s.get("cases"), list) else []
        gaps = sum(1 for c in cases if c.get("status") == "gap")
        beads = ", ".join(
            _e(b.get("id", "")) for b in s.get("impl_beads", []) if isinstance(b, dict)
        ) if isinstance(s.get("impl_beads"), list) else ""
        badge = f' <span class="gapmark">{gaps} GAP</span>' if gaps else ""
        case_rows = "".join(_render_case(c) for c in cases)
        out.append(
            f'<details open><summary><b>{_e(s.get("id", ""))}</b> '
            f'{_e(s.get("title", ""))}{badge}'
            + (f' <span class="sub">impl: {beads}</span>' if beads else "")
            + f"</summary><ul>{case_rows}</ul></details>"
        )

    unmapped = _items(d, "unmapped_beads")
    if unmapped:
        rows = "".join(
            f'<li>{_e(b.get("id", "") if isinstance(b, dict) else b)} — '
            f'{_e(b.get("title", "") if isinstance(b, dict) else "")}</li>'
            for b in unmapped
        )
        out.append(f"<h3>Beads not traceable to a story</h3><ul>{rows}</ul>")

    findings = d.get("findings") if isinstance(d.get("findings"), dict) else {}
    for label, key in (("Resolved findings", "resolved"), ("Deferred findings", "deferred")):
        vals = findings.get(key) if isinstance(findings.get(key), list) else []
        if vals:
            rows = "".join(f"<li>{_e(v)}</li>" for v in vals)
            out.append(f"<h3>{label}</h3><ul>{rows}</ul>")
    return "".join(out)


def _render_decomposition(d: dict) -> str:
    out = []
    children = _items(d, "children")
    if children:
        rows = "".join(
            "<tr><td>" + _e(c.get("id", "")) + "</td><td>" + _e(c.get("title", ""))
            + "</td><td>" + _e(c.get("type", "")) + "</td><td>"
            + ", ".join(_e(x) for x in (c.get("deps") or []))
            + "</td><td>" + ("ready" if c.get("ready") else "blocked") + "</td></tr>"
            for c in children if isinstance(c, dict)
        )
        out.append(
            '<div class="scroll"><table><tr><th>bead</th><th>title</th>'
            f"<th>type</th><th>needs</th><th>state</th></tr>{rows}</table></div>"
        )
    sv = d.get("swarm_validate") if isinstance(d.get("swarm_validate"), dict) else {}
    if sv:
        badge = _chip("PASS", "chip ok") if sv.get("ok") else _chip("FAIL", "chip bad")
        out.append(f'<p><b>swarm validate:</b> {badge} <code>{_e(sv.get("output", ""))}</code></p>')
    if d.get("victor_summary"):
        out.append(f'<p><b>Victor:</b> {_e(d["victor_summary"])}</p>')
    return "".join(out)


_SECTION_RENDERERS = {
    "framing": _render_framing,
    "research": _render_research,
    "architecture": _render_architecture,
    "test-strategy": _render_test_strategy,
    "decomposition": _render_decomposition,
}

_CSS = """
:root { --fg: #1c1c1c; --sub: #666; --bg: #fff; --card: #f6f6f4; --line: #ddd;
  --accent: #6a5acd; --ok: #1a7f37; --warn: #9a6700; --bad: #cf222e; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e6e6e6; --sub: #999; --bg: #191919; --card: #232323;
    --line: #3a3a3a; --accent: #9d8cff; --ok: #4ac26b; --warn: #d4a72c; --bad: #ff7b72; }
}
:root[data-theme="light"] { --fg: #1c1c1c; --sub: #666; --bg: #fff; --card: #f6f6f4;
  --line: #ddd; --accent: #6a5acd; --ok: #1a7f37; --warn: #9a6700; --bad: #cf222e; }
:root[data-theme="dark"] { --fg: #e6e6e6; --sub: #999; --bg: #191919; --card: #232323;
  --line: #3a3a3a; --accent: #9d8cff; --ok: #4ac26b; --warn: #d4a72c; --bad: #ff7b72; }
body { color: var(--fg); background: var(--bg);
  font: 15px/1.5 system-ui, sans-serif; max-width: 60rem; margin: 0 auto; padding: 1.5rem; }
section { border: 1px solid var(--line); border-radius: 8px; padding: 1rem 1.25rem; margin: 1rem 0; }
section.pending { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent); }
section.empty { color: var(--sub); border-style: dashed; }
h2 { margin: 0 0 .5rem; font-size: 1.1rem; }
h3 { font-size: .95rem; margin: 1rem 0 .25rem; }
.sub { color: var(--sub); font-size: .85rem; }
.card { background: var(--card); border-radius: 6px; padding: .5rem .75rem; margin: .5rem 0; }
.chip { display: inline-block; border: 1px solid var(--line); border-radius: 999px;
  padding: 0 .5em; font-size: .75rem; }
.chip.layer { border-color: var(--accent); color: var(--accent); }
.chip.ok { border-color: var(--ok); color: var(--ok); }
.chip.warn { border-color: var(--warn); color: var(--warn); }
.chip.bad { border-color: var(--bad); color: var(--bad); }
.chip.stat { font-weight: 600; }
.statrow { margin-bottom: .75rem; }
.badge { color: var(--accent); font-size: .8rem; font-weight: 600; margin-left: .5rem; }
ul { margin: .25rem 0; padding-left: 1.25rem; }
li.case { list-style: none; margin: .15rem 0; }
li.case.gap { color: var(--bad); }
.gapmark { color: var(--bad); font-weight: 700; font-size: .75rem; }
.load-error { color: var(--bad); background: var(--card); border-radius: 6px; padding: .5rem .75rem; }
details { margin: .5rem 0; }
summary { cursor: pointer; }
table { border-collapse: collapse; font-size: .85rem; }
td, th { border: 1px solid var(--line); padding: .25rem .5rem; text-align: left; }
.scroll { overflow-x: auto; }
code { background: var(--card); padding: 0 .25em; border-radius: 4px; }
"""


def render(epic_id: str, state: dict, highlight: str | None = None) -> str:
    """One self-contained HTML page (fragment per Artifact rules: <title> +
    inline <style> + content, no doctype/html/head/body wrappers)."""
    parts = [
        f"<title>Planning dossier — {_e(epic_id)}</title>",
        f"<style>{_CSS}</style>",
        f"<h1>Planning dossier — {_e(epic_id)}</h1>",
    ]
    for name in SUBSTAGES:
        data = state.get(name)
        title = _TITLES[name]
        badge = ' <span class="badge">◀ awaiting signoff</span>' if name == highlight else ""
        cls = "pending" if name == highlight else ""
        if data is None:
            parts.append(
                f'<section id="{name}" class="empty {cls}"><h2>{title}{badge}</h2>'
                "<p>not yet produced</p></section>"
            )
        elif isinstance(data, LoadError):
            parts.append(
                f'<section id="{name}" class="{cls}"><h2>{title}{badge}</h2>'
                f'<p class="load-error">{_e(data.message)}</p></section>'
            )
        else:
            body = _SECTION_RENDERERS[name](data) or '<p class="sub">(empty deliverable)</p>'
            parts.append(f'<section id="{name}" class="{cls}"><h2>{title}{badge}</h2>{body}</section>')
    return "\n".join(parts)


def write_dossier(
    epic_id: str,
    base: str | None = None,
    state_dir: str | Path | None = None,
    out: str | Path | None = None,
    highlight: str | None = None,
) -> Path:
    state_dir = Path(state_dir) if state_dir else planning_dir(epic_id, base)
    state = load_state(state_dir)
    out_path = Path(out) if out else state_dir / "dossier.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(epic_id, state, highlight=highlight))
    return out_path
