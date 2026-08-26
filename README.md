<div align="center">

# Rainmaker

**A sales tool that keeps working when the internet does not.**

[![ci](https://github.com/tasnimuldatascience/rainmaker/actions/workflows/ci.yml/badge.svg)](https://github.com/tasnimuldatascience/rainmaker/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)](services/api/pyproject.toml)
[![typescript](https://img.shields.io/badge/typescript-5.6-3178c6?logo=typescript&logoColor=white)](packages/crdt)
[![tests](https://img.shields.io/badge/tests-309%20passing-22863a)](#tests)
[![license](https://img.shields.io/badge/license-MIT-22863a)](LICENSE)

<br>

<img src="docs/img/pipeline-dark.png" alt="Rainmaker pipeline board" width="100%">

</div>

---

## What is this?

**Give it your work email and it runs your first sales call.** It reads your company's website
while you watch, opens your own pages and talks you through them, books a meeting in a real
calendar, and shows you a price — then writes the whole thing into the pipeline.

| Part | What it does |
|---|---|
| **The call** | An AI account executive you can type to or talk to. She answers out loud, says she is an AI before anything else, and stops selling the moment you ask for a person |
| **Research** | Reads a prospect's public website live, on screen, and only lets her state what it actually found |
| **Tools** | A calendar, a CRM, a browser and a mailbox — reached over MCP, so a customer's own systems drop in |
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

The four tool servers need no setup — the API starts them itself, and the calendar and CRM work
offline like the rest of the console.

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

## The call

<img src="docs/img/call.png" alt="Liv mid-call, screen-sharing the prospect's own pricing page" width="100%">

That is one screenshot of one real call. The email `dana.whitfield@stripe.com` went in; everything
after it happened by itself.

**1 — She reads their site before she says a word.** Not a lookup: a browser opens their pages and
pulls out facts with the page each came from.

<img src="docs/img/research-live.png" alt="Nine facts read live from stripe.com" width="100%">

**2 — Then she shows them their own website.** That screenshot at the top is stripe.com/pricing,
opened live and narrated. The browsing is not a background job — it is the demo, because reading a
prospect's site before a call is the work she is replacing.

**3 — She books out of a real calendar.**

<img src="docs/img/booking.png" alt="Real availability, offered and booked" width="100%">

**4 — Then the price**, sized to what she found, on screen and never spoken. And the whole call is
written into the pipeline as CRDT ops through the CRM tool server, so a rep's laptop sees it like
any other edit.

### The graph decides, the model writes

The step the call is on, which tool fires, and every sentence where being wrong costs something —
the disclosure, the times offered, the booking confirmation, the handoff — are all decided in
code. The model writes the greeting, the questions, and the narration.

That split is the whole design. A model that phrases a question awkwardly costs a moment. A model
that decides on its own to confirm a meeting books nothing and promises everything, and
Qwen2.5-1.5B is nowhere near reliable enough to be trusted with the difference.

Two things she will not do, both enforced rather than requested:

```python
Disclosure(required=False)
# DisclosureError: AI disclosure cannot be disabled. If a deployment believes it needs
#                  to, that is a legal question, not a configuration one.
```

Ask for a human and she stops selling immediately — one fixed line, and the model is never
consulted about whether to agree. She is also held to two sentences a turn in code: asked
politely, the model wrote four to six every time and got cut off mid-word by the token limit.

### Her tools are a protocol, not an integration

Four MCP servers, each runnable on its own in any MCP host:

```bash
python -m rainmaker.mcp.servers.calendar    # availability, booking, cancellation
python -m rainmaker.mcp.servers.crm         # call outcomes, as CRDT ops
python -m rainmaker.mcp.servers.research    # a page's text, and a picture of it
python -m rainmaker.mcp.servers.email       # the follow-up. Off without SMTP
```

They run as separate processes so a third-party server that hangs cannot take a live call with
it, and every call has a deadline. Swapping the local calendar for a customer's Google Calendar
is a line in `mcp.toml` — the point of building it this way rather than as a function she calls.

The calendar will not sell the same slot twice, and that is enforced by a unique index rather
than by code, because listing times and booking one are separated by a conversation.

### Why it does not pause awkwardly

People tolerate about 300ms of silence. Past about 800, a pause stops sounding like thinking.

Nothing waits for the previous thing to finish: synthesis starts on the first few words of a
reply, and clause two is produced while clause one is playing. Measured on the same 175-character
reply:

| | Silence before the first sound |
|---|---:|
| Synthesise the whole reply, then play it | 3007ms |
| Synthesise the first clause first | **407ms** |

Fifteen real turns: **850ms median** from pressing send to hearing her — the model ~140ms, the
voice ~720ms. The console shows the breakdown per turn, and on this hardware it usually goes
slightly over budget. Synthesis is why, and [ARCHITECTURE.md](ARCHITECTURE.md) says why the GPU
fix was not taken.

### Her face

She is a **photoreal portrait of nobody** — a StyleGAN face from a public-domain set, not a real
person's likeness. It brightens and moves with the real loudness of the audio playing at that
instant, and it does not pretend to lip-sync.

That last part is deliberate. Warping the mouth of a still photograph is guesswork, and the
uncanny valley is steepest exactly there: a nearly-right face moving slightly wrong reads as a
corpse, while the same face holding still reads as a video call with a frozen frame, which
everyone sees every week. Real lip-sync is a provider swap — set a streaming-avatar key and the
same slot renders a talking head.

> [!IMPORTANT]
> **What is running and what is not.** The conversation, the voice, the research, the browsing,
> the tools, the timing, the disclosure and the handoff are all built and running — the
> screenshots are one live call and the numbers came off the wire. The hosted talking-head
> provider is reachable through the same interface and **has not been run here**: there is no key.
> **MuseTalk was tried and does not run on this machine at all** — it pins `torch 2.0.1+cu118`,
> whose newest architecture is `sm_90`, and this GPU is `sm_120`. `calls/avatar.py` has the
> details rather than leaving an adapter nobody can run.

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
pytest                         # 260 tests — research, syncing, the API, the live call, the tools
```

309 tests in total. None of them load a language model: a test that spends six seconds on
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
| `test_avatar.py` | That the face admits what it is: synthetic, and not lip-syncing unless a provider is actually doing it |
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
apps/console           the dashboard and the call surface       (React)
services/api
  calls/
    intake.py          an email address becomes a domain to research
    agenda.py          the plot: which step, which tool, which words are fixed
    pipeline.py        the turn loop, the latency budget, the disclosure
    providers.py       the local model and the local voice
    avatar.py          which face is on screen, and what it admits to
  mcp/
    client.py          spawns the tool servers, routes calls, enforces deadlines
    servers/           calendar, crm, research, email — each runnable on its own
  research/            reading websites and extracting facts
  sync/                collecting and relaying changes
  crm/                 turning changes into a readable view
```

Nothing above `sync/` knows how a change arrived. Nothing above `research/extract` knows how a
page was fetched. See [ARCHITECTURE.md](ARCHITECTURE.md) for the decisions and what was rejected.

---

## Licence

MIT. Not affiliated with River. An original system exploring the same problem.
