# Same-host scoped-test contention audit

Audit date: 2026-07-28

Tracking bead: `SABLE-x2r7g`

Measurement harness SHAs: `d8192c6`, `24643bd`

Accepted solution SHA: `def231e`

## Outcome

SABLE does not need a global test lock or a host-wide admission controller for
scoped worker tests. The measured failure was narrower: the real-tmux worker
status integration module used placeholder bead ids but allowed
`sable-worker-status` to resolve them through the checkout's real `bd` command.
Fifteen nominally tmux-only workers therefore opened one shared embedded Dolt
store while also creating hundreds of tmux processes.

The accepted change isolates that accidental test dependency and gives every
tmux test a private, cleaned socket root. It does not change production
authority or test membership. Two unconstrained 15-worker tmux repeats passed
in 29.65 and 35.90 seconds; the baseline timed out all 15 workers at 300
seconds. Two unconstrained representative mixed repeats passed in 66.59 and
53.75 seconds, down 58.3% and 66.4% from the 159.79-second baseline.

No admission rule was added. Cheap and subprocess-heavy tests remained
unconstrained, the real-bd/Dolt workload remained real, and every worker still
ran exactly one sealed catalog module without nested fan-out.

## Benchmark contract

The host was a representative shared development machine, not an idle
laboratory runner:

| Property | Value |
| --- | --- |
| Platform | WSL2, Linux `6.6.87.2-microsoft-standard-WSL2` |
| CPU | 14 logical CPUs, Intel Core Ultra 7 165U |
| Memory | 25,200,099,328 bytes (23.47 GiB) |
| Python / pytest | 3.13.9 / 9.1.1 |
| tmux | 3.6b |
| bd / Dolt | 1.0.5 / 2.1.9 |
| Git | 2.43.0 |
| Ambient load | Docker/Supabase services, browser, and interactive agent processes |

Every report captures the exact Git SHA and cleanliness, environment and tool
fingerprint, ambient process sample, positive tmux socket control when needed,
worker launch and completion times, GNU `time` resource record, pytest
membership, stable log and digest, procfs host samples, pressure-stall deltas,
shared-path changes, and attributable processes.

The built-in workload catalog is deliberately closed:

| Class | Exact scoped module | Required membership | Boundary |
| --- | --- | ---: | --- |
| Cheap unit | `bin/test_gaudi_prefilter.py` | 25 passed | In-process AST decisions plus pytest startup |
| Subprocess-heavy | `bin/test_sable_contained.py` | 44 passed | Real Git repositories and many short subprocesses |
| tmux | `bin/test_sable_worker_status_integration.py` | 23 passed | Real tmux servers, sockets, panes, clients, and process reads |
| Real bd/Dolt | `bin/test_footprint_lib_integration.py` | 5 passed | Isolated real bd stores backed by embedded Dolt plus real Git |

The catalog validator rejects broad `bin/`, the shell run set, and the sealed
verification command. A successful worker must report the exact catalog
membership. The deterministic mixed plant assigns workers 1 through 15 as:

```text
cheap, subprocess, cheap, tmux, subprocess,
cheap, real-bd, cheap, subprocess, tmux,
cheap, subprocess, real-bd, cheap, tmux
```

The isolated baseline used three workers with `--parallelism 1`, providing
three sequential observations without concurrent plant load. Representative
plants used 15 workers with parallelism 15. Runs requiring tmux were executed
on the real host because a filesystem sandbox that denies Unix socket creation
does not satisfy the benchmark contract.

## Isolated baselines

All values are seconds except percentages and memory. “PSI” is the increase in
Linux `some` pressure time for CPU/I/O/memory during the plant.

| Class | Result | Plant wall | Aggregate worker wall | Aggregate CPU | Worker median / max | Host CPU mean / peak | Minimum available memory | PSI CPU / I/O / memory |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Cheap unit | 3/3 pass | 3.44 | 3.33 | 3.20 | 1.11 / 1.16 | 13.0% / 22.1% | 14.98 GiB | 0.02 / 0.02 / 0.00 |
| Subprocess-heavy | 3/3 pass | 9.19 | 9.09 | 9.09 | 3.03 / 3.39 | 18.2% / 44.8% | 14.93 GiB | 0.16 / 0.08 / 0.00 |
| tmux | 3/3 pass | 155.66 | 155.55 | 156.51 | 51.26 / 54.26 | 18.0% / 61.2% | 14.76 GiB | 2.91 / 2.63 / 0.00 |
| Real bd/Dolt | 3/3 pass | 136.01 | 135.90 | 103.91 | 45.62 / 46.42 | 14.9% / 58.6% | 14.89 GiB | 2.32 / 4.91 / 0.00 |

Every isolated worker passed with zero logged collision signatures. The tmux
fixture left 69 attributable socket nodes in the shared `/tmp/tmux-1000`
directory, an independent cleanup defect discovered by the plant.

Per-worker latencies, in worker order:

```text
cheap:       1.11, 1.16, 1.06
subprocess:  2.67, 3.39, 3.03
tmux:       54.26, 50.03, 51.26
real-bd:    46.42, 45.62, 43.86
```

## Representative 15-worker baselines

| Class | Result | Plant wall | Aggregate worker wall | Aggregate CPU | Worker median / max | Host CPU mean / peak | Minimum available memory | PSI CPU / I/O / memory | Collisions / residue |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Cheap unit | 15/15 pass | 2.07 | 28.08 | 23.36 | 1.90 / 2.05 | 82.5% / 100% | 14.63 GiB | 0.36 / 0.03 / 0.00 | 0 / 0 |
| Subprocess-heavy | 15/15 pass | 6.83 | 92.00 | 65.20 | 6.20 / 6.77 | 84.3% / 100% | 14.43 GiB | 2.60 / 0.31 / 0.00 | 0 / 0 |
| tmux | 0/15 pass; 15 timeouts | 300.23 | 4,501.45 | unknown | 300.08 / 300.16 | 30.8% / 100% | 12.35 GiB | 13.25 / 5.76 / 3.06 | 0 / 140 |
| Real bd/Dolt | 15/15 pass | 174.30 | 2,592.78 | 1,190.07 | 173.05 / 174.29 | 68.8% / 100% | 13.08 GiB | 65.23 / 17.27 / 0.00 | 0 / 0 |

The timed-out tmux workers emitted no complete GNU `time` records, so their
aggregate CPU is unknown. Schema 1 printed zero for this case; schema 2 records
the missing value as unknown. The parent found 40 detached tmux-server or bash
descendants at report time. tmux had daemonized them out of each pytest process
group, so process-group termination alone was insufficient.

Per-worker latencies:

```text
cheap:
2.02, 1.96, 1.76, 1.75, 1.90, 1.90, 1.89, 2.05,
1.68, 1.98, 1.98, 1.87, 1.96, 1.64, 1.73

subprocess-heavy:
6.42, 6.20, 5.78, 6.20, 6.30, 6.75, 6.24, 6.29,
5.21, 6.01, 5.91, 6.73, 5.20, 5.99, 6.77

tmux:
300.03, 300.03, 300.05, 300.05, 300.05, 300.04, 300.06, 300.08,
300.13, 300.15, 300.15, 300.15, 300.16, 300.16, 300.15

real-bd/Dolt:
173.06, 172.48, 172.49, 172.06, 174.29, 173.05, 173.20, 171.94,
171.54, 173.81, 172.56, 173.08, 173.85, 173.12, 172.27
```

Cheap and subprocess-heavy work used the host rather than waiting on a shared
mutable resource: their plant walls stayed short while aggregate CPU and CPU
pressure rose. The real-bd workload had high aggregate work and pressure but
completed reliably because its fixture already creates isolated stores.
Only the tmux family crossed from slower into non-termination.

The clean mixed baseline passed all 15 workers:

| Plant wall | Aggregate worker wall | Aggregate CPU | Global median / max | Host CPU mean / peak | Minimum available memory | PSI CPU / I/O / memory | Collisions / residue |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 159.79 | 698.70 | 375.31 | 9.59 / 159.78 | 33.5% / 100% | 14.27 GiB | 8.16 / 5.25 / 0.00 | 0 / 69 |

Mixed baseline latency by class:

| Class | Workers | Median | Maximum |
| --- | ---: | ---: | ---: |
| Cheap unit | 6 | 3.39 | 4.20 |
| Subprocess-heavy | 4 | 9.64 | 9.73 |
| tmux | 3 | 158.65 | 159.78 |
| Real bd/Dolt | 2 | 85.79 | 87.35 |

Mixed per-worker latency:

```text
3.21, 9.73, 3.05, 159.78, 9.34, 4.06, 87.35, 4.20,
9.70, 158.65, 3.12, 9.59, 84.23, 3.57, 149.14
```

## Diagnosis

The hypotheses were ranked before changing behavior:

1. unbounded tmux and child-process churn;
2. contention or residue in the shared tmux socket directory;
3. kernel process, pseudo-terminal, or I/O pressure;
4. ordinary CPU scheduling;
5. an explicit socket-name or Dolt-lock collision.

The cheap and subprocess plants rejected ordinary CPU scheduling as the
primary cause. The logs contained no duplicate-session, lost-server,
resource-exhaustion, or Dolt-lock signature, rejecting hypothesis 5 as an
explanation visible at the error surface.

Two controlled interventions then separated resource residue from latency:

| Experiment | Result | Wall | Median / max | Aggregate CPU | Interpretation |
| --- | --- | ---: | ---: | ---: | --- |
| Private per-worker temp roots | 0/15 pass; 15 timeouts | 300.17 | 300.05 / 300.10 | unknown | Contained 183 socket nodes but did not fix execution |
| Four host-heavy slots | 15/15 pass | 462.07 | 110.84 / 159.43 active latency | 688.04 | Safe, but slower and still paid for the wrong boundary |

The harness itself also needed hardening after the first timeout. It now
captures plant descendants by immutable PID plus `/proc` start time, reports
them before cleanup, terminates only exact recorded identities, and proves none
remain. This is necessary because daemonized tmux servers escape a pytest
process group and later rewrite their environment, making a late environment
marker scan unreliable.

A diagnostic stack capture showed the tmux tests blocked while
`sable-worker-status` waited for `bd show`. Source inspection explained why:
generic tmux cases use intentionally nonexistent placeholder bead ids, but
their subprocess environment exposed the checkout's real `bd`. Every test
therefore queried one shared embedded Dolt store even though the module's
declared authority was tmux.

A causal shim that returned the production command's documented
unresolvable-bead result changed no tmux behavior and produced:

| Workers | Result | Wall | Median / max | Aggregate CPU |
| ---: | --- | ---: | ---: | ---: |
| 1 | 1/1 pass | 9.71 | 9.71 / 9.71 | 5.11 |
| 15 | 15/15 pass | 25.84 | 25.42 / 25.83 | 212.12 |

That single-variable intervention identified the accidental shared bd/Dolt
lookup as the genuinely contended boundary. Socket residue was real but not
the cause of the timeout, and broad host-heavy admission was treating a test
fixture defect as production scheduling policy.

## Implemented isolation

`bin/test_sable_worker_status_integration.py` now:

- prepends a tiny exit-1 `bd` shim for generic placeholder-bead cases,
  preserving the production fail-open result for an unresolvable bead;
- lets the two integration cases that require an open bead prepend their
  existing status-returning shim;
- creates a short private `TMUX_TMPDIR` for every test;
- kills the named tmux server and removes that private socket root in fixture
  teardown.

Authority remains intact:

- all 23 real-tmux integration tests remain in the module;
- open-bead cases still prove that an apparently done pane is downgraded;
- `bin/test_sable_worker_status.py` retains closed, open, unavailable, and
  confirmation decision coverage;
- `bin/test_footprint_lib_integration.py` retains the real bd/Dolt boundary;
- production `sable-worker-status` code and its fail-closed authority were not
  changed.

The measurement tool is also fail-closed. It owns every worker PID and status,
uses one stable log and digest per worker, requires an exact pytest summary and
GNU `time` record, treats missing data as malformed or unknown rather than
green, snapshots shared resources, and writes the report atomically without
overwriting existing evidence.

## Repeated acceptance

The post-fix isolated control ran three tmux workers sequentially:

| Result | Plant wall | Aggregate worker wall | Aggregate CPU | Median / max | Host CPU mean / peak | Minimum available memory | PSI CPU / I/O / memory | Collisions / residue |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 3/3 pass | 34.91 | 34.80 | 20.71 | 11.71 / 11.96 | 16.3% / 60.0% | 14.95 GiB | 0.58 / 0.24 / 0.00 | 0 / 0 |

Its worker latencies were `11.13, 11.96, 11.71` seconds.

Two clean, native, unconstrained 15-worker tmux repeats at `def231e` were:

| Run | Result | Plant wall | Aggregate worker wall | Aggregate CPU | Median / max | Host CPU mean / peak | Minimum available memory | PSI CPU / I/O / memory | Collisions / residue |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 15/15 pass | 29.65 | 431.87 | 247.75 | 28.91 / 29.65 | 85.7% / 100% | 14.43 GiB | 11.72 / 0.84 / 0.00 | 0 / 0 |
| 2 | 15/15 pass | 35.90 | 523.76 | 278.42 | 35.39 / 35.84 | 87.0% / 100% | 14.39 GiB | 16.80 / 1.34 / 0.00 | 0 / 0 |

These walls are at least 90.1% and 88.0% below the censored 300.23-second
timeout baseline. They show normal CPU contention relative to the 11.71-second
isolated median, but no shared-resource collapse, failure, timeout, socket
residue, or plant-owned process after cleanup.

Per-worker latency:

```text
tmux repeat 1:
27.12, 29.65, 28.31, 27.97, 28.47, 28.93, 29.59, 29.28,
28.97, 29.53, 28.56, 28.91, 28.70, 28.59, 29.30

tmux repeat 2:
34.12, 35.46, 34.32, 35.57, 33.43, 34.77, 33.01, 34.25,
35.82, 35.39, 35.48, 35.43, 35.11, 35.84, 35.78
```

Two clean, native, unconstrained mixed repeats were:

| Run | Result | Plant wall | Change from baseline | Aggregate worker wall | Aggregate CPU | Global median / max | Host CPU mean / peak | Minimum available memory | PSI CPU / I/O / memory | Collisions / residue |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 15/15 pass | 66.59 | -58.3% | 241.99 | 174.85 | 7.18 / 66.56 | 37.0% / 100% | 14.54 GiB | 4.84 / 3.25 / 0.00 | 0 / 0 |
| 2 | 15/15 pass | 53.75 | -66.4% | 204.59 | 151.99 | 6.82 / 53.74 | 35.6% / 100% | 14.55 GiB | 3.62 / 2.52 / 0.00 | 0 / 0 |

Class latency confirms that cheap work was not serialized:

| Run | Cheap median / max | Subprocess median / max | tmux median / max | Real bd/Dolt median / max |
| --- | ---: | ---: | ---: | ---: |
| 1 | 2.98 / 3.32 | 7.36 / 7.71 | 21.49 / 21.67 | 66.54 / 66.56 |
| 2 | 2.53 / 2.75 | 6.97 / 7.19 | 18.57 / 18.81 | 53.63 / 53.74 |

Mixed per-worker latency:

```text
repeat 1:
3.32, 7.71, 1.50, 21.67, 7.18, 2.88, 66.52, 1.50,
7.54, 20.75, 3.09, 7.00, 66.56, 3.29, 21.49

repeat 2:
1.34, 6.82, 2.55, 18.57, 7.19, 1.66, 53.74, 2.75,
6.81, 18.81, 2.65, 7.12, 53.52, 2.52, 18.54
```

## Diagnostic attribution and failure propagation

Two 15-worker negative controls prove that a plant cannot turn one bad child
green:

| Control | Parent exit | Report | Exact attribution | Other workers | Residue |
| --- | ---: | --- | --- | --- | ---: |
| `--plant-failure 7` | 1 | 14 pass, 1 failed | Worker 7, exit 17, `SABLE-X2R7G-PLANTED-FAILURE` in its stable log | 14 pass | 0 |
| `--plant-timeout 9 --worker-timeout 5` | 1 | 14 pass, 1 timeout | Worker 9 timeout | 14 pass | 0 |

The failure plant completed in 2.23 seconds with 26.04 aggregate worker-seconds
and 23.58 aggregate CPU-seconds. The timeout plant completed in 5.05 seconds
with 41.49 aggregate worker-seconds. Its aggregate CPU is correctly unknown
because the killed worker could not emit a complete resource record.

## Reproduction

Run from a clean checkout with the pinned test dependencies installed. tmux
plants must run where Unix socket creation is permitted.

```bash
# Three sequential observations for an isolated class.
bin/sable-test-contention \
  --scenario tmux \
  --workers 3 \
  --parallelism 1 \
  --report /tmp/sable-contention-tmux-isolated.json \
  --require-clean

# Homogeneous 15-worker plant.
bin/sable-test-contention \
  --scenario tmux \
  --workers 15 \
  --parallelism 15 \
  --report /tmp/sable-contention-tmux-15-r1.json \
  --require-clean

# Representative 6/4/3/2 cheap/subprocess/tmux/real-bd mixture.
bin/sable-test-contention \
  --scenario mixed \
  --workers 15 \
  --parallelism 15 \
  --report /tmp/sable-contention-mixed-15-r1.json \
  --require-clean
```

Use new report names for repeats; the runner refuses to overwrite a report or
artifact directory. Negative controls intentionally return exit 1:

```bash
bin/sable-test-contention \
  --scenario cheap-unit \
  --workers 15 \
  --plant-failure 7 \
  --report /tmp/sable-contention-failure.json \
  --require-clean

bin/sable-test-contention \
  --scenario cheap-unit \
  --workers 15 \
  --plant-timeout 9 \
  --worker-timeout 5 \
  --report /tmp/sable-contention-timeout.json \
  --require-clean
```

Raw JSON and worker logs are intentionally not committed: they contain large,
host-specific process and procfs samples. The tables and complete latency
vectors above are the durable record; the sealed runner regenerates the raw
evidence and preserves exact per-worker logs, hashes, resource records, and
verdict reasons.

## Reusable decision

Classify a workload by what it actually touches, not by the test module's name.
Isolate an accidental shared boundary before adding scheduling policy. A
private temp directory can contain residue without curing contention, and the
absence of a lock-error string does not prove that a shared database is cheap.
Only after boundary isolation should a plant justify admission control.

For SABLE, normal OS scheduling is sufficient for isolated scoped workers on
this 14-core host. The reusable protection is test-owned resource isolation
and attributable cleanup, not a universal semaphore. If a future real
bd/tmux/Docker boundary still collapses after its state and names are isolated,
the same harness can measure a configurable `--heavy-slots` width without
serializing ordinary work.
