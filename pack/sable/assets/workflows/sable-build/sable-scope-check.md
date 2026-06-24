SABLE mechanical gate: scope-creep containment.

No implementation work happens at this step. This gate mechanically verifies
that the worker touched only what the bead's declared scope allows.

The gate check (`.gc/scripts/checks/scope-creep-diff.sh`) reads the aggregate
implementation-summary Changed Files section and asserts every changed file
falls within the declared scope (`gc.scope.allowed_paths`). Out-of-scope edits
fail the gate; an explicit `[scope-override] <reason>` in the summary waives it.
In Phase 1 the gate fails open when no scope is declared (the base decomposition
does not yet record per-bead scope; sable-decomposition makes it enforcing in
Phase 3).

Confirm the implementation-summary is reachable, then close this step.
