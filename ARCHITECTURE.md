# Architecture

## Shape

```
                        ┌─────────────────────────────────────────┐
   prospect's public    │  research agent                         │
   pages ──────────────►│  fetch → extract → typed facts + source │
                        └────────────────┬────────────────────────┘
                                         │ enrichment
                                         ▼
   prospect ◄── WebRTC ──►  closer agent  ──── transcript, outcome ───┐
                            (STT→LLM→TTS→avatar, 800ms budget)        │
                                                                      ▼
   rep's laptop                                              ┌────────────────┐
   ┌──────────────────────────────┐      ops (WS or POST)    │  op log        │
   │  console                     │◄───────────────────────► │  append-only   │
   │  CRDT replica ── IndexedDB   │      ops (fan-out)       │  SQLite WAL    │
   │  outbox ── service worker    │                          └────────────────┘
   └──────────────────────────────┘                                  │
        writes land here FIRST, always                     read-only materialisation
                                                                     ▼
                                                            /api/deals (integrations)
```

The load-bearing arrow is the one from the console to IndexedDB. Every other arrow is allowed
to fail.

## Layering

```
packages/crdt/           the merge semantics. No I/O, no framework, no network.
  clock.ts               hybrid logical clocks, drift bound, total order
  types.ts               the op vocabulary — every type designed for commutativity
  document.ts            Replica: LWW registers, OR-Set tags, RGA text
  scripts/fixtures.ts    generates the TS→Python agreement fixtures

apps/console/            React 18. Renders a replica; never fetches to read.
  lib/store.ts           IndexedDB + outbox + reconnect + WS/HTTP transport
  lib/useStore.ts        useSyncExternalStore bindings
  components/            board, drawer, research, call, sync badge
  public/sw.js           app shell cache — the thing that makes a cold offline start work

services/api/
  research/              fetch (policy) → extract (deterministic) → schema (provenance)
  sync/                  oplog (durable) + hub (relay, backpressure)
  calls/                 turn loop, latency budget, disclosure
  crm/                   read-only materialisation
  app.py                 the three HTTP surfaces
```

---

## Decisions, and what was rejected

### A hand-rolled CRDT rather than Yjs or Automerge

**Chosen because** the merge semantics *are* the product's hard part, and this build exists to
demonstrate them. Yjs is the right production answer and would be the recommendation on a real
team; here it would hide exactly the reasoning worth showing. It is also ~40kb for an app whose
premise is a bad connection.

**Rejected: Automerge.** Excellent, and a much larger wasm payload for a document model this
small.

The scope is deliberately narrow — four CRDT types, not a general JSON-CRDT. Everything the
console needs is a register, a set, or a text sequence.

### The server does not merge

**Chosen because** it is what makes offline the *same* code path as online. If the server
merged, the console would need a second reconciliation path for the offline case, and a special
case is where divergence lives. It also keeps the server small enough to reason about: append,
dedup, order, relay.

**Rejected: server-authoritative with optimistic UI.** The conventional answer, and it means
every write has a rollback path and the UI must model "probably saved". A rep in a tunnel
should not be looking at maybe-saved state.

### Sequence numbers, not vector clocks

**Chosen because** the CRDT already handles concurrency. The server only has to answer *what
have I not seen*, and a monotonic per-workspace counter answers that in one integer and one
indexed range scan.

**Rejected: vector clocks.** They grow with device count and must be reconciled on every
reconnect, buying nothing the CRDT does not already provide.

### Bounded per-client queues

A client on a bad connection cannot keep up with a busy workspace. Its queue is capped; on
overflow the connection is closed with a resync hint. Unbounded buffers are how one slow phone
exhausts server memory, and reconnect-and-replay is cheap precisely because the log is sequenced.

### Durable before broadcast

Appending first costs one fsync of latency and makes "acknowledged" mean "durable". Broadcasting
first would let a client observe an op that a crash then loses — the one inconsistency a
log-backed system must never produce.

### SQLite, not Postgres

The op log is an append-only table with one unique index and no joins; the bottleneck is fsync,
not query planning. WAL gives concurrent readers alongside the single writer, which is the actual
access pattern. Nothing uses SQLite specifics, so Postgres later is a driver change.

### The research agent has a fixed plan

**Chosen because** an LLM with a fetch tool has no bound on what it reads, no reproducibility,
and no way to explain a wrong answer. A fixed plan over high-yield paths plus deterministic
extraction means the same domain twice produces byte-identical output and every field traces to
a URL.

**Rejected: agentic crawling.** More impressive in a demo, unusable in a sales conversation
where being confidently wrong costs the deal.

### `INFERRED` requires evidence, at the type level

A model-derived value that carries no excerpt cannot be constructed. Enforced in the schema
rather than in review, because the rule is worthless if a caller can forget it.

### The API materialiser does not implement RGA

It returns note *lengths*, not merged text. A second sequence CRDT is where drift would be worst
and the API has no use for merged prose. If it ever does, the right move is to run the
TypeScript replica server-side — not to port RGA into Python.

---

## Bugs this design caught

**1. Cancellation-before-creation, twice.** A tag removal arriving before its add, and a
character delete arriving before its insert, were both silently dropped — so the removed thing
came back, on one replica only. Found by the convergence property test; the shrunk
counterexample was two operations long. Both now record the cancellation unconditionally.

**2. Non-determinism from `hash(qid)`.** Python randomises string hashing per process, so the
research agent's per-query seed changed every run. Any experiment seeded off a builtin string
hash is unreproducible by construction.

**3. A validator that never ran.** `@field_validator("excerpt")` is skipped by Pydantic when the
field is simply omitted — which is precisely the case the rule existed to catch. The one
guarantee keeping the model honest silently did not apply. Now a `model_validator`.

**4. Leaked per-run state.** `PolitePool.skipped` accumulated across calls, so researching the
same domain twice reported every skip twice and the agent was not idempotent.

**5. No offline cold start.** The screenshot script tried to reload with the network severed and
got `ERR_INTERNET_DISCONNECTED` — proving the app could not boot offline despite all the local
data being present. Fixed with a service worker; the shot is now in the README as evidence.

---

## Not built

- **Auth.** Actor identity is a per-browser id. A real deployment needs workspace membership
  enforced at the relay, which is the only place it can be enforced.
- **Log compaction.** The op log grows forever. A production system needs periodic snapshotting
  with tombstone GC — which for RGA means proving no live insert anchors to a collected char.
- **Real WebRTC media.** The call pipeline's interfaces and budget accounting are real; the
  transport is not wired to LiveKit here.
- **The realtime avatar.** Adapter code exists; the GPU service does not. Stated in the README
  rather than implied.
