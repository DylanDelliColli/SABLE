# Test-suite audit playbook

This is the reusable method behind SABLE's test-cost audit. It is intended as
design input for a standalone skill or tool that can audit an unfamiliar
repository without buying speed by silently deleting authority.

The optimization target is not “fewer tests.” It is:

> Lower the wall time of the workflow that gates the user's next useful action,
> while preserving the set of behaviors and real boundaries that authorize
> that action.

That distinction matters. Test count, summed test duration, runner wall time,
CI queue time, and number of CI job slots consumed are different quantities.
Optimizing the wrong one can make the fleet slower.

## 1. Discover the real verdict before measuring

Build a verdict manifest from the commands the repository actually executes:

- local edit/test loop;
- pre-commit and pre-push hooks;
- pull-request or merge-preview CI;
- merge queue/train;
- scheduled or release snapshots.

For every verdict, record:

- exact commands and test membership;
- whether selection is full, impact-based, or allowlisted;
- exclusions and their reasons;
- installed and deliberately absent tools;
- number of CI jobs and account concurrency limits;
- caches, prior artifacts, retries, timeouts, and nested runners;
- which output or status is the final merge authority.

Do not start from the framework's conventional command. A repository can run
the same underlying suite through wrappers, hooks, coverage tools, selectors,
or nested test processes. Static discovery should scan workflow files, hook
configuration, task runners, package scripts, and test code that invokes other
test runners.

Before optimization, save an invariant manifest: collected test identifiers,
allow/exclude membership, required static checks, expected skips, planted
negative controls, and the real-boundary proof owned by each expensive family.
After optimization, diff that manifest. A faster run with missing authority is
not an improvement.

## 2. Make the benchmark environment faithful

Environment fidelity is part of the test contract, not benchmark housekeeping.
Record at least:

- operating system and CPU allocation;
- runtime and dependency versions;
- complete executable search path;
- presence and absence of optional CLIs;
- permission to create sockets, subprocesses, containers, and network
  connections;
- global Git/runtime identity and default-branch settings;
- clean versus dirty checkout;
- warm versus cold caches;
- other test processes sharing the host.

Add positive controls for important assumptions. If tmux is required, create a
socket before accepting the benchmark. If a clean room must omit a database
CLI, assert that it is absent. If npm is expected, assert that it resolves.
Print this contract into the captured evidence.

Three SABLE attempts demonstrated why:

1. A filesystem sandbox denied tmux socket creation and produced 83 apparent
   test failures. That was a sandbox measurement, not a suite baseline.
2. A filtered `PATH` correctly hid bd/Dolt but accidentally hid npm too. The
   resulting shell failure did not model CI.
3. A test passed only in a dirty checkout because it piped the working-tree
   diff into a fixture. A clean checkout exposed that `git apply` rejects an
   empty patch. Dirty-state success had hidden a correctness defect.

A general tool should refuse to compare runs whose environment fingerprints
differ on declared contract fields.

## 3. Measure in-band, once

Instrumentation should wrap the authoritative execution rather than launch a
second “profiling” pass. Capture:

- workflow wall time;
- queue time;
- setup time;
- per-lane wall time;
- per-module and per-test duration;
- summed test duration;
- retries, skips, deselections, and collection count;
- peak and average CPU/memory where available;
- job-slot count and billed runner time.

Use a monotonic clock. Preserve the original exit status. Write reports
atomically, and treat a missing or malformed report as unknown—not green.

Measure at least two comparable runs after the design stabilizes. One run is
enough to reject a hypothesis, but rarely enough to establish a performance
contract. Report both absolute and percentage changes and retain raw
machine-readable evidence.

The useful cost equation for a rolling-worker system is approximately:

```text
time-to-next-wave =
  queue delay
  + critical-path setup
  + max(concurrent verdict lanes)
  + promotion/landing serialization
```

Summed test seconds remain important because they expose contention and compute
waste, but the merge bottleneck is usually critical-path wall time.

## 4. Classify cost before changing it

Assign each hotspot to one or more causes:

- repeated immutable fixture or dependency setup;
- process-launch or CLI parsing overhead;
- guessed sleeps and polling intervals;
- one real boundary crossed for every state-machine permutation;
- nested runner or wrapper replaying an authoritative suite;
- duplicate workflow steps producing the same evidence;
- wrong-tier work in the edit or merge path;
- broad collection/import cost;
- unavoidable real integration authority;
- host contention or a shared mutable resource.

The safest recurring shape is:

1. exhaustive fast tests for decision logic;
2. one real end-to-end proof for each owned authority boundary;
3. a planted negative control where a false green is otherwise plausible.

This is not permission to replace integration tests with mocks. It is a prompt
to stop paying for the same cold real boundary for every already-covered
decision permutation.

When many tests need the same complex real fixture, first ask whether production
has too many authority surfaces. Test cost can be architectural evidence. A
single production landing or state authority can remove more test complexity
than an elaborate scheduler.

## 5. Experiment with falsifiable hypotheses

Rank hypotheses before editing. Change one scheduling or setup variable at a
time, and define the rejection condition in advance. A useful sequence is:

1. remove exact duplicate evidence;
2. share immutable setup and copy/rebind mutable fixture state;
3. replace guessed waits with observable readiness;
4. overlap already-independent verdict lanes;
5. add bounded within-lane concurrency only if measurement justifies it;
6. change test membership only with an explicit authority proof.

For concurrency, run a plant before building resource taxonomy. Start with a
small fixed width. Capture every child log and result independently. If it
fails, use the failure to identify the real resource collision; do not invent a
large scheduling framework preemptively.

Common resource classes worth checking are:

- CPU and memory;
- tmux/server socket names;
- fixed TCP ports;
- Docker daemon, images, and container names;
- process-table observation;
- global Git configuration;
- `HOME`, `TMPDIR`, and fixed files under `/tmp`;
- shared databases, bd/Dolt stores, locks, evidence files, and journals;
- a repository's real refs or working tree.

Concurrency is acceptable only if every child remains attributable and the
combined verdict is fail-closed. A scheduler child that exits without a
parseable result must make the parent red.

Measure concurrency at both levels:

- **within one verdict**, where a runner may overlap independent lanes; and
- **across the fleet**, where 15 workers can each launch a nominally serial,
  scoped test process on the same development host.

An isolated one-runner benchmark says nothing about the second case. Record
host CPU/memory capacity, simultaneous worker cardinality, and per-worker
latency under a representative swarm. Avoid enabling internal test parallelism
in every worker by default: 15 workers are already a parallel scheduler. Also
avoid a universal global lock without evidence; it can serialize cheap,
independent unit runs. If admission control is needed, place the smallest
configurable token budget around the measured contended boundary (for example
real bd/tmux/Docker integration), not around all testing.

Do not compare a shared development-host result with an isolated CI result as
if only the code changed. During SABLE's audit, a green top-level run took
715.72 seconds while an unrelated repository's pytest process and persistent
database servers shared the 14-core host. The accepted ephemeral CI jobs took
4m14s and 4m11s end to end. The shared result is valuable evidence that fleet
contention needs its own experiment, but it is not a valid regression baseline
for the CI topology.

## 6. Interpret speed versus contention honestly

In SABLE's clean-room baseline:

- full Python: 304.90 seconds;
- serial shell allowlist: 400.64 seconds;
- sequential critical path: 705.54 seconds.

Overlapping Python with serial shell reduced wall time to 471.26 seconds, but
shell aggregate time rose by 17.6%. Running two shell suites at once reduced
shell wall time to 282.70 seconds while increasing aggregate shell time from
397.8 to 539.3 seconds. Early one-job plants with Python, two-wide shell, and
static authority were green near 400 seconds.

That faster topology was still rejected. A later repeat failed a real tmux
message-delivery test after exhausting eight verified attempts and took
508.51 seconds. Several greens had hidden a contention-flake distribution.
SABLE therefore ships only coarse Python + serial-shell + static overlap. This
is a central audit lesson: a faster percentile is not acceptable when the tail
creates false reds, and repeated runs must include the proposed concurrency.

Keeping one job remains appropriate because each preview gets a dedicated
runner, GitHub queues by job slot, and a second workflow job per preview would
cut rolling-fleet concurrency at the account's 20-job ceiling. It would not
automatically be appropriate for a shared self-hosted runner or a
compute-billed environment.

The general report must therefore show both:

- latency won on the critical path; and
- extra aggregate compute and variance introduced.

## 7. Preserve diagnostic quality

Parallel output must not become an unreadable interleaving. Capture one log per
test or lane, then replay it under a stable name. The final summary should name
every failed, missing, or malformed result and preserve the original exit code.

Keep serial execution available for local diagnosis. CI can opt into bounded
parallelism without forcing every developer to debug buffered concurrent logs.

Do not allow a self-skip to look like full coverage. Important optional-boundary
tests need an explicit skip count or disposition, and the top-level verdict
should surface it.

### Same-host swarm diagnosis

When the production topology is many scoped workers on one development host,
audit each selected workload for every boundary it actually reaches. A module
named for tmux may also execute `bd`, Git, Docker, or a repository-local helper.
Static classification by filename can therefore put admission around the
symptom while leaving the contended resource hidden.

Use this sequence:

1. Establish sequential observations and a simultaneous representative plant
   with identical membership.
2. Preserve per-worker active latency, scheduler wait, aggregate worker wall,
   aggregate CPU, host CPU/memory/pressure, stable logs, and shared-resource
   deltas.
3. Separate name/state isolation from performance. A private socket or temp
   root can contain residue while every worker still blocks on another shared
   database.
4. Inspect blocked subprocess stacks and the complete command tree. Logged
   lock errors are useful but not required evidence of a shared-resource
   bottleneck.
5. Change one boundary at a time. A test-local unavailable-resource shim is a
   valid causal probe only when unavailability is already the intended case;
   it is not a substitute for a required real-boundary proof.
6. Repeat the unconstrained 15-worker plant after isolation. Add configurable
   admission only if the isolated real boundary still fails or creates an
   unacceptable tail.

Timeout cleanup must account for daemonization. A subprocess can escape the
worker's process group, and a daemon may rewrite the environment marker used
to find it later. Capture descendants while their identity is attributable,
pair each PID with an immutable start-time value to avoid PID reuse, report
survivors before cleanup, terminate only exact recorded identities, and prove
none remain. Missing resource records from killed workers are unknown, not
zero.

Keep queue time distinct from active worker latency when evaluating a token
width. A narrow admission experiment may pass while making total wall time
and worker service time much worse. Compare it with resource isolation and
ordinary OS scheduling before adopting the queue.

## 8. Standalone tool shape

A useful cross-repository auditor can stay small if it separates adapters from
judgment:

1. **Discovery adapters** identify workflow, hook, and framework commands.
2. **Environment probe** fingerprints tools, versions, permissions, and
   clean/dirty state, with declared positive controls.
3. **Runner adapters** collect membership, wall time, per-test costs, skips,
   retries, and exit polarity in-band.
4. **Static boundary scanner** finds tests and wrappers that launch pytest,
   shell suites, package test commands, containers, or other runners.
5. **Invariant recorder** saves membership, exclusions, boundary declarations,
   and planted controls before and after.
6. **Experiment reporter** compares wall time, aggregate work, job slots, and
   variance without automatically deleting or moving tests.

The tool may recommend:

- exact duplicate runner removal;
- immutable fixture-template reuse;
- readiness signals;
- candidate independent lanes;
- resource declarations for observed collisions;
- wrong-tier tests and nested-runner review.

It should not automatically:

- prune tests from duration or coverage overlap alone;
- convert real-boundary tests to mocks;
- bless statusless/skipped evidence;
- compare mismatched environments;
- introduce broad concurrency from one green sample.

Architectural ownership—what behavior authorizes a merge—still requires human
review. The tool's job is to make that decision evidence-rich and difficult to
accidentally weaken.

## Acceptance template

```text
On two comparable clean-room executions:

- critical-path wall time is at least <target>% below the current-HEAD baseline;
- the workflow consumes no more than <N> CI job slots per verdict;
- collected test and allowlist membership is preserved or every change has an
  explicit authority disposition;
- required real-boundary proofs and planted negative controls remain
  load-bearing;
- every lane/test leaves attributable status and output;
- skips, deselections, missing results, and corrupt reports fail loud;
- the complete sealed verdict is green.
```

The repository-specific measurements and reductions that produced these
learnings remain in [TEST-COST-AUDIT.md](TEST-COST-AUDIT.md). The separate
15-worker local-host study, including full per-worker latency records and the
decision not to add broad admission, is in
[TEST-CONTENTION-AUDIT.md](TEST-CONTENTION-AUDIT.md).
