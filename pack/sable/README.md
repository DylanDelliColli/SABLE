# sable Gas City Pack

`sable` is the portable opinion layer of **SABLE** expressed as a derived Gas
City pack on top of the `gascity` base. SABLE's durable value is its *opinion*,
concentrated in two places: an adversarial, opinion-eliciting **planning** phase,
and a set of mechanical **gates** — test-evidence with a unit+integration
mandate, scope-creep diff containment, and the Fresh-Agent-Test bar. Execution is
commodity: the `gascity` engine owns the role-agnostic orchestrator reconcile
loop that drives runs *outside* the operator session (walk-away execution). Keep
the opinion portable; even Gas City is rented.

Like every derived methodology pack, `sable` imports the Gas City base as `gc`
(see `pack.toml`) and layers its opinions on the shared `build-base` contract —
inheriting the `gc.*` role agents, base formulas, and template fragments rather
than re-defining them.

## Status: bootstrapping (Phase 1)

This pack is built in phases (see EPIC `SABLE-vj4x`):

- **Phase 1 (in progress)** — pack skeleton; the SABLE gate check-scripts
  (test-evidence, scope-creep); `sable-build` + `sable-review` formulas that reuse
  the base `gc.*` roles; and a deliberately **stubbed** `sable-planning`
  (office-hours framing only) — validated end-to-end as a walk-away autonomous
  run.
- **Phase 2** — the real SABLE planning process (adversarial elicitation:
  framing → research → architecture → test-strategy → a plan-review gauntlet),
  swapped into the single planning slot.
- **Phase 3** — full derived-pack contract compliance + registration, so the
  shared `gascity` compatibility suite covers `sable` at parity with the other
  methodology packs (compound-engineering, superpowers, bmad, gstack).

## Compatibility ledger

The pack-local compatibility ledger lives at [`REQUIREMENTS.md`](./REQUIREMENTS.md)
and records the `build-base` contract proofs — the inherited `gc` import and the
planned overrides — together with the evidence commands that reproduce each claim.
It is the pack-local evidence for `GC-METH-012` (external implementation
compatibility) tracked in `../gascity/REQUIREMENTS.md`.
