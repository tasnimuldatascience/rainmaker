<div align="center">

# Rainmaker

**A sales tool that keeps working when the internet does not.**

[![ci](https://github.com/tasnimuldatascience/rainmaker/actions/workflows/ci.yml/badge.svg)](https://github.com/tasnimuldatascience/rainmaker/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)](services/api/pyproject.toml)
[![typescript](https://img.shields.io/badge/typescript-5.6-3178c6?logo=typescript&logoColor=white)](packages/crdt)
[![tests](https://img.shields.io/badge/tests-286%20passing-22863a)](#tests)
[![license](https://img.shields.io/badge/license-MIT-22863a)](LICENSE)

<br>

<img src="docs/img/pipeline-dark.png" alt="Rainmaker pipeline board" width="100%">

</div>

---

## What is this?

Three things a sales team needs, built to work together:

| Part | What it does |
|---|---|
| **Research** | Reads a company's public website and pulls out useful facts — how they price, what they use, who they are hiring |
| **AI caller** | An agent you can type to or talk to. It answers out loud, says it is AI up front, and hands over to a person when asked — running a language model and a voice on your own machine |
| **Sales dashboard** | Where reps track their deals — **and it works with no internet at all** |

---

## The offline part is real

Not "shows you a cached page". **Turn off your wifi, reload the browser tab, and everything
still works.** Your edits save. When the connection returns, they sync.

<div align="center">
<img src="docs/img/offline.png" alt="Rainmaker running with the network disconnected" width="100%">
<br>
<sub>Taken with the network genuinely disconnected, then a full page reload. The badge shows
<b>83 edits saved on the device</b>, waiting to sync.</sub>
</div>

Why this is hard: the first version of that screenshot script failed with
`ERR_INTERNET_DISCONNECTED`. The app could not even start after a reload. All the clever syncing
in the world is useless if the page refuses to open.

**Try it yourself:** run it, drag a deal card between columns, turn off your wifi, keep working.
There is no spinner and no "failed to save" message, because there is no request that can fail.

---

## Run it

```bash
git clone https://github.com/tasnimuldatascience/rainmaker && cd rainmaker
npm install
pip install -e "services/api[dev]"

# terminal 1 — the backend
uvicorn rainmaker.app:app --app-dir services/api/src --port 8000

# terminal 2 — the dashboard
npm run dev            # http://localhost:5173
```

Then **turn off your wifi and keep working.** That is the demo.

### Giving her the local brain and voice

The call works without this — she answers from a grounded script and the browser speaks. For the
real thing:

```bash
pip install -e "services/api[voice]"      # the voice: ~40MB of wheels
python scripts/fetch-models.py            # its weights: 330MB, Apache-2.0, no account

pip install -e "services/api[brain]"      # the model: torch, ~2.5GB
# Qwen2.5-1.5B-Instruct downloads itself on first start
```

Restart the API and `/api/calls/health` will say what loaded. So will the console.

Optional, and everything works without them:

```bash
FIRECRAWL_API_KEY=fc-…     # use the real research API instead of the built-in browser
OUR_CATEGORY="vector search,learning-to-rank"   # rank job posts mentioning these highest
RAINMAKER_VOICE=browser    # force the fallback voice, to hear the difference
```

---

## How the offline part works

Normally, saving something means asking a server and waiting. If the server cannot be reached,
you get an error and your work is stuck.

Rainmaker never asks. Every edit is saved on your device immediately, and the change is queued to
send later. The server's only job is to collect changes and pass them on — **it never decides who
wins.**

That raises an obvious problem: what if two people edit the same deal while both are offline?

The answer is a **CRDT** — a way of storing data where changes can arrive in any order, or twice,
or years late, and everyone still ends up with the same result.

### Testing it properly

You cannot test this with a handful of examples, because the bugs live in orderings nobody thinks
to write down.

So the tests generate **300 random sequences of edits**, deliver them to two devices in different
orders, and check both end up identical:

```
For any set of edits, delivered in any two orders:
    device A's result  ==  device B's result
```

That found two bugs that reading the code had missed. Both were the same shape:

> **A deletion arriving before the thing it deletes.** Delete a character, and if that deletion
> reaches the other device before the original typing does, the character comes back — on one
> device only.

The failing case the test found was two operations long. Nobody would have written that test
by hand.

### Two versions of the same rules

The dashboard merges changes in TypeScript. The backend does the same in Python.

Two implementations of the same rule **will** drift apart eventually, and when they do, the
dashboard and the backend quietly disagree about a customer's deal. Nothing errors. Nobody
notices.

So the TypeScript version runs seven scenarios and records what it decided. The Python tests then
check they agree. These recordings are **generated by running the real code**, never written by
hand — a hand-written expectation only records what the author *believed* both sides do, which is
exactly the thing being tested.

---

## Research that cannot make things up

<img src="docs/img/research.png" alt="Research panel with provenance badges" width="100%">

Every fact comes with a label saying where it came from:

| Label | Meaning |
|---|---|
| **Seen** | These exact words appear on a page we read. You can quote it |
| **Worked out** | Calculated from things we saw, by a written-down rule. No AI involved |
| **AI guess** | An AI's interpretation — **and it must show the text it was reading** |

That last rule is enforced by the code, not by convention:

```python
Sourced[str](value="fintech", provenance=Provenance.INFERRED)
# ValueError: an INFERRED value must carry the excerpt it was inferred from;
#             an unsourced inference is a guess wearing a schema
```

**This is deliberately not "an AI with a web browser."** It follows a fixed list of pages, has a
hard limit on how many it will read, and pulls out facts using ordinary code. The AI only adds
clearly-labelled guesses on top, and **can never overwrite something that was actually seen.**

Run it twice on the same company and you get byte-for-byte identical results.

**It also stays in bounds**, enforced in code: public pages only, never any login, obeys
`robots.txt` including the requested delay between requests, one request at a time per website,
and the page limit counts *attempts* rather than successes.

---

## The AI caller

<img src="docs/img/call.png" alt="Live call: the agent mid-reply, with the per-stage latency budget" width="100%">

**Type to her, or hold the talk button and speak. She answers out loud either way.** The reply
above came out of a 1.5-billion-parameter model on the laptop that took the screenshot, and the
voice is a 330MB synthesiser running on its CPU. Nothing left the machine.

| Part | What runs | Where |
|---|---|---|
| Thinking | **Qwen2.5-1.5B-Instruct**, streamed token by token | your GPU, or your CPU |
| Speaking | **Kokoro-82M**, synthesised clause by clause | your CPU |
| Hearing | the browser's own speech recognition | the browser |

The first two are optional. Without them the agent still holds a conversation — it answers from
a small grounded script and the browser speaks the words — and the console's engine badge says
which of the two it is, because a demo that has quietly degraded and does not mention it is how
a reviewer concludes the product always sounded like that.

### Why it does not pause awkwardly

In conversation people tolerate about 300 milliseconds of silence. Past about 800, a pause stops
sounding like thinking and starts sounding broken.

The trick is that **nothing waits for the previous thing to finish**. Synthesis starts on the
first few words of the reply rather than the first sentence, and the audio for clause two is
produced while clause one is still playing. Measured on the same 175-character reply:

| | Silence before the first sound |
|---|---:|
| Synthesise the whole reply, then play it | 3007ms |
| Synthesise the first clause first | **407ms** |

Fifteen real turns on that laptop: **850ms median** from pressing send to hearing her, of which
the model is ~140ms and the voice ~720ms. The console shows the breakdown per turn, so when it
goes over budget you can see which stage spent it. On this hardware it usually does go slightly
over, and synthesis is why — see [ARCHITECTURE.md](ARCHITECTURE.md).

### Two things it will not do

```python
Disclosure(required=False)
# DisclosureError: AI disclosure cannot be disabled. If a deployment believes it needs
#                  to, that is a legal question, not a configuration one.
```

An AI on a sales call that does not admit it is an AI is the mistake that ends a company. Making
it impossible to switch off is cheaper than making it a setting somebody eventually switches off.

The second is the handoff. **Ask for a human and she stops selling immediately** — one fixed
line, and the model is never consulted about whether to agree. Asking a small model to abandon
its objective on request is a bet, and the stake is the worst thing this product can do.

She is also held to two sentences per turn, in code rather than in the prompt. Asked politely,
the model wrote four to six every time and got cut off mid-word by the token limit.

### The face

**What ships is a drawn face**, not a photo-realistic one, and it moves to the audio that is
actually playing — the mouth shapes come from the clause being spoken and stop when the sound
does. What sells it is not the mouth: it is irregular blinking, constant small movement, eyes
that drift and return, and a listening pose so she never freezes between sentences.

> [!IMPORTANT]
> **What is running and what is not.** The conversation, the voice, the timing, the disclosure,
> the handoff, the syncing, the research and the dashboard are all built and running — that
> screenshot is the live system mid-sentence, and the numbers above came off the wire. A
> **photo-realistic** face (MuseTalk over LivePortrait) is wired up behind the same interface
> but **has not been run here**: it needs several gigabytes of weights and a dedicated GPU.

---

## The dashboard

<table>
<tr>
<td width="50%"><img src="docs/img/pipeline-light.png" alt="Pipeline, light theme"></td>
<td width="50%"><img src="docs/img/deal-drawer.png" alt="Deal detail with collaborative notes"></td>
</tr>
<tr>
<td align="center"><sub>Light theme — properly designed, not just inverted</sub></td>
<td align="center"><sub>Notes merge letter by letter. No save button, because there is nothing to save</sub></td>
</tr>
</table>

---

## Tests

```bash
npm test                       # 49 tests — syncing, text editing, the call rail
pytest                         # 237 tests — research, syncing, the API, the live call, the tools
```

286 tests in total. None of them load a language model: a test that spends six seconds on
Qwen to check that a WebSocket sends JSON is testing Qwen.

| Test file | What it protects |
|---|---|
| `convergence.test.ts` | Everyone ends up with the same data, whatever order changes arrive in — plus that the order is actually *correct* |
| `diff.test.ts` | Typing in a shared note produces the right text, including emoji and accents |
| `test_research.py` | The research agent cannot invent facts, wander onto other websites, or read more pages than allowed |
| `test_sync.py` | Duplicate changes, saving before broadcasting, slow clients, and TypeScript ↔ Python agreement |
| `test_app.py` | **The offline flush endpoint.** Reconnect, retry, deduplicate, and never half-apply a batch |
| `test_pipeline.py` | That a call cannot start without the AI disclosure, and that every stage of the turn is measured |
| `test_call.py` | Where a reply is cut for synthesis, what the agent is allowed to claim, and that asking for a human ends the sell without the model being consulted |
| `test_agenda.py` | That she researches before she greets, that the times she offers come from the calendar and not the model, and that typing over her introduction does not kill the call |
| `test_mcp.py` | That the calendar cannot sell the same slot twice, that a dead tool server degrades the call instead of ending it, and that she will not email anyone who was not on the call |
| `test_readme.py` | That these counts are the counts. A badge is an image, and nobody proofreads an image |

### The bug the API tests found immediately

`/api/sync/append` — the path a console uses to flush its offline queue — **could not accept a
request body at all.** Every POST returned:

```json
422  {"detail":[{"type":"missing","loc":["query","req"],"msg":"Field required"}]}
```

The request model was declared inside `create_app()`. With `from __future__ import annotations`
every annotation is a string that FastAPI resolves against the *module's* globals, and a class
defined in a function body is not in them — so the parameter silently degraded to a query
parameter.

**It went unnoticed because its failure is silent by design.** The console only reaches for this
path when the WebSocket is unavailable — *"it works in situations the WebSocket does not (some
corporate proxies)"* — and a failed flush leaves the ops queued for retry rather than surfacing an
error, because from the user's point of view the write already succeeded locally. So on exactly
the networks the fallback exists to serve, nothing ever synced and nobody was told.

The endpoint had no tests. It now has eleven.

### And one it found on the second look

A batch containing one malformed op was rejected with 422 *after* persisting the good ops before
it. The console retries the whole batch on failure, so the bad op failed forever while the good
ones deduplicated: the outbox never drained, and still nothing surfaced. Ops are validated in full
before anything is written now — one poison op rejects its batch rather than half-applying it.

### A bug that convergence testing could not find

The 300 randomised runs check that two devices **agree**. They passed throughout while shared
notes were quietly scrambling, because both devices agreed on the same wrong answer — and
agreeing on a wrong answer is still agreeing.

Characters were ordered by comparing their ids as text. Ids look like `alice:6` and `alice:10`,
and as text `"alice:10"` sorts before `"alice:6"`, because `1` comes before `6`. **So notes
started scrambling after about ten keystrokes.**

The first attempted fix — read the number properly and compare it numerically — was also wrong,
and broke the case the first bug was hiding: those numbers count per person, so one person's
edit number 0 can happen long after another person's edit number 5. Two people typing in the
same sentence got their words tangled with text that predated them both.

Both are now ordered by timestamp, which is the only value that compares meaningfully across
people. It took a test that checked **what the text actually said**, rather than that two copies
matched.

---

## Project layout

```
packages/crdt          the offline-syncing data structures     (TypeScript)
apps/console           the dashboard                            (React)
services/api
  research/            reading websites and extracting facts
  sync/                collecting and relaying changes
  calls/               the call loop, timing, and disclosure
  crm/                 turning changes into a readable view
```

Nothing above `sync/` knows how a change arrived. Nothing above `research/extract` knows how a
page was fetched. See [ARCHITECTURE.md](ARCHITECTURE.md) for the decisions and what was rejected.

---

## Licence

MIT. Not affiliated with River. An original system exploring the same problem.
