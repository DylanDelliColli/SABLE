"""Structure tests for sable-build + sable-review (SABLE-vj4x.4).

Phase 1: sable-build extends build-base, reuses gc.* base roles and base
methodology formulas, and adds SABLE's opinion in exactly two places:
  - framing: the `requirements` step routes to sable.office-hours
  - gates: two dedicated steps (sable-test-evidence, sable-scope-check) wired
    between `review` and `finalize`, each running an already-tested SABLE
    check-script.
sable-review is the pack-local code-review formula (thin extension of
code-review-base) that code_review_formula selects.

Runnable with `python3 -m pytest` or `python3 -m unittest`.
"""
from __future__ import annotations

import pathlib
import tomllib
import unittest

PACK_ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = PACK_ROOT / "formulas" / "sable-build.formula.toml"
REVIEW = PACK_ROOT / "formulas" / "sable-review.formula.toml"

VOCAB = {
    "allowed_drain_policies": {"separate", "same-session"},
    "implementation_strategy": {"drain", "convoy-step"},
    "interaction_modes": {"interactive", "autonomous", "headless"},
    "review_modes": {"report", "agent", "interactive"},
}


def _build() -> dict:
    return tomllib.loads(BUILD.read_text(encoding="utf-8"))


def _steps() -> dict:
    return {s["id"]: s for s in _build()["steps"]}


class SableBuildTests(unittest.TestCase):
    def test_extends_build_base(self) -> None:
        data = _build()
        self.assertEqual(data["formula"], "sable-build")
        self.assertEqual(data["extends"], ["build-base"])
        self.assertTrue(data["target_required"])
        self.assertIn("catalog", data)

    def test_methodology_metadata_uses_allowed_vocabulary(self) -> None:
        meth = _build()["metadata"]["gc"]["methodology"]
        self.assertEqual(set(meth), set(VOCAB))
        self.assertIn(meth["implementation_strategy"], VOCAB["implementation_strategy"])
        self.assertLessEqual(set(meth["allowed_drain_policies"]), VOCAB["allowed_drain_policies"])
        self.assertLessEqual(set(meth["interaction_modes"]), VOCAB["interaction_modes"])
        self.assertLessEqual(set(meth["review_modes"]), VOCAB["review_modes"])

    def test_selectors_pin_sable_planning_and_review(self) -> None:
        v = _build()["vars"]
        self.assertEqual(v["planning_formula"]["default"], "sable-planning")
        self.assertEqual(v["code_review_formula"]["default"], "sable-review")

    def test_requirements_routes_to_office_hours_with_gate(self) -> None:
        step = _steps()["requirements"]
        self.assertEqual(step["metadata"]["gc.run_target"], "sable.office-hours")
        self.assertEqual(step["metadata"]["gc.build.artifact_schema"], "gc.build.requirements.v1")
        self.assertEqual(step["check"]["check"]["path"], ".gc/scripts/checks/build-artifact-valid.sh")

    def test_test_evidence_gate_step_wired_after_review(self) -> None:
        step = _steps()["sable-test-evidence"]
        self.assertEqual(step["needs"], ["review"])
        self.assertEqual(step["check"]["check"]["path"], ".gc/scripts/checks/test-evidence.sh")

    def test_scope_gate_step_wired_after_test_evidence(self) -> None:
        step = _steps()["sable-scope-check"]
        self.assertEqual(step["needs"], ["sable-test-evidence"])
        self.assertEqual(step["check"]["check"]["path"], ".gc/scripts/checks/scope-creep-diff.sh")

    def test_finalize_rewired_behind_the_gates(self) -> None:
        step = _steps()["finalize"]
        self.assertEqual(step["needs"], ["sable-scope-check"])
        self.assertEqual(step["metadata"]["gc.build.artifact_schema"], "gc.build.final-report.v1")

    def test_all_route_targets_are_gc_or_sable(self) -> None:
        for step in _build()["steps"]:
            target = step.get("metadata", {}).get("gc.run_target", "")
            if not target:
                continue
            with self.subTest(step=step["id"], target=target):
                self.assertTrue(
                    target.startswith("gc.") or target.startswith("sable.") or target.startswith("{{"),
                    f"{target} must be gc.*, sable.*, or a templated var",
                )

    def test_sable_review_extends_code_review_base(self) -> None:
        data = tomllib.loads(REVIEW.read_text(encoding="utf-8"))
        self.assertEqual(data["formula"], "sable-review")
        self.assertEqual(data["extends"], ["code-review-base"])


if __name__ == "__main__":
    unittest.main()
