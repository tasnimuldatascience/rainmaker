# Architecture

## Shape

```
                        ┌─────────────────────────────────────────┐
   prospect's public    │  research agent                         │
   pages ──────────────►│  fetch → extract → typed facts + source │
                        └────────────────┬────────────────────────┘
                                         │ enrichment
                                         ▼
   prospect ◄── WebSocket ►  closer agent  ─── transcript, outcome ───┐
                            (text/speech in, Qwen → Kokoro out)       │
                                    │                                 │
                     ┌──────────────┼──────────────┐                  │
                     ▼              ▼              ▼                  │
              tour: a browser   quote: arith-   payments: a hosted    │
              on the SELLER's   metic over      checkout. No card     │
              own pages         published rates ever reaches us       │
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
  public/sw.js           app shell cache — the thing that makes a cold start work unconnected

services/api/
  research/              fetch (policy) → extract (deterministic) → schema (provenance)
  sync/                  oplog (durable) + hub (relay, backpressure)
  agents/                specs, versions, publishing; quoting.py does the arithmetic
  calls/                 turn loop, latency budget, disclosure, admission, the agenda
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

**Chosen because** it makes a disconnected client the *same* code path as a connected one. If
the server merged, the console would need a second reconciliation path for that case, and a special
case is where divergence lives. It also keeps the server small enough to reason about: append,
dedup, order, relay.

**Rejected: server-authoritative with optimistic UI.** The conventional answer, and it means
every write has a rollback path and the UI must model "probably saved". A rep in a tunnel
should not be looking at maybe-saved state.

### Membership is checked at the relay, because nowhere else can check it

**Chosen because** it is the only component both replicas trust. The server merges nothing and
decides nothing about content — but every op crosses it, and a client that decides whether it is
allowed to write has decided nothing at all.

What replaced what: actor identity used to be `?actor=dana` in the socket's query string. That
is not a claim about who you are, it is a claim you would like to make, and anyone able to open
a socket could open it as anybody, into any workspace, with the op log attributing their writes
accordingly. Now a signed grant names one actor in one workspace, and four things follow from
it:

| | |
|---|---|
| **Identity** | The token body is signed, so editing the actor invalidates the signature instead of changing who you are |
| **Membership** | The `(workspace, actor)` pair must exist. A grant for one workspace does not open another |
| **Attribution** | Every op's actor must match the grant's |
| **Revocation** | Membership is a row, and `verify` reads it every time rather than trusting the signature — so removing a member takes effect on their next use rather than when their token expires |

**Attribution is a correctness property, not an audit trail.** The CRDT breaks
hybrid-logical-clock ties by actor id. An op wearing somebody else's actor does not merely
misattribute an edit; it decides which of two concurrent edits wins. That places this check
inside the merge's correctness rather than beside it.

**Rejected: a session table.** The relay is the hot path and a signature check is a hash rather
than a query. The cost is that a token cannot be invalidated before it expires — which is
exactly why membership is still read from the table on every use, so revocation does not have to
wait for one.

**Enrolment is open, and that is a policy rather than an oversight.** `POST /api/sync/token`
grants a token to whoever asks, because this deployment has no sign-in screen. That is one
handler to put an identity provider in front of, and it is the cheap half. The enforcement
underneath it is the half that is expensive to retrofit, and it is real. A test pins the open
enrolment as a decision, so that adding an IdP later breaks something that says what the old
behaviour was rather than silently changing what the system means.

### The log is pruned, not snapshotted

**Chosen because** a snapshot would require the server to materialise state, and the server does
not implement RGA — deliberately, because a second sequence CRDT in a second language is where
drift would be worst. Compaction instead deletes ops that provably cannot change the outcome,
which needs no merge and leaves the remaining log replayable by exactly the code that replayed
the full one.

**A watermark decides when, and it is the entire safety argument.** An op may be dropped only
once *every* replica has acknowledged it. If replica R wrote an insert and another replica
deleted that character, dropping both while R is behind leaves the character visible on R
forever — divergence that no later op repairs. So the watermark is the minimum acknowledged
sequence across known replicas, and an empty registry means *compact nothing* rather than
"everyone is up to date". The acknowledgement is the `?since=` the console already sends, which
is why this needed no client release — and it is trustworthy only because that socket is now
authenticated.

**Then three rules, one per merge type:**

| | |
|---|---|
| **LWW register** | Keep the hybrid-logical-clock winner per field; drop the losers |
| **OR-set** | An add and the remove that annihilates it can both go |
| **RGA** | An insert and delete for a dead character can go — **unless a living character anchors to it**, in which case the insert stays as a tombstone anchor. Drop it and the survivor's `after` dangles, and it orphans on replay |

**And a fourth rule that only appears once the first three are correct together.** Each rule is
individually sound, and applied together they can empty an entity completely: a deal whose whole
history is one tag added and removed, or one note typed and then deleted, compacts to nothing —
and an entity nothing mentions is an entity nobody created. It leaves the board. So compaction
retains one *witness* pair per entity and per text field, chosen at the head of the document so
the witness cannot itself be orphaned. It costs about two ops per text field, permanently, and
without it the property test's exact-equality assertion is simply false.

**Proved rather than argued.** 200 randomised logs at random watermarks assert that materialising
the compacted log equals materialising the full one, exactly; a structural check asserts no
surviving insert names an anchor that is gone. RGA cannot be checked in Python, so the fixture
bridge runs the other way from `crdt_agreement.json`: the real compactor emits cases, and the
real TypeScript replica asserts the full and compacted logs render identical text with no
orphans, under shuffled delivery. Disabling anchor retention fails that; dropping every removal
below the watermark fails the property test.

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

### The voice runs on the CPU, and that is where the latency budget goes

Kokoro-82M synthesises at 1.7–2.9x realtime on CPU, but the number that matters is its FIXED
cost: ~340ms per call on a sixteen-core laptop, almost independent of how short the text is
(3 characters cost 386ms, 13 cost 444ms). So the floor on "silence after the prospect stops" is
about 380ms no matter how the reply is chunked, and the measured median is 724ms — the floor
plus the wait for enough tokens to cut a first chunk from.

Chunking still pays for itself many times over: the same 175-character reply is 3007ms to first
sound synthesised whole and 407ms synthesised first-clause-first. What chunking cannot do is
beat the fixed cost.

**Rejected: moving synthesis to the GPU.** `onnxruntime-gpu` would likely take the fixed term
down substantially and is the obvious next step. It is another dependency, another install path,
and another way for a fresh clone to fail — and the rule here is that a clone runs. The turn
budget is missed by roughly 100ms on typed input as a result, and the console displays that
rather than hiding it.

**Rejected: server-side transcription.** faster-whisper would keep audio on the machine, which
fits everything else about this design, and it would compete with Kokoro for the same cores on
the one path where latency is visible. The browser's recogniser is free, streams partials, and
leaves the CPU to the two models that need it. The cost is real and stated in the console:
Chrome sends microphone audio to Google. Typing is the path that does not.

### The model may not state a figure, and it is a filter rather than a prompt

Rejected: telling the model not to. That is what the prompt already said, and it is what every
guide on grounding recommends, and it does not work — asked what capacity was available, a
1.5B model answered "starting from $50 per GPU per hour" about a product that charges $2.40.

Rejected: refusing to synthesise a whole turn containing a figure. The buyer then hears silence
where an answer should be, and the failure is invisible to everyone except them.

Taken: a streaming filter on the model's tokens only, replacing a figure before it can reach
synthesis. The platform's own computed sentences — the quote, the checkout, the times offered —
never pass through it, which is the actual invariant: **the platform may state a number it
worked out, and the model may not state one at all.**

The cost is a hold on the token stream, and it is scoped to earn it: text waits only while it
could still be becoming a figure. A turn with no numbers is not delayed at all, and "sixty-four
nodes are free" is delayed by one word. A fixed window would have been four lines shorter and
would have stalled every sentence containing a digit — on an agent whose job includes reading
out live capacity.

### The amount comes from the quote, and the agent never sees a card

An agent that invents a meeting wastes a slot. An agent that invents an amount takes somebody's
money, and there is no version of that which an apology fixes.

So the amount handed to the payment server is arithmetic over the tenant's published rates, and
the checkout is hosted: the buyer enters their card on the processor's page, on the processor's
domain. Nothing here, nothing in the model's context and nothing in a transcript ever contains a
card number, which keeps the product out of PCI scope rather than in it and managed. The tools
have no parameter that could carry one, and a test asserts that.

Rejected: a stub for the payment step. A payment step nobody can click through is a payment step
nobody has debugged, so the default provider is a mock that persists real intents, enforces the
same invariants, and serves a real page — and is what the tests run against. A key swaps it for
Stripe, and `mark_paid` then refuses outright, because there the processor's webhook is the only
thing allowed to say a checkout was paid.

There is a ceiling above which the agent will not charge at all; it reports and offers a person.
Not a technical limit — an agent that can raise an unbounded charge is a headline.

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

**5. No cold start when disconnected.** The screenshot script tried to reload with the network
severed and got `ERR_INTERNET_DISCONNECTED` — proving the app could not boot despite all the cached
data being present. Fixed with a service worker; the shot is now in the README as evidence.

**6. A price the model invented, said out loud.** Found by driving a real call rather than by a
test: asked what capacity was available, the model quoted $50 per GPU-hour for a product priced
at $2.40. Every other part of the design was already correct, and the prompt already forbade it.
Nothing was checking, and a rule the model is asked to follow is a request. See the filter above.

**7. A demo agent frozen at whatever was seeded first.** `seed` returned early whenever any
version was live, so a change to the agent's tour or competitors never reached the running one.
The symptom was not an error: the comparison step fell through to the model and the tour was
empty, silently, on a database nobody thought to look at. Idempotent is not the same as inert.

**8. The word "seat", compiled in.** The quote read out "for 40 seats" because the platform had
only ever had one tenant, who sold seats. Discovered by configuring a second one that sells
GPU-hours — which is the entire reason to keep a second tenant that is not a reskin of the
first.

---

## Decided against, and what it costs

These are trade-offs rather than gaps: each was reachable, each was measured, and the price of
declining it is stated in the product rather than hidden.

- **GPU synthesis.** `onnxruntime-gpu` would take Kokoro's ~340ms fixed cost down substantially
  and is the obvious next step for latency. It is also another dependency, another install path
  and another way for a fresh clone to fail, and the rule here is that a clone runs. **The price:
  the turn budget is missed by roughly 100ms on typed input, and the console displays that.**
- **Server-side transcription.** faster-whisper would keep audio on the machine, and would
  compete with Kokoro for the same cores on the one path where latency is visible. **The price:
  Chrome sends microphone audio to Google, which the console says out loud; typing does not.**

## Not built

- **An identity provider, and closing live sockets on revoke.** Membership itself is now
  enforced at the relay: a signed grant names one actor in one workspace, it is checked against
  the membership table on every socket and every flush rather than trusted on its signature
  alone — so revoking a member takes effect on their next use, not on their token's expiry —
  and every op must be attributed to the actor the grant names, because the CRDT breaks
  concurrent-edit ties by actor id and forging an actor forges the outcome of a merge rather
  than merely its byline. What is missing is anything deciding *who* gets a grant:
  `POST /api/sync/token` enrols whoever asks, because there is no sign-in screen to ask them.
  That is one handler to put an identity provider in front of, and the enforcement below it —
  the expensive half to retrofit — is already real. An open socket also keeps streaming until
  it reconnects; revocation should hang up.
- **An SFU, and the media transport that comes with one.** Audio runs over the call WebSocket as
  base64 WAV per clause. Three of the four things usually cited to justify WebRTC here are
  built: clips are scheduled ahead on the Web Audio clock, in arrival order, which is a playout
  buffer; the socket has a heartbeat and reconnects with bounded backoff, keeping the transcript
  across a drop; and the microphone is opened with echo cancellation, noise suppression and gain
  control asked for explicitly. The transport is the part that is not. TCP retransmits rather
  than conceals, so a lossy link grows latency instead of degrading audio, and there is no FEC,
  no partial-frame codec and no adaptive buffer depth to trade against it. Two further limits
  are real and worth naming: the reconnect restores the socket, not the server's `CallSession`,
  so the agent comes back without the conversation; and a second human on the call — the rep a
  handoff exists for — needs an SFU, which is a service a fresh clone would have to run.
- **A resync signal for a replica that was gone too long.** Compaction evicts a replica unseen
  for the horizon (a fortnight by default) so one dead device cannot stop the log being pruned
  forever. A replica that returns after that may find ops it never saw already gone. Its recovery
  is a full replay from `since=0`, which is correct — the compacted log materialises identically
  — but nothing tells the console to do it, so it would sit on a checkpoint the server can no
  longer honour.
- **A commercially licensed talking head.** The face lip-syncs, on the local GPU, via Wav2Lip —
  whose weights are academic and personal use only. Every other dependency here is permissive.
  A hosted provider behind the same interface is the commercial path and is not wired up.
- **A head that moves.** Wav2Lip regenerates a mouth on a fixed photograph; the head itself never
  turns, nods or blinks. That is the difference between this and a video avatar, and closing it
  needs a driving video (LivePortrait) rather than a still.
