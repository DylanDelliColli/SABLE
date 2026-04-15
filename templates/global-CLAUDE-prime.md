# Global Instructions

## ⚠️ Prime Directive

**ALL work flows through beads. No exceptions.**

Every bug fix, feature, refactor, investigation, and noticed issue must exist as a bead before you act on it. If you cannot point to the bead your current action serves, stop and create one. `bd q "<title>"` takes three seconds — there is no task too small. Never substitute TodoWrite, scratch lists, or memory for beads. This is the foundation of every workflow rule below; bypassing it corrupts the methodology.

If the work seems too small for a bead, create one anyway. Your sense that "this one doesn't need a bead" is the failure mode this entire framework was built to prevent.

## ⚠️ Test Coverage Required: Unit + Integration

**Every code change requires both unit AND integration tests. Unit tests alone are insufficient.** Smoke tests are strongly encouraged but not gated.

Unit tests with mocked dependencies routinely pass while shipping broken systems. Integration tests must exercise real composition (real DB via Docker/sqlite, real HTTP, real file system) — mocking the database in an integration test defeats its purpose. If a change genuinely cannot be integration-tested, the bead must explicitly say why. See SABLE §4.5 for the full rationale and the bead-description template that captures both layers.

---

