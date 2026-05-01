# Sherlock Finding Bead Template

Required sections for any bead labeled `sherlock-finding`. Sherlock writes
these. Victor validates them. Optimus/Tarzan execute them.

The standard Fresh Agent Test is the floor; this template is the ceiling.

---

```markdown
## Title (one line, declarative)
{{ what's wrong, in <10 words }}

## Category
{{ pick exactly one sub-label, the bead must also carry it }}
- `sherlock:design-rot`     — architectural smell, wrong abstraction, leaky boundary
- `sherlock:redundancy`     — duplicated logic, parallel implementations
- `sherlock:verbosity`      — overlong module/function/configuration that obscures intent
- `sherlock:dead-code`      — unreachable, unreferenced, gated by a removed flag
- `sherlock:test-gap`       — missing or weak coverage of a documented behavior

## Rationale
Why is this bad? What does it cost (correctness, performance, readability,
onboarding time)? Don't say "it's bad practice" — say "this causes X."

## Evidence
For EACH location involved in this finding, provide all of:

### File: <path>
- **Symbol:** <function / class / const / module name>
- **Fingerprint:** <a literal substring grep-able from the file — preferably
  unique enough that `grep -n '<fingerprint>'` returns ≤3 matches>
- **Anchor:** line N @ commit <SHA>   *(optional but cheap)*
- **What's wrong here:** <1-2 sentences specific to this site>

Repeat the block per file/site. If the finding spans many sites, list at
least the 3 most representative; note the total count.

## Proposed approach
What should be done? Concrete enough that a worker can execute without
re-deriving the design. Don't write the code; write the plan.

## Scope estimate
- Size: S / M / L
  - S: one file, one PR, <2hrs worker time
  - M: multiple files in one subsystem, one PR, half-day
  - L: cross-cutting, may need an epic with child beads
- Risk: low / medium / high (chance of regression / breakage)
- Recommended worker model: haiku / sonnet / opus
  - The bead must also carry the matching `model:<haiku|sonnet|opus>` label.
  - Apply the ladder, not the bead-type. Default Sonnet. Step DOWN to Haiku
    only if ALL four: mechanical work, deterministic spec, low-risk path,
    no judgment calls. Step UP to Opus if ANY: design thinking, security-
    sensitive (auth/payments/RLS/PII), cross-cutting impact, spec has
    judgment-call gaps, unclear debugging.
  - Watch for mis-classifications: "epic child = Opus" (many are mechanical),
    "single-file = Haiku" (single-file auth changes still need Opus),
    "sherlock:design-rot = Haiku" (often needs Opus). The `sherlock:dead-code`
    sub-category is the most reliably Haiku-shaped.

## Risk if not addressed
What happens if this stays in the codebase? Choose at least one:
- Future development cost (specific examples)
- Reliability or correctness risk (specific failure mode)
- Onboarding friction (concrete pattern that confuses readers)
- Silent contributor to other findings in this audit pass

## Test spec
Both unit AND integration unless explicitly opted out. Required by SABLE
Prime Directive #2 — same as any bead.

- **Unit:** <file::test_name + assertion>
- **Integration:** <file::test_name + real dependency it exercises>
- **If [no-integration]:** explicit reason. Sherlock should rarely use this
  exemption — most design findings ARE composition issues that integration
  tests exercise.

## Acceptance criteria
- <criterion 1>
- <criterion 2>
- The fingerprint above no longer matches in HEAD (or is replaced by the
  intended construct)
```

---

## Why each section exists

| Section | What it prevents |
|---------|------------------|
| Category sub-label | Optimus/Tarzan can't filter findings by type without it |
| Rationale | Vague "should refactor" beads that workers can't act on |
| Evidence with fingerprint | Line drift between bead creation and execution; worker re-exploring to find the issue |
| Proposed approach | Worker re-deriving the design from scratch |
| Scope estimate | Mis-bundling — Optimus pulls a "small" bead that turns out to be a 2-day epic |
| Risk if not addressed | De-prioritization without justification — "this can wait" with no cost analysis |
| Test spec | Standard SABLE non-negotiable |
| AC with fingerprint check | Verifying the fix actually addressed the cited site |

## Self-review checklist (Sherlock runs before submitting any bead)

Before `bd create`, re-read the draft and confirm:

- [ ] Could a fresh agent execute this without opening any other context window? (Fresh Agent Test)
- [ ] Does the fingerprint actually grep to the cited line? (Run the grep.)
- [ ] Is the category accurate, or is this actually a different sub-label?
- [ ] Is the scope estimate honest? (When in doubt, size up — Optimus rebundles down.)
- [ ] Does the test spec exercise the fix, not the surrounding scaffolding?
- [ ] Could two of these findings be merged into one cleaner bead? (Merge if yes.)
- [ ] Could one of these findings be split into two cleaner beads? (Split if yes.)

If any answer is "no" or "not sure," revise before submitting.
