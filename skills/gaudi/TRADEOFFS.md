# System Design Tradeoffs

When Gaudi runs `--epic` mode and finds an implementation bead whose
description doesn't name the load-bearing tradeoff, it surfaces a finding and
walks the user through the choice. This file is the reference catalog —
twelve recurring tradeoffs Gaudi looks for, each explained in plain language.

The list is adapted from the *System Design Tradeoffs* section of
[ashishps1/awesome-system-design-resources](https://github.com/ashishps1/awesome-system-design-resources)
plus a few additions that come up often in SABLE work.

> **Pedagogical rule:** when naming a tradeoff to the user for the first time
> in a session, lead with the plain-language definition of each side, then ask
> the question. Don't say "is this push or pull?" — say "two ways to do this:
> *push* means we send updates the moment they happen; *pull* means consumers
> ask for updates when they're ready. Push trades read-cost for write-cost.
> For your case [...], which fits?"
>
> When the user is uncertain, give them the **safe default** for their
> situation and explain why. Don't leave them stuck on a binary they don't
> have the experience to call.

---

## 1. Strong vs Eventual Consistency

**Strong consistency**: every reader sees every write the instant it's
committed. Equivalent to "there is one source of truth and everyone reads from
it." Examples: a single-node database, a synchronous-replicated quorum write.

**Eventual consistency**: writes propagate to readers over time —
milliseconds to seconds, sometimes longer. Two readers asking at the same
moment may see different states. Examples: DNS, most NoSQL stores by default,
CDN-cached content.

**Costs**: strong consistency costs latency under load (every write waits for
acknowledgment from N replicas) and availability under partition (if the
quorum can't be reached, writes fail). Eventual consistency costs *correctness
windows* — readers can act on stale data and make decisions that need to be
reconciled later.

**When each fits**: strong for money, inventory, auth state, and anywhere two
readers seeing different things would cause real harm. Eventual for feeds,
counters, search indexes, analytics, and anywhere "approximately right" is
fine.

**Safe default if uncertain**: strong consistency for anything where
correctness matters more than scale. Most apps never grow to the point where
eventual consistency is required — it's an optimization, not a default.

---

## 2. Push vs Pull Architecture

**Push**: producers send data to consumers as soon as it's available. Examples:
WebSocket broadcasts, webhooks, fan-out-on-write social feeds, server-sent
events.

**Pull**: consumers fetch data when they want it. Examples: polling APIs,
cron-driven syncs, fan-out-on-read social feeds.

**Costs**: push optimizes reads (consumers don't query — data arrives) at the
cost of write expense (one event → N consumer notifications) and the cost of
maintaining live connections. Pull optimizes writes (producer writes once) at
the cost of read latency and wasted "nothing changed" queries.

**When each fits**: push when subscribers are many but events are rare, or
when freshness is critical (live chat, market data, alerts). Pull when events
are frequent but few consumers care (logs, metrics, batch jobs), or when
consumers can't accept inbound connections (mobile apps with intermittent
network).

**Hybrid**: long polling and server-sent events sit in the middle — pull
shaped to feel like push without permanent connections.

**Safe default if uncertain**: pull. It's simpler, more robust to consumer
failure, and doesn't require connection-state management. Switch to push only
when latency requirements force it.

---

## 3. Vertical vs Horizontal Scaling

**Vertical scaling**: make the single machine bigger — more CPU, more RAM,
faster disk. Sometimes called "scale up."

**Horizontal scaling**: add more machines and distribute work across them.
Sometimes called "scale out."

**Costs**: vertical scaling hits a hard ceiling (the biggest machine money can
buy) and forces planned downtime for upgrades. Horizontal scaling forces you
to design for partial failure (one node going down doesn't take the whole
system), distributed state, and inter-node coordination — which adds
operational complexity and bugs.

**When each fits**: vertical when traffic is predictable and modest, when
state is hard to shard (graph databases, big in-memory caches), or when the
operational cost of horizontal isn't justified. Horizontal when traffic is
unbounded, when you need geographic distribution, or when fault tolerance
requires multiple nodes anyway.

**Safe default if uncertain**: vertical, until you have a reason. Most apps
fit on a single big server for far longer than the architecture-blog
discourse suggests.

---

## 4. Synchronous vs Asynchronous Communication

**Synchronous**: caller waits for the callee to finish before continuing.
Examples: a function call, an HTTP request with a blocking client.

**Asynchronous**: caller fires the work off and continues without waiting.
The result (if any) comes back later via callback, promise, queue, or event.

**Costs**: synchronous is simpler — call site has the result immediately, and
errors propagate naturally. But the caller is coupled to the callee's latency
and availability; slow downstream → slow caller → cascading failure.
Asynchronous decouples the two but introduces queue depth, eventual delivery,
retry logic, and harder-to-trace error paths.

**When each fits**: synchronous for fast, reliable operations where the
caller needs the result to proceed (validation, authorization, cache reads).
Asynchronous for slow, unreliable, or fan-out operations where the caller
doesn't need to wait (sending email, generating reports, indexing).

**Safe default if uncertain**: synchronous. The complexity of async-everywhere
is significant; only reach for it when you have a real reason (slow
downstream, fan-out, decoupled deploys).

---

## 5. Batch vs Stream Processing

**Batch**: collect inputs over a window (an hour, a day), then process the
whole window at once. Examples: nightly ETL jobs, daily reports, end-of-day
reconciliation.

**Stream**: process each input as it arrives. Examples: real-time analytics,
fraud detection, IoT sensor processing.

**Costs**: batch is operationally simpler — failures retry the whole batch,
state is checkpointed at window boundaries. But results are stale by up to
the window length. Stream is fresh but operationally harder — backpressure,
exactly-once delivery, late-arriving data, windowing semantics all become
real problems.

**When each fits**: batch when staleness up to the window is fine and the
operation needs to see all the data (aggregations, joins, ML training).
Stream when freshness matters (alerts, dashboards, anomaly detection) or when
the data volume is unbounded and can't be held in memory.

**Hybrid (lambda/kappa architectures)**: many systems run both — stream for
the current window, batch for the historical correction.

**Safe default if uncertain**: batch. Simpler operations, simpler debugging.
Move to stream only when staleness is a real product problem.

---

## 6. Concurrency vs Parallelism

**Concurrency**: structuring a program so multiple tasks make progress over
time — interleaved on a single thread, or across threads, or across machines.
Tasks can wait for each other.

**Parallelism**: actually running tasks simultaneously on multiple cores or
machines. A special case of concurrency.

**Costs**: concurrent code (especially shared-memory threading) is famously
hard — race conditions, deadlocks, atomicity violations. Parallel code adds
coordination overhead and may not scale linearly with cores (Amdahl's law).
Single-threaded code is easy but caps at one core's worth of work.

**When each fits**: concurrent for IO-bound workloads (the program spends
most of its time waiting — DB, network, disk). Parallel for CPU-bound
workloads (the program is doing real computation — image processing, ML
inference, simulation).

**Safe default if uncertain**: single-threaded with async IO. Modern runtimes
(Node, Python asyncio, Go's goroutines, Rust's tokio) get you 80% of the
benefit with 10% of the complexity. Reach for true parallelism (worker pools,
multiprocessing) only when profiling shows you're CPU-bound.

---

## 7. Long Polling vs WebSockets vs Server-Sent Events

**Long polling**: client sends a request, server holds it open until it has
data (or times out), then responds. Client immediately sends the next
request. Looks like push, costs like pull.

**WebSockets**: a persistent bidirectional connection. Both sides can send at
any time.

**Server-Sent Events (SSE)**: a persistent server-to-client stream. Client
can't push; server sends events when they happen.

**Costs**: long polling is the most compatible (works through any HTTP proxy)
but inefficient (connection overhead per message). WebSockets are most
efficient and bidirectional but require infrastructure that handles persistent
connections (load balancer config, sticky sessions, scaling). SSE is the
middle ground — efficient one-way, plain HTTP, but unidirectional.

**When each fits**: long polling when you control nothing in the network path
and updates are rare. WebSockets for bidirectional real-time (chat, games,
collaborative editing). SSE for server-to-client streams (notifications, live
dashboards, AI streaming).

**Safe default if uncertain**: SSE if you only need server-to-client.
WebSockets only when you need true bidirectional. Long polling almost never
in 2026 — it's a last-resort compatibility option.

---

## 8. REST vs RPC

**REST**: resource-oriented HTTP — nouns are URLs, verbs are HTTP methods,
state transitions happen via standard verbs. Heavy on conventions and
discoverability.

**RPC**: function-call-oriented — clients invoke named procedures on the
server. Examples: gRPC, JSON-RPC, custom POST-based RPC.

**Costs**: REST is uniform and cache-friendly but maps poorly to operations
that aren't CRUD (workflows, computations, bulk operations end up as
awkward POSTs). RPC fits any operation naturally but loses HTTP-layer
features (caching, intermediate proxies, browser inspection).

**Note on GraphQL**: a third option, query-language-oriented — clients
specify exactly the fields they want. Solves over-fetching but adds caching
complexity and an N+1 footgun at the resolver layer.

**When each fits**: REST when your domain is genuinely resource-shaped
(public APIs, simple CRUD). RPC when operations don't map to CRUD
(workflows, batch jobs, streaming) or when you want strong typing and codegen
across languages. GraphQL when clients are heterogeneous and over-fetching
hurts (mobile apps, frontends with diverse view shapes).

**Safe default if uncertain**: REST for public-facing APIs (discoverability,
caching). RPC (gRPC) for internal service-to-service.

---

## 9. Read-Through vs Write-Through Cache

**Read-through**: on a cache miss, the cache fetches from the source, stores
it, and returns it to the caller. Callers always read from cache.

**Write-through**: on a write, the cache updates the source and the cache
entry in the same operation. Cache and source stay in sync.

**Other patterns to know**:
- **Cache-aside (lazy loading)**: caller checks cache, misses → fetches from
  source → populates cache. Most common pattern.
- **Write-behind (write-back)**: cache acknowledges the write immediately,
  flushes to source asynchronously. Fastest writes, risk of data loss.
- **Refresh-ahead**: cache proactively refreshes hot entries before they
  expire.

**Costs**: read-through hides cache logic from the caller but couples cache
and source. Write-through guarantees freshness but doubles every write's
latency. Cache-aside is flexible but every caller has to remember the
miss-fill dance. Write-behind risks losing recent writes if the cache dies
before the flush.

**When each fits**: read-through or cache-aside for read-heavy workloads
(catalogs, product pages). Write-through when staleness is unacceptable
(inventory, balances). Write-behind only when write throughput is critical
and durability is handled elsewhere.

**Safe default if uncertain**: cache-aside with TTL. Most apps land here.
Move to write-through only when stale-read bugs are a real product problem.

---

## 10. Stateful vs Stateless Design

**Stateful**: the service remembers something between requests — session
data, in-memory state, open connections.

**Stateless**: every request carries everything needed to serve it. The
service holds no per-client state.

**Costs**: stateful is easier to program (especially for long-running
workflows) but couples scaling to state (you can't load-balance freely;
restarts lose state; sharding is required at scale). Stateless is harder
upfront (state must live somewhere — DB, cache, JWT) but trivial to scale
horizontally and survive restarts.

**When each fits**: stateful for genuinely long-running connections (chat,
games, streaming) or when state is too large to externalize cheaply (search
indexes, ML models). Stateless for almost everything else, especially
HTTP request/response services.

**Safe default if uncertain**: stateless. The operational simplicity is
enormous. Externalize state to a database or cache and treat each request as
self-contained.

---

## 11. Latency vs Throughput

**Latency**: how long a single request takes from start to finish. Measured
in milliseconds, usually as a percentile (p50, p99).

**Throughput**: how many requests the system can handle per unit time.
Measured in requests/sec.

**The tradeoff**: optimizing one often costs the other. Batching improves
throughput (amortize per-request overhead) but worsens latency (wait for the
batch to fill). Caching can improve both — but adds tail latency on misses.
Parallelism can improve throughput but also tail latency from queueing.
Connection pooling improves throughput, can hurt latency at pool exhaustion.

**When each fits**: latency-bias for interactive systems (web pages, APIs,
chat) — users notice tail latency. Throughput-bias for batch systems,
analytics, ML training, ingestion pipelines — total time matters more than
any single request.

**Most apps optimize the wrong one**: tail latency is what users actually
experience, but throughput is what's easy to measure. p99 is more often the
constraint than requests/sec.

**Safe default if uncertain**: optimize for p99 latency in user-facing paths;
optimize for throughput in offline paths.

---

## 12. Normalization vs Denormalization (data modeling)

**Normalized**: data is stored once, in the most general form, with
relationships expressed via foreign keys. Standard relational practice
(3NF, BCNF).

**Denormalized**: data is duplicated across rows or tables to make reads
cheaper. Common in analytics tables, NoSQL document stores, and read-side
projections in event-sourced systems.

**Costs**: normalized data is consistent by construction (one place to update)
but expensive to read at scale (joins, aggregations). Denormalized data is
fast to read but every write has to update multiple copies — and missing one
creates inconsistent state.

**When each fits**: normalized for transactional systems (OLTP) where writes
matter and reads can afford joins. Denormalized for read-heavy systems
(analytics, search, feeds) where the join cost would dominate, or for NoSQL
stores that don't support joins efficiently.

**Hybrid**: many systems normalize the source of truth and project
denormalized views for specific read paths.

**Safe default if uncertain**: start normalized. Denormalize specific read
paths once they prove to be hot. Premature denormalization creates write
amplification and inconsistency bugs that are hard to undo.

---

## How Gaudi uses this catalog

**Epic-mode interview (Phase E3-E4)**: when classifying an implementation
bead's feature shape, Gaudi checks which of these tradeoffs are load-bearing
for that shape. If a tradeoff is load-bearing but the bead description
doesn't name a position on it, Gaudi files a `[TRADEOFF-UNSPECIFIED]` finding.

**Pedagogical cadence**: when surfacing a tradeoff to the user, Gaudi names
both sides, explains the cost of each, and offers the safe default if the
user is uncertain. The goal is not to force the user to know — it's to make
sure the decision is *made consciously* and recorded on the bead so the
worker who implements it knows which side they're building for.

**Bead output**: when a tradeoff is resolved, the bead's `## Tradeoffs
locked` section names the choice and the one-sentence reason. Example:

> **Push vs pull**: pull (cache-aside with 5min TTL). The reader frequency
> is ~10x the writer frequency, and 5-minute staleness is acceptable per
> product.

Workers implementing the bead see the locked tradeoff and don't have to
re-derive it from scratch.
