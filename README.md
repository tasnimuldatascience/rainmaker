<div align="center">

# Rainmaker

**An AI sales agent platform with a rep console that works with the network off.**<br>
Research agent · live closer · local-first CRDT sync

[![ci](https://github.com/tasnimuldatascience/rainmaker/actions/workflows/ci.yml/badge.svg)](https://github.com/tasnimuldatascience/rainmaker/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)](services/api/pyproject.toml)
[![typescript](https://img.shields.io/badge/typescript-5.6-3178c6?logo=typescript&logoColor=white)](packages/crdt)
[![tests](https://img.shields.io/badge/tests-78%20passing-22863a)](#tests)
[![license](https://img.shields.io/badge/license-MIT-22863a)](LICENSE)

<br>

<img src="docs/img/pipeline-dark.png" alt="Rainmaker pipeline board" width="100%">

</div>

<br>

## The idea

An AI agent that runs live video sales calls is only as good as what it knows walking in, and a
rep console is only useful if it works on a train. Rainmaker is three systems that have to
cooperate:

<table>
<tr><td width="150"><b>Research agent</b></td><td>Reads a prospect's public pages and returns <b>typed facts with sources</b> — pricing motion, stack, open roles, buying signals. Never prose, never an unsourced claim.</td></tr>
<tr><td><b>Closer agent</b></td><td>Runs the call with an animated face: discloses it is AI before anything else, grounds every claim in what research actually read, hands off the instant someone asks for a human. Measured 800ms turn budget.</td></tr>
<tr><td><b>Rep console</b></td><td><b>Local-first.</b> Every edit lands on the device instantly and syncs when it can. Hand-rolled CRDT, IndexedDB, service worker, offline outbox.</td></tr>
</table>

<br>

## It genuinely works offline

Not "shows a cached page" offline. **Reload the tab with the connection severed and the whole
pipeline comes back**, edits still land, and the queue drains on reconnect.

<div align="center">
<img src="docs/img/offline.png" alt="Rainmaker running with the network disconnected" width="100%">
<br>
<sub>Captured by <a href="scripts/screenshots.mjs">the screenshot script</a> against a genuinely
severed connection — <code>setOffline(true)</code>, then a hard reload. The badge shows 83 edits
saved locally and waiting.</sub>
</div>

That screenshot is the reason the service worker exists: the first version of this script
failed with `ERR_INTERNET_DISCONNECTED`, which proved the app could not survive a cold reload.
All the CRDT machinery in the world is worthless if the tab refuses to start.

<br>

## The interesting engineering

### A CRDT with no server-side merge

`packages/crdt` is a hand-rolled op-based CRDT — hybrid logical clocks, LWW registers,
observed-remove tag sets, and an RGA text sequence for collaborative notes. The server never
merges anything; it appends, orders, and relays. That is what lets the console keep working
when the server is unreachable, and what keeps the server small enough to be obviously correct.

**Convergence is tested as a property, not with examples.** 300 randomised histories are
replayed into two replicas in different orders and asserted identical:

```ts
// For any set of ops S and any permutations p, q of S:
//   apply(p) on replica A  ==  apply(q) on replica B
```

That found two bugs inspection had missed — both the same class, and both invisible without it:
**a cancelling operation that arrives before the operation it cancels was being silently
dropped.** A character delete beating its insert made the character come back on one replica
only. The shrunk counterexample was two operations long.

### Two implementations, one guard

The console merges in TypeScript; the API materialises the same log in Python. Two
implementations of one merge rule *will* drift, and when they drift the API and the UI disagree
about a customer's pipeline — silently, because neither errors.

So [`packages/crdt/scripts/fixtures.ts`](packages/crdt/scripts/fixtures.ts) runs seven scenarios
through the **real** TypeScript replica and records what it decided; the Python suite asserts
its reducer agrees. Generated, never hand-written — a hand-written expectation encodes what the
author *believed* both sides do, which is the assumption under test. The concurrent-tiebreak
cases matter most: the answer is arbitrary, and both sides must pick the *same* arbitrary answer.

### A research agent that cannot make things up

<img src="docs/img/research.png" alt="Research panel with provenance badges" width="100%">

Every fact carries how it was obtained:

| tier | meaning |
|---|---|
| `OBSERVED` | appears verbatim on a page we fetched. Citable. |
| `DERIVED` | computed from observed values by a documented rule. Reproducible without a model. |
| `INFERRED` | a model's reading — and **structurally required** to carry the excerpt it was drawn from |

```python
Sourced[str](value="fintech", provenance=Provenance.INFERRED)
# ValueError: an INFERRED value must carry the excerpt it was inferred from;
#             an unsourced inference is a guess wearing a schema
```

This is deliberately **not** "an LLM with a browser tool". A fixed plan over high-yield paths, a
hard page budget, deterministic extraction. The model only adds labelled `INFERRED` fields on
top of a complete `OBSERVED` base and can never overwrite one. The same domain twice produces
byte-identical output.

**Scope discipline**, enforced in code rather than documented: public pages only, no credentials
ever, robots.txt obeyed including crawl-delay, per-host serialisation, hard page cap applied to
*requests* rather than successes.

### The latency budget is the product

<img src="docs/img/call.png" alt="Live call view with per-stage latency budget" width="100%">

Human turn-taking tolerates ~300ms; past ~800ms a pause stops reading as thinking and starts
reading as broken. Every stage streams into the next:

| stage | naive | streamed | budget |
|---|---:|---:|---:|
| VAD / endpoint | 250ms | 250ms | 250ms |
| STT final | 400ms | 60ms | 80ms |
| LLM first token | 700ms | 220ms | 250ms |
| TTS first audio | 350ms | 70ms | 100ms |
| lip-sync | 120ms | 30ms | 40ms |
| **total** | **~1820ms** | **~630ms** | **720ms** |

Starting TTS at the first *clause* rather than the first sentence is worth ~200ms — a quarter of
the entire budget.

### Disclosure is structural, not a setting

```python
Disclosure(required=False)
# DisclosureError: AI disclosure cannot be disabled. If a deployment believes it needs
#                  to, that is a legal question, not a configuration one.
```

A synthetic face on a sales call that does not say it is synthetic is the failure mode that ends
the company. Making it impossible to disable is cheaper than making it a setting somebody
eventually turns off.

<br>

## Avatar stack

Fully open-source, self-hostable, with commercial adapters behind the same interface:

| layer | choice | why |
|---|---|---|
| lip-sync | **MuseTalk** (~30fps, consumer GPU) | the only open-source lip-sync that is genuinely realtime |
| idle motion | **LivePortrait** | avoids the frozen-mannequin tell while listening |
| TTS | **Kokoro-82M** (Apache-2.0) | latency dominates perceived realism; best free latency/quality point |
| STT | **faster-whisper** streaming | realtime on GPU, no API cost |
| turn-taking | **Silero VAD** + semantic endpointing | interrupting badly reads as fake instantly |
| transport | **LiveKit** (Apache-2.0) | SFU, reconnection, adaptive bitrate |

**The shipped face is a vector viseme rig** ([`Avatar.tsx`](apps/console/src/components/Avatar.tsx)) —
deliberately the same pipeline a neural renderer uses:

```
audio / text ──► viseme sequence ──► mouth shape ──► frame
```

Only the last stage differs: vector paths instead of diffused pixels. Everything upstream — the
phoneme→viseme mapping, timing, co-articulation, idle behaviour — is what a realtime avatar
actually needs, and it is the part that decides whether a face reads as alive. What sells it is
not the mouth: irregular blinking (periodic blinking is instantly robotic), continuous
micro-sway, eye saccades that re-fix on the camera, brow lift on stressed syllables, and a
listening pose with slowed blinking so she never freezes between utterances.

> [!IMPORTANT]
> **What is verified and what is not.** The orchestration, budget accounting, disclosure
> enforcement, CRDT, sync, research agent, console, and the vector avatar are implemented and
> running — the screenshot above is the live rig captured mid-utterance. The **photoreal**
> option (MuseTalk over a LivePortrait idle loop) sits behind the same provider interface but
> has **not been run end to end here**: it needs several GB of weights and a persistent GPU
> service. Claiming a tested photoreal avatar would fall apart in the first interview question.

<br>

## Quickstart

```bash
git clone https://github.com/tasnimuldatascience/rainmaker && cd rainmaker
npm install
pip install -e "services/api[dev]"

# terminal 1 — API
uvicorn rainmaker.app:app --app-dir services/api/src --port 8000

# terminal 2 — console
npm run dev            # http://localhost:5173
```

Then **turn off your wifi and keep working.** That is the demo.

Optional, both with working fallbacks:

```bash
FIRECRAWL_API_KEY=fc-…     # research hits the real API instead of self-hosted Playwright
OUR_CATEGORY="vector search,learning-to-rank"   # job posts mentioning these score highest
```

<br>

## The console

<table>
<tr>
<td width="50%"><img src="docs/img/pipeline-light.png" alt="Pipeline, light theme"></td>
<td width="50%"><img src="docs/img/deal-drawer.png" alt="Deal detail with collaborative notes"></td>
</tr>
<tr>
<td align="center"><sub>Light theme — a real theme, not an inversion</sub></td>
<td align="center"><sub>Notes merge character by character. No save button, because there is nothing to save.</sub></td>
</tr>
</table>

Drag a card between stages with the network off; it lands instantly. There is no optimistic-
update bookkeeping and no rollback path, because there is no request that can fail.

<br>

## Tests

```bash
npm test                       # 19 — CRDT, incl. 300 randomised convergence runs
pytest services/api            # 59 — research, op log, relay, cross-implementation agreement
```

| suite | what it protects |
|---|---|
| `convergence.test.ts` | strong eventual consistency under random delivery orders |
| `test_research.py` | the agent cannot fabricate, wander off-domain, or exceed its budget |
| `test_sync.py` | dedup, durability-before-broadcast, backpressure, TS↔Python agreement |

<br>

## Architecture

```
packages/crdt          hybrid logical clocks · LWW · OR-Set · RGA text   (TypeScript)
apps/console           React 18 · IndexedDB · service worker · outbox
services/api
  research/            fetch → extract → typed facts with provenance
  sync/                op log (SQLite WAL) + backpressured relay
  calls/               turn loop, latency budget, disclosure
  crm/                 read-only materialisation
```

Nothing above `sync/` knows how an op arrived; nothing above `research/extract` knows how a page
was fetched. See [ARCHITECTURE.md](ARCHITECTURE.md) for the decisions and what was rejected.

<br>

## License

MIT. Not affiliated with River. Built as an original system exploring the same problem space.
