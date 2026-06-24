# sable Compatibility Ledger

This ledger proves that the `sable` pack preserves the Gas City `build-base`
contract while layering SABLE's opinion (adversarial planning + mechanical gates)
on top. Each claim names the files that prove it; the Evidence Commands section
gives the exact commands that reproduce the proof.

This ledger is the pack-local evidence for `GC-METH-012` (external implementation
compatibility) in `../gascity/REQUIREMENTS.md`;
`../gascity/tests/test_derived_pack_compatibility.py` enforces the claims below
for every *registered* derived pack. `sable` registers in Phase 3 (EPIC
`SABLE-vj4x.8`); until then the claims marked **(Phase 1)** are the live, provable
subset and the rest are the committed target.

## Compatibility Claims

- **Import contract (Phase 1):** `sable/pack.toml` imports the Gas City pack as
  `gc` from `../gascity`, so the pack inherits the shared `gc.*` surface (base
  formulas, role agents, template fragments) instead of re-defining it.
- **Prompt hygiene (Phase 1):** the pack-level worker-protocol fragment
  `sable/template-fragments/gc-role-worker.template.md` is byte-identical to the
  base `../gascity/roles/prompts/shared/gc-role-worker.md.tmpl`, carrying the Gas
  City claim protocol unchanged. Every per-agent nested fragment copy added in
  later phases must match it.
- **Formula contract (Phase 1+):** `sable/formulas/sable-build.formula.toml`
  declares `extends = ["build-base"]` and preserves the inherited anchor order;
  it pins SABLE's opinion only in the planning and review slots
  (`planning_formula = sable-planning`, `code_review_formula = sable-review`) and
  reuses the base formulas/roles for everything else during bootstrapping.
- **Gate contract (Phase 1):** SABLE's mechanical gates ship as pack
  check-scripts under `sable/assets/scripts/checks/` (test-evidence +
  unit/integration mandate; scope-creep diff containment) and are wired onto the
  review step as `[steps.check.check]` execs — the same gate mechanism the base
  uses for `build-artifact-valid.sh`.
- **Providerless routes:** every step routes via `gc.run_target` to a
  providerless pack-local agent (`sable.*`) or a `gc.*` role; no lane dispatches
  provider-native subagents.
- **Planning slot (Phase 1 = stub, Phase 2 = full):** `sable-planning` extends
  `planning-base` and preserves the required `requirements` + `plan` artifact
  gates. Phase 1 ships a framing-only stub (office-hours); Phase 2 replaces the
  single planning slot with SABLE's adversarial multi-substage planning.
- **Full methodology selectors + registration (Phase 3):** the remaining
  pack-local formulas (`sable-decomposition`, `sable-implementation`,
  `sable-work`, `sable-work-item`, `sable-fix-loop`) and the
  `THIRD_PARTY_BUILD_PACKS` registration land in Phase 3, at which point the
  shared contract suite covers `sable` at parity with the other packs.

## Evidence Commands

Run these from the gascity-packs checkout root (where `sable` is a sibling of
`gascity`):

```sh
sed -n '1,8p' sable/pack.toml
diff sable/template-fragments/gc-role-worker.template.md gascity/roles/prompts/shared/gc-role-worker.md.tmpl  # expect no output
python3 -m pytest sable/tests/test_sable_pack_structure.py -q
gc lint sable
```

## Notes

- `sable/README.md` references this ledger so the pack documentation points to
  the compatibility contract directly.
- The ledger is pack-local; the base contracts remain defined by `gascity` and
  are inherited through `build-base`.
- Claims marked **(Phase 1)** are live now; **(Phase 2)** / **(Phase 3)** are the
  committed target tracked under EPIC `SABLE-vj4x`.
