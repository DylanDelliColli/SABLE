# Merge-gate baseline measurement (SABLE-nueh3)

One-page method note for the SABLE-jd5fj "merge pipeline v2" planning dossier.
Landing precondition for jd5fj phase 2 (S2 optimistic disjoint promotion) per
Sherlock RESEARCH — records the *current* serial `sable-merge-gate promote`
path so the post-change comparison is apples-to-apples. Full numbers also
recorded in `.claude/sable/state/planning/SABLE-jd5fj/research.json`.

## Method

All data comes from three sources on this repo, no synthetic data:

1. **`git log tmux-only`** — every `promote()` call builds the landed merge
   commit via `commit-tree base_tip -p base -p branch`, so the worker branch's
   original tip commit (with its real author/committer date) survives forever
   as the merge commit's second parent, even after the worker branch itself is
   deleted on green (`cleanup_after_merge`, SABLE-dn7r). This gives an exact
   count and timestamp for every promotion, and a coarse latency proxy
   (merge-commit date − branch-tip date) for all of them.
2. **`~/.claude/sable/logs/post-push-merge-notify.log`** — the pre-push hook's
   `CONFIRMED local=<sha> remote=<sha>` lines give real push timestamps. Joined
   against the matching `ci-verify/<bead>-<sha7>` GitHub Actions run's
   `updatedAt` (CI-completion instant, just before the fast-forward push) via
   `gh run list --workflow=ci-verify --json ...`, this gives a precise
   push→CI-done latency for the subset of promotions the log still covers.
3. **`bd` evidence notes** (`_append_evidence` in `bin/sable-merge-gate`) and
   `gh run list` history — cross-referenced to find every case where a
   preview run (`ci-verify/*`) went green and a later run on the same headSha
   on `tmux-only` did not.

Two important scope notes, both discovered while measuring (see beads below):

- Textual-conflict rejections (exit 22, `git merge-tree` conflict) and
  precondition failures (exit 3) are raised in `build_preview()` *before* the
  `promote()` try/finally that calls `_append_evidence`/`_notify` — they leave
  **no bd note, no GH run, no git object**. The historical exit-22 rate is
  therefore **unrecoverable** from this repo's history (SABLE-lxvl2 files the
  fix so future baselines don't hit the same wall).
- `_append_evidence` shells out with `check=False` and swallows failures
  silently. One real instance was found: the `wk-reaper-guard` merge was
  promoted with `--bead SABLE-reaper-guard` (a branch-derived slug, not the
  real id `SABLE-517s`), so its GREEN note silently failed to write — accounts
  for the 125-vs-126 GREEN-note/landed-merge discrepancy below (SABLE-aq7ij
  files the fix).

## Results

### Measurement window

126 promoted merges landed on `tmux-only` between 2026-07-14T13:22:04 and
2026-07-17T12:29:48 (-0400), ~71 hours of gate activity, then a gap to today
(2026-07-21) with no further gate activity — i.e. all data below is from one
contiguous multi-day drain, not cherry-picked.

### (1) Push-to-integrated latency, current serial path

| Measure | Coarse proxy (n=126, commit-date only) | Precise push→CI-done (n=47, real push log × gh run join) | Precise, same-session only (n=41, excludes a 15h overnight gap) |
|---|---|---|---|
| median | 647s | 1621s | 1513s |
| p90 | 3571s | 54846s | 3841s |
| p95 | 54275s | 55147s | 4944s |
| mean | 4025s | 8721s | 1950s |

The same-session column (n=41) is the honest floor for the gate's own
contribution — the wide p90/p95 in the other columns is a shift-boundary
artifact (6 branches pushed 22:29–22:46 on 07-16, not gated until 13:33–14:07
on 07-17), not the merge-queue mechanism.

Directly corroborating, independently-recorded natural experiment
(SABLE-iqnq, chuck, 2026-07-17 post-crash-recovery drain, 11 back-to-back
fires): **fire→land 304–331s per item, mean ~325s, sd ~10s** — CI-run-duration
dominated, essentially independent of diff size/shape. Matches this
measurement's own GH-run-duration computation on all 131 successful preview
runs: **median 259s, p90 312s**.

**Headline: the current per-merge floor is ~5–6 minutes (one CI run), and the
same-session queue latency (median ~25 min, p90 ~64 min) is dominated by CI
run duration × queue depth, not by the gate's own logic** — consistent with
the "ceiling is ~one CI run per merge by construction" framing in the bead
title.

### (2) Textual conflict rate (exit-22 class)

**Unmeasurable from history** (see Method above). This repo's exit-22 count
is not recoverable; SABLE-lxvl2 fixes the trail going forward. Published
base rates (arxiv 2604.03551: 27.67% overall; 2607.04697: 19.8–41.7% under
temporal overlap) remain the best available reference for this class — they
are conservative lower bounds by the source papers' own admission, and this
repo's actual rate is unknown but structurally rejected before landing either
way (exit 22 already blocks promotion; jd5fj does not touch this path).

### (3) THE KEY UNKNOWN: textually-clean-but-semantically-broken rate

**Zero directly observed instances in 126 promotions / ~271 CI runs.**

Cross-referencing every `ci-verify/*` preview run against the corresponding
`tmux-only` post-promote run for the same headSha found:

- 1 case (`SABLE-ita7`, `6d073a4be8`) where the **identical object** went
  green on preview and red 4 minutes later on `tmux-only`. This predates the
  SABLE-r3i6 same-SHA dedup guard (landed one day later, 2026-07-16), so
  before r3i6 every promotion re-ran the full suite from scratch — this is
  read as CI/test non-determinism (the bead's own acceptance criteria cite
  pre-existing ambient-env-ordering flaky tests SABLE-ne44/4mlu/dybx), **not**
  a semantic merge conflict. It is not a new finding — it's a known flaky
  class.
- 3 cases of green→cancelled, all mechanical (`concurrency: cancel-in-progress`
  on the `ci-verify` workflow cancels an in-flight run when a later push lands
  on the same ref).
- 3 additional `tmux-only` failures predate the very first gate commit
  (pre-gate bring-up noise, not merge-gate events).

**Why zero is expected, not reassuring:** under the current design, the exact
tested tree object is what gets fast-forwarded onto the base (byte-identical
promotion) — there is no path today where the *content* that lands differs
from the content that was CI-tested, other than the base moving underneath it
(handled explicitly as exit 23, non-promoting, retry-safe, observed at ~4/131
successful previews ≈ 3%). **S2 is exactly the change that would remove this
guarantee** by skipping re-verification on disjoint footprints — so this
baseline's 0/126 is not evidence the semantic-break rate is low; it is
evidence that the current design structurally prevents the class from
occurring, which is precisely what S2 proposes to relax.

For an apples-to-apples pre/post comparison: treat the pre-S2 rate as
**0/126 observed, rule-of-three upper bound ≈ 2.4% (3/126)** at the historical
sample size available. S6 (before/after telemetry, per the jd5fj
test-strategy) must measure this rate directly and continuously once S2
ships, since it cannot be bounded any tighter from history — the class is
definitionally unexposed by the design being measured.

## Beads filed during this measurement

- SABLE-lxvl2 — exit-22/exit-3 rejections leave no durable evidence trail
- SABLE-aq7ij — `_append_evidence` silently swallows bd update failures
  (confirmed data loss on `wk-reaper-guard` / SABLE-517s)
- SABLE-x68wo — 3 orphaned `ci-verify/*` refs past the sweep age threshold
  (operational hygiene sighting)
