Run the SABLE code review and write the review report.

Validate the review context, then review the implementation against the
approved requirements and plan. Write the review report artifact (schema
`gc.build.review.v1`) to the formula-provided path and record its resolved path
on the workflow root (`gc.build.review_report_path`).

Phase 1 inherits the base review behavior (synthesis by gc.review-synthesizer).
SABLE's mechanical gates — test-evidence (unit + integration mandate) and
scope-creep containment — run as dedicated steps in sable-build and are
independent of this agent-judged report. SABLE's adversarial review lenses (the
multi-perspective gauntlet + the 3-verdict acceptance/test-evidence/simplicity
gate) are elaborated in later phases.
