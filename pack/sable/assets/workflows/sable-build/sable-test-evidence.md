SABLE mechanical gate: test-evidence (the unit + integration mandate).

No implementation work happens at this step. The implementation is already
complete and summarized; this gate mechanically verifies the proof rather than
trusting any agent's self-reported verdict.

The gate check (`.gc/scripts/checks/test-evidence.sh`) reads the aggregate
implementation-summary artifact and requires that its Verification section
records BOTH a unit-test proof AND an integration-test proof (SABLE Prime
Directive), honoring `[no-integration] <reason>` / `[no-test] <reason>` escapes
for genuinely non-applicable work.

Confirm the implementation-summary exists and is reachable, then close this
step. If the gate fails, the missing test evidence must be added to the
implementation before the build can finalize.
