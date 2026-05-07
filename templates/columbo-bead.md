# Columbo Bead Template

Required sections for any bead labeled `columbo-test-spec` (forward mode) or
`columbo-test-gap` (audit mode). Columbo writes these. Tarzan/Optimus's
worker dispatches execute them. The skeleton test file (forward only) plus
the bead together form the worker's contract.

The standard Fresh Agent Test is the floor; this template is the ceiling.
Bead-description-gate.sh enforces the required sections per label.

---

## Sub-labels (mirror the 12-category taxonomy in `roles/columbo.md`)

Every Columbo bead carries one or more category sub-labels so Tarzan/Optimus
can filter by shape at claim time. The taxonomy is closed — do not invent
categories outside this list.

For `columbo-test-spec`:
- `columbo-test-spec:behavioral`        — category 1, every requirement covered
- `columbo-test-spec:boundary`          — category 2, edges and limits
- `columbo-test-spec:negative`          — category 3, invalid input / unauthorized access
- `columbo-test-spec:state-machine`     — category 4, state + transition completeness
- `columbo-test-spec:failure-modes`     — category 5, dependency failures, idempotency
- `columbo-test-spec:concurrency`       — category 6, ordering / race conditions
- `columbo-test-spec:integration`       — category 7, real DB / external API contract
- `columbo-test-spec:regression`        — category 8, prior bugs in the area
- `columbo-test-spec:invariants`        — category 9, property-based truths
- `columbo-test-spec:security`          — category 10, adversarial / injection
- `columbo-test-spec:performance`       — category 11, regression on hot path
- `columbo-test-spec:observability`     — category 12, useful errors / required logs

For `columbo-test-gap`: same suffixes (`columbo-test-gap:boundary`, etc.).

A bead may carry 1-3 sub-labels if cases span categories. >3 → split the
bead. Wide beads = shallow execution.

---

## Model label (required)

Every Columbo bead also carries `model:<haiku|sonnet|opus>` per SABLE.md
§6.9. Defaults Columbo applies:

| Bead shape | Default model |
|---|---|
| `columbo-test-spec:behavioral` for a CRUD endpoint, concrete inputs/expected | `model:haiku` |
| `columbo-test-spec:boundary` or `:negative` with concrete cases | `model:haiku` |
| `columbo-test-spec:integration` exercising real DB or external API | `model:sonnet` |
| `columbo-test-spec:state-machine`, `:concurrency`, `:invariants` | `model:sonnet` |
| `columbo-test-spec:security` (auth/payments/RLS/PII) | `model:opus` |
| `columbo-test-gap:*` for routine missing coverage | `model:haiku` |
| `columbo-test-gap:concurrency` / `:invariants` / `:security` | `model:opus` |

Apply the ladder, not the bead-type. Step UP if the spec has judgment-call
gaps or the area is debugging-heavy. Watch the mis-classifications: a
"single-file boundary test" for an auth-touching field still needs Opus.

---

## Forward-mode template — `columbo-test-spec`

```markdown
## Feature under test
{{ one sentence stating the behavior — same restatement Columbo confirmed
   with the user in Phase 1 of the conversation }}

## Test file
`<path-relative-to-repo-root>` — the skeleton file Columbo wrote. Worker
fills in the it.todo bodies and renames `.skel.test.<ext>` → `.test.<ext>`,
or merges the cases into an existing test file with the same coverage shape.

## Test layer
{{ One of: UNIT | E2E | EVAL. Drives skeleton-file placement and tells
   the worker what kind of test to write. Decision rules:
   - UNIT (default): pure functions, internal helpers, single-function
     edge cases, obscure flows
   - E2E: common user flow spanning 3+ components/services; integration
     where mocking would hide failures; auth/payment/destruction flows
   - EVAL: critical LLM call needing a quality eval; prompt-template
     change; system-instruction change
   One layer per bead — if cases span layers, split the bead. }}

## Cases
For EACH test case in this bead, fill all four fields:

- **Case name:** `<exact string from the it.todo / pytest.mark.skip>`
  - **Why:** <one sentence — what bug this catches or what intent it codifies>
  - **Inputs:** <concrete values — `name=""`, `count=2147483647`, `user.id=42 with role=admin`. Never "edge case" or "various."
  - **Expected:** <assertion shape — `422 with body {"error":"name_required"}`, `cache.get(key) returns None`, `event.attempts == 3 and final state == FAILED`. What is checked, not "should pass.">

Repeat per case. Bead with one case is fine. Bead with >12 cases → split.

## Categories
{{ comma-separated list from the 12-category taxonomy, e.g. "1, 2, 3" — must
   match the sub-labels on the bead }}

## Fixtures / setup
{{ Any non-trivial setup the worker needs: test DB seed, mocked clock,
   fake user, fixture factory, env vars. If nothing non-trivial: write
   "Fixtures: none." Do NOT omit the section — the gate checks for it. }}

## Out of scope
{{ Cases the user and Columbo discussed and explicitly chose NOT to test.
   Document them so the worker doesn't add them speculatively, and so a
   future audit run knows they were a deliberate decision rather than an
   oversight. If nothing was deferred: write "Out of scope: none — full
   coverage map landed in this bead." }}
```

## Audit-mode template — `columbo-test-gap`

```markdown
## Symptom
{{ One paragraph: what is shallow or missing in current coverage? Don't
   say "tests are weak" — say "the existing test for `process_refund`
   only covers the success path; nothing exercises partial-failure mid-
   batch where the gateway returns 200 for some items and 503 for others." }}

## Cited test file
- **Path:** `<path-relative-to-repo-root>` — existing test file that is
  shallow, OR the path where a missing test should live (use the project's
  test-directory convention).
- **Symbol:** `<test_function_or_describe_block_name>` if a shallow test
  exists; otherwise `<expected-symbol>` for a missing test.

## Cited source file
- **Path:** `<path-relative-to-repo-root>` — the source the gap concerns.
- **Symbol:** `<function / class / handler / method>` — what is undertested.

## Existing test quality
{{ Grade the cited existing test on the three-tier rubric, or note its
   absence. The grade tells the executing worker whether they're upgrading
   a thin test or near-rewriting one. Required for every columbo-test-gap
   bead.

   - **★★★** — tests behavior with edge cases AND error paths (covered;
     should NOT be a gap — drop the bead instead)
   - **★★** — tests correct behavior, happy path only; gap is missing
     edges / error paths
   - **★** — smoke test / existence check / trivial assertion (`it
     renders`, `doesn't throw`, single field equality); essentially no
     real coverage
   - **none — net-new test required** — no existing test at the cited
     site; the gap is true missing coverage

   Format:
     Grade: ★★ (or ★, or "none — net-new test required")
     Rationale: <1-2 sentences citing the existing test's actual shape:
                what it checks, what it skips> }}

## Fingerprint
A literal substring grep-able from the cited test file (or source if the
test doesn't exist yet). Choose something unique — `grep -n '<fingerprint>'`
should return ≤3 matches. Run the grep before submitting.

This is load-bearing. By the time a worker actions this bead, line numbers
have drifted; the worker uses the fingerprint to locate the current site.
Without it, the worker re-explores — defeating the point of the audit.

## Cases to add
Same shape as forward mode's `## Cases` section: case name, Why, Inputs,
Expected. Each case becomes one new test the worker writes.

- **Case name:** `<exact string for the new it / test()>`
  - **Why:** <what bug this catches>
  - **Inputs:** <concrete values>
  - **Expected:** <assertion shape>

Repeat per case.

## Categories
{{ comma-separated list from the 12-category taxonomy, must match sub-labels }}

## Risk if not addressed
{{ One paragraph naming a concrete cost. Choose at least one: future
   correctness risk (specific failure mode), reliability/regression risk
   (specific scenario that has bitten or will bite production), onboarding
   friction (concrete pattern that misleads readers about behavior), or
   silent contributor to other gaps in this audit pass. If you cannot
   articulate a concrete cost, the gap is not worth filing — drop it. }}
```

---

## Why each section exists

| Section | What it prevents |
|---------|------------------|
| Feature under test (spec) | Worker building tests for the wrong behavior |
| Test file (spec) | Worker creating a parallel test file when a skeleton already exists |
| Test layer (spec) | Worker writing a unit test when E2E was needed (or vice versa) — different cost/coverage profile |
| Cases with Why/Inputs/Expected | Generic happy-path tests that pass without exercising the case |
| Categories | Optimus/Tarzan can't filter by test shape without it; orientation for code review |
| Fixtures / setup | Worker reverse-engineering setup from prod code |
| Out of scope (spec) | Worker speculatively adding cases the user explicitly declined |
| Symptom (gap) | Vague "tests need work" gaps that reviewers can't act on |
| Cited test/source file (gap) | Worker re-exploring to find the audited site |
| Existing test quality (gap) | Worker not knowing whether to upgrade a thin test (★★) or near-rewrite a smoke-only one (★) |
| Fingerprint (gap) | Line drift between bead creation and execution |
| Cases to add (gap) | Worker fixing the wrong shallow spot |
| Risk if not addressed (gap) | De-prioritization without justification |

## Self-review checklist (Columbo runs before submitting any bead)

Before `bd create`, re-read the draft and confirm:

- [ ] Could a fresh worker take this bead + the cited skeleton file and write the implementation without re-interviewing the user? (Fresh Agent Test)
- [ ] Does every `## Cases` bullet have Why + Inputs + Expected (all three, not "various" or "edge cases")?
- [ ] Forward only: is `## Test layer` set to one of UNIT / E2E / EVAL, with the cases all matching that layer (no mixed-layer beads)?
- [ ] Forward only: do the case names exactly match `it.todo` strings in the cited skeleton file? (Run a grep.)
- [ ] Forward only: does the skeleton file's directory match the test layer (unit-test dir / e2e dir / evals dir)?
- [ ] Audit only: is `## Existing test quality` filled with a grade (★/★★/★★★) or `none — net-new test required`?
- [ ] Audit only: if grade is ★★★, this should NOT be a gap bead — drop it. (★★★ tests are covered, not gaps.)
- [ ] Audit only: does the fingerprint grep to ≤3 matches in the cited file? (Run the grep.)
- [ ] Are categories listed consistent with sub-labels on the bead?
- [ ] Is the model: label appropriate per the heuristic table — and if stepped UP, is the reason in `## Notes`?
- [ ] Is `## Out of scope` filled (forward) — even if "none — full coverage map landed"?
- [ ] Is `## Risk if not addressed` (gap) a concrete cost, not a generic "tests are good"?
- [ ] **Regression rule (forward, IRON):** if the feature touches existing code, is at least one filed bead a regression-test bead at priority ≤ 1?
- [ ] Could two beads merge? (Often `:behavioral` + `:boundary` for the same feature collapse cleanly.)
- [ ] Could one bead split? (Often a `:state-machine` bead is actually `:state-machine` + `:invariants` stapled together — split.)

If any answer is "no" or "not sure," revise before submitting.

---

## Where this template is referenced

- `templates/multi-manager/agents.yaml` → `columbo.bead_template: templates/columbo-bead.md`
- `templates/multi-manager/roles/columbo.md` → Phase 5 (Produce output)
- `hooks/bead-description-gate.sh` → enforces required sections per label
