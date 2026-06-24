"""Structure tests for the stubbed sable-planning formula + office-hours agent
(SABLE-vj4x.5).

Phase 1 stub: sable-planning extends planning-base and overrides ONLY the
`requirements` step to route framing to sable.office-hours, preserving the
required requirements.v1 artifact gate. `plan` and `plan-review` stay inherited
(gc.design-author / gc.review-synthesizer). The real multi-substage planning is
the Phase 2 swap (SABLE-vj4x.7).

Runnable with `python3 -m pytest` or `python3 -m unittest`.
"""
from __future__ import annotations

import os
import pathlib
import tomllib
import unittest

PACK_ROOT = pathlib.Path(__file__).resolve().parents[1]
FORMULA = PACK_ROOT / "formulas" / "sable-planning.formula.toml"
AGENT_DIR = PACK_ROOT / "agents" / "office-hours"
PACK_FRAGMENT = PACK_ROOT / "template-fragments" / "gc-role-worker.template.md"
CLAIM_INCLUDE = '{{ template "gc-role-worker" . }}'
GUARD = "Do not invoke provider-native subagents"

GASCITY_PACKS_ROOT = pathlib.Path(
    os.environ.get(
        "GASCITY_PACKS_ROOT",
        str(pathlib.Path.home() / "dev-environment" / "gascity-packs"),
    )
)
BASE_FRAGMENT = (
    GASCITY_PACKS_ROOT / "gascity" / "roles" / "prompts" / "shared" / "gc-role-worker.md.tmpl"
)


def _formula() -> dict:
    return tomllib.loads(FORMULA.read_text(encoding="utf-8"))


def _requirements_step() -> dict:
    steps = _formula()["steps"]
    return next(s for s in steps if s["id"] == "requirements")


class SablePlanningStubTests(unittest.TestCase):
    def test_formula_extends_planning_base(self) -> None:
        data = _formula()
        self.assertEqual(data["formula"], "sable-planning")
        self.assertEqual(data["extends"], ["planning-base"])
        self.assertTrue(data["internal"])
        self.assertFalse(data["target_required"])

    def test_requirements_routes_to_office_hours(self) -> None:
        step = _requirements_step()
        self.assertEqual(step["metadata"]["gc.run_target"], "sable.office-hours")

    def test_requirements_keeps_artifact_gate(self) -> None:
        step = _requirements_step()
        self.assertEqual(
            step["metadata"]["gc.build.artifact_schema"], "gc.build.requirements.v1"
        )
        self.assertEqual(
            step["check"]["check"]["path"], ".gc/scripts/checks/build-artifact-valid.sh"
        )

    def test_office_hours_agent_is_providerless(self) -> None:
        data = tomllib.loads((AGENT_DIR / "agent.toml").read_text(encoding="utf-8"))
        self.assertNotIn("provider", data)
        self.assertEqual(data["scope"], "rig")
        self.assertTrue(data["fallback"])

    def test_office_hours_prompt_embeds_claim_protocol_and_guard(self) -> None:
        text = (AGENT_DIR / "prompt.template.md").read_text(encoding="utf-8")
        self.assertEqual(text.count(CLAIM_INCLUDE), 1)
        self.assertIn(GUARD, text)

    def test_office_hours_local_fragment_matches_pack_and_base(self) -> None:
        local = AGENT_DIR / "template-fragments" / "gc-role-worker.template.md"
        self.assertEqual(
            local.read_text(encoding="utf-8"), PACK_FRAGMENT.read_text(encoding="utf-8")
        )
        if BASE_FRAGMENT.is_file():
            self.assertEqual(
                local.read_text(encoding="utf-8"),
                BASE_FRAGMENT.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
