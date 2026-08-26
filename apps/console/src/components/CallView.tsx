/**
 * The live call.
 *
 * THE SHAPE OF THIS SCREEN IS THE PRODUCT. It is not a chat window with a face attached — it is
 * a demo that begins with an email address, opens the prospect's own website while they watch,
 * talks over it, offers real times from a real calendar, and shows a price at the end. The
 * layout follows that: the stage is whatever Liv is currently showing, and Liv herself is a
 * small floating window over it, the way a person presenting a screen appears.
 *
 * WHY THE EMAIL COMES FIRST. Everything she knows before she speaks comes from the domain in it.
 * A "Start call" button would open a conversation with someone she knows nothing about, which is
 * the generic AI-chat demo this is deliberately not.
 *
 * WHAT CHANGED HERE, because the file's history is the point: it used to play a hardcoded script
 * of four exchanges on `setTimeout` and animate the mouth at 52ms per character. Everything on
 * screen was a drawing of a call.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CallState, FrontDoor, Intake as IntakeFields, Panels, Step } from "../lib/call";
import { frontDoor } from "../lib/call";
import { LiveCall } from "../lib/call";
import type { LocalStore } from "../lib/store";
import { Portrait } from "./Portrait";

const STAGE_COLOR: Record<string, string> = {
  stt: "var(--accent)",
  llm: "var(--agent)",
  tts: "var(--info)",
  avatar: "var(--won)",
};

const BUDGET_MS = 800;
const STAGE_ORDER = ["stt", "llm", "tts", "avatar"];

/**
 * The rail along the top of the stage. Order is the happy path; the call may skip about.
 *
 * EVERY STEP THE AGENDA CAN EMIT HAS TO BE HERE. `opening` was missing, so for the three or
 * four seconds she spends introducing herself nothing was highlighted and the rail read as
 * broken — `findIndex` returns -1 and every pill falls through to "idle".
 */
const STEPS: { id: Step; label: string }[] = [
  { id: "researching", label: "Reading their site" },
  { id: "opening", label: "Introducing" },
  { id: "discovery", label: "Understanding" },
  { id: "guide", label: "Showing" },
  { id: "compare", label: "Comparing" },
  { id: "quote", label: "Quoting" },
  { id: "close", label: "Closing" },
  { id: "pay", label: "Checkout" },
  { id: "wrap", label: "Wrapping up" },
];

export function CallView({ store }: { store: LocalStore }) {
  const call = useMemo(() => new LiveCall(), []);
  const [state, setState] = useState<CallState | null>(null);
  const [draft, setDraft] = useState("");
  const [who, setWho] = useState<IntakeFields>({ name: "", email: "", company: "" });
  const [door, setDoor] = useState<FrontDoor | null>(null);

  // Asked once, before anything is drawn. The console always reaches tenant zero, so this is
  // the same request the embed makes with a key in it.
  useEffect(() => {
    let live = true;
    void frontDoor().then((found) => live && setDoor(found));
    return () => {
      live = false;
    };
  }, []);
  const transcriptEnd = useRef<HTMLDivElement | null>(null);

  useEffect(() => call.subscribe(setState), [call]);

  // Leaving the view ends the call. Without this the socket stays open, the agent keeps talking
  // into a component that no longer exists, and the next visit joins mid-sentence.
  useEffect(() => () => call.hangUp(), [call]);

  useEffect(() => {
    transcriptEnd.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [state?.turns.length]);

  const send = useCallback(() => {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    call.say(text);
  }, [call, draft]);

  if (!state) return null;

  const live = state.phase !== "idle" && state.phase !== "ended";
  const lastAgent = [...state.turns].reverse().find((t) => t.who === "agent" && t.budget);
  const budget = lastAgent?.budget;
  const stages: [string, number][] = budget
    ? STAGE_ORDER.flatMap((stage) => {
        const ms = budget[stage];
        return typeof ms === "number" ? [[stage, ms] as [string, number]] : [];
      })
    : [];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Live call</h1>
          <p>
            She reads their business before she says a word, drives the product on screen while
            she talks, quotes a number computed from your pricing, takes payment, and books a
            person only when the deal actually needs one.
          </p>
        </div>
      </div>

      <div className="call-grid">
        {/* ── the stage ─────────────────────────────────────────────── */}
        <div className="demo" data-live={live}>
          {live && <StepRail step={state.step} />}

          {live ? (
            <Stage panels={state.panels} active={state.active} onPick={(i) => call.pickSlot(i)} />
          ) : (
            <Intake
              door={door}
              who={who}
              setWho={setWho}
              error={state.intakeError}
              errorField={state.intakeField}
              connecting={state.phase === "connecting"}
              onStart={() =>
                void call.connect({
                  name: who.name.trim(),
                  email: who.email.trim(),
                  company: who.company.trim(),
                })
              }
            />
          )}

          {live && (
            <>
              <div className="caption" data-empty={!state.caption && !state.partial}>
                {state.caption ||
                  (state.partial && (
                    <em style={{ color: "var(--fg-3)" }}>{state.partial}</em>
                  )) ||
                  (state.phase === "thinking" ? "…" : "Ask her anything.")}
              </div>

              <div className="composer">
                <input
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && send()}
                  placeholder={
                    state.handsFree
                      ? "She's listening — or type"
                      : "Type, or turn the microphone on and just talk"
                  }
                  aria-label="Say something to the agent"
                />
                <button className="btn" onClick={send} disabled={!draft.trim()}>
                  Send
                </button>
                <button
                  className="talk"
                  data-listening={state.listening}
                  data-on={state.handsFree}
                  disabled={!state.micSupported}
                  title={
                    state.micSupported
                      ? "Leave the microphone open. It closes while she speaks, because the browser gives no echo cancellation to speech recognition — so you cannot talk over her. Chrome sends the audio to Google; typing stays on this machine."
                      : "This browser has no speech recognition. Typing works everywhere."
                  }
                  onClick={() => call.setHandsFree(!state.handsFree)}
                >
                  {state.listening ? <i /> : "🎤"}
                  {!state.handsFree
                    ? "Talk to her"
                    : state.listening
                      ? "Listening"
                      : "Mic on · she's speaking"}
                </button>
              </div>

              <div className="pip" data-speaking={state.speaking}>
                <Portrait
                  level={() => call.level()}
                  mouth={() => call.mouthFrame()}
                  speaking={state.speaking}
                  listening={!state.speaking}
                  fill
                />
                <span className="pip-name">
                  <span className="live-dot" />
                  Liv · Rainmaker
                </span>
              </div>
            </>
          )}
        </div>

        {/* ── the instruments ───────────────────────────────────────── */}
        <div className="col-flex" style={{ gap: "var(--s-4)" }}>
          <div className="row">
            {live ? (
              <button className="btn" onClick={() => call.hangUp()}>
                End call
              </button>
            ) : (
              state.turns.length > 0 && (
                <span className="tiny muted">Call ended.</span>
              )
            )}
            {state.handoff && (
              <span className="tag" data-tone="won">
                handed off to a human
              </span>
            )}
            {state.booked && (
              <span className="tag" data-tone="won">
                meeting booked
              </span>
            )}
          </div>

          {state.error && (
            <div className="card" style={{ borderColor: "var(--lost)" }}>
              <p className="tiny" style={{ color: "var(--lost)" }}>
                {state.error}
              </p>
            </div>
          )}

          <div className="card">
            <h3>What is running</h3>
            <p className="sub">
              Reported by the server, not assumed. A demo that has quietly fallen back to the
              browser voice should say so.
            </p>
            {state.engines ? (
              <div className="engine-row">
                <span className="engine" data-local={state.engines.llm.local}>
                  brain{" "}
                  <b>
                    {state.engines.llm.local
                      ? (state.engines.llm.model?.split("/").pop() ?? "local")
                      : "scripted"}
                  </b>
                  {state.engines.llm.device && ` · ${state.engines.llm.device}`}
                </span>
                <span className="engine" data-local={state.engines.tts.local}>
                  voice <b>{state.engines.tts.local ? "Kokoro-82M" : "browser"}</b>
                </span>
                <span className="engine" data-local={false}>
                  ears <b>browser</b>
                </span>
              </div>
            ) : (
              <p className="tiny muted">Start a call to see which engines loaded.</p>
            )}
          </div>

          <div className="card">
            <h3>Turn latency budget</h3>
            <p className="sub">
              Measured per stage. Past {BUDGET_MS}ms a pause stops reading as thinking and starts
              reading as broken. Transcription and the face are timed in the browser; the model
              and the voice on the server.
            </p>
            {stages.length ? (
              <>
                <div className="budget">
                  {stages.map(([stage, ms]) => (
                    <i
                      key={stage}
                      style={{ flex: Math.max(ms, 1), background: STAGE_COLOR[stage] }}
                      title={`${stage} ${Math.round(ms)}ms`}
                    >
                      {ms > 60 ? stage : ""}
                    </i>
                  ))}
                </div>
                <div className="budget-key">
                  {stages.map(([stage, ms]) => (
                    <span key={stage}>
                      <span
                        style={{
                          display: "inline-block",
                          inlineSize: 8,
                          blockSize: 8,
                          borderRadius: 2,
                          marginInlineEnd: 5,
                          background: STAGE_COLOR[stage],
                        }}
                      />
                      {stage} {Math.round(ms)}ms
                    </span>
                  ))}
                  <span className="right">
                    <b
                      style={{
                        color: (lastAgent?.total ?? 0) <= BUDGET_MS ? "var(--won)" : "var(--risk)",
                      }}
                    >
                      total {Math.round(lastAgent?.total ?? 0)}ms
                    </b>{" "}
                    / {BUDGET_MS}ms
                  </span>
                </div>
              </>
            ) : (
              <p className="tiny muted">Say something to see where the time went.</p>
            )}
          </div>

          <div className="card" style={{ maxBlockSize: "38vh", overflow: "auto" }}>
            <h3>Transcript</h3>
            <p className="sub">
              Written to the deal record when the call ends — as CRDT ops, through the CRM tool
              server, so a rep&apos;s laptop sees it like any other edit.
            </p>
            {state.turns.length === 0 && <p className="tiny muted">Nothing yet.</p>}
            {state.turns.map((turn, i) => (
              <div className="turn" key={i}>
                <span className="turn-who" data-who={turn.who}>
                  {turn.who === "agent" ? "Liv" : "You"}
                </span>
                <div>
                  <p style={{ fontSize: "var(--t-sm)", lineHeight: 1.6 }}>{turn.text}</p>
                  {turn.total !== undefined && turn.total > 0 && (
                    <p className="tiny muted" style={{ marginBlockStart: 4 }}>
                      {Math.round(turn.total)}ms{" "}
                      {turn.total <= BUDGET_MS ? "· within budget" : "· over budget"}
                    </p>
                  )}
                </div>
              </div>
            ))}
            <div ref={transcriptEnd} />
            {state.turns.length > 0 && (
              <button
                className="btn"
                style={{ marginBlockStart: "var(--s-4)" }}
                onClick={() => {
                  const text = state.turns
                    .map((t) => `${t.who === "agent" ? "Liv" : "Prospect"}: ${t.text}`)
                    .join("\n\n");
                  store.editText("deal", "d-corvus", "notes", text);
                }}
              >
                Copy transcript into Corvus Data
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * The front door: name, work email, company.
 *
 * THREE FIELDS, WHICH IS TWO MORE THAN IT HAD. One field is a nicer form and a worse call — the
 * name is how she greets you, the company is what she talks about, and the address is what she
 * reads before she says a word. It is also the same thing a human rep asks in the first ten
 * seconds, so nobody experiences it as a gate.
 *
 * THE ERROR GOES UNDER THE FIELD IT BELONGS TO, in the words the server chose, because the
 * server is the thing that knows why and it phrased it as a sentence she could say out loud.
 */
function Intake({
  door,
  who,
  setWho,
  error,
  errorField,
  connecting,
  onStart,
}: {
  door: FrontDoor | null;
  who: IntakeFields;
  setWho: (value: IntakeFields) => void;
  error: string | null;
  errorField: string | null;
  connecting: boolean;
  onStart: () => void;
}) {
  const enter = (e: React.KeyboardEvent) => e.key === "Enter" && onStart();
  const bad = (field: string) => (error && errorField === field ? true : undefined);
  const asks = (field: string) => !door || door.fields.includes(field);

  return (
    <div className="hero">
      <span className="pill">
        <i />
        Live demo
      </span>
      <h2>
        Meet <span>Liv</span>.
      </h2>
      <p>
        The AI account executive that answers every buyer the moment they land, at any hour, with
        nobody to wait for. Tell her who you are and she&apos;ll read your business before she
        says a word. This call is the demo.
      </p>

      <div className="intake">
        <label className="field">
          <span>Your name</span>
          <input
            value={who.name}
            autoComplete="name"
            placeholder="Dana Whitfield"
            data-bad={bad("name")}
            onChange={(e) => setWho({ ...who, name: e.target.value })}
            onKeyDown={enter}
          />
        </label>
        <label className="field">
          <span>{door && !door.require_work_email ? "Email" : "Work email"}</span>
          <input
            type="email"
            value={who.email}
            autoComplete="email"
            placeholder="dana@yourcompany.com"
            data-bad={bad("email")}
            onChange={(e) => setWho({ ...who, email: e.target.value })}
            onKeyDown={enter}
          />
        </label>
        {asks("company") && (
          <label className="field">
            <span>Company</span>
            <input
              value={who.company}
              autoComplete="organization"
              placeholder="Corvus Data"
              data-bad={bad("company")}
              onChange={(e) => setWho({ ...who, company: e.target.value })}
              onKeyDown={enter}
            />
          </label>
        )}
        <button className="btn" data-variant="primary" onClick={onStart} disabled={connecting}>
          {connecting ? "Connecting…" : "Start the call"}
        </button>
      </div>
      {error ? (
        <p className="tiny" style={{ color: "var(--lost)", marginBlockStart: "var(--s-3)" }}>
          {error}
        </p>
      ) : (
        <p className="tiny muted" style={{ marginBlockStart: "var(--s-3)" }}>
          {door && !door.require_work_email
            ? "Nothing is shared with anyone else."
            : "A work address, please — the domain is what she reads before the call starts."}
        </p>
      )}
    </div>
  );
}

/** Where the call is. A rail rather than a spinner: the steps are the story. */
function StepRail({ step }: { step: Step | null }) {
  const index = STEPS.findIndex((entry) => entry.id === step);
  return (
    <div className="rail" role="status" aria-label="call progress">
      {STEPS.map((entry, i) => (
        <span
          key={entry.id}
          className="rail-step"
          data-state={
            step === "handoff" || step === "booking"
              ? i < index
                ? "done"
                : "idle"
              : i < index
                ? "done"
                : i === index
                  ? "now"
                  : "idle"
          }
        >
          {entry.label}
        </span>
      ))}
      {(step === "handoff" || step === "booking") && (
        <span className="rail-step" data-state="now">
          {step === "booking" ? "Getting a person" : "Handing over"}
        </span>
      )}
    </div>
  );
}

/**
 * What she is showing right now.
 *
 * ONE PANEL AT A TIME. A dashboard of six simultaneous widgets is a screen nobody reads; a
 * presenter shows one thing and talks about it.
 */
function Stage({
  panels,
  active,
  onPick,
}: {
  panels: Panels;
  active: keyof Panels | null;
  onPick: (index: number) => void;
}) {
  if (active === "browser" && panels.browser) {
    const page = panels.browser;
    return (
      <div className="screen">
        <div className="screen-bar">
          <span className="screen-dots" aria-hidden>
            <i /> <i /> <i />
          </span>
          <span className="screen-url">{page.url}</span>
          {page.label && <span className="tag">{page.label}</span>}
        </div>
        {page.frame ? (
          <img
            className="screen-frame"
            src={`data:image/jpeg;base64,${page.frame}`}
            alt={page.title || page.url}
          />
        ) : (
          <div className="screen-loading">Opening {page.url}…</div>
        )}
      </div>
    );
  }

  if (active === "slots" && panels.slots) {
    return (
      <div className="stage-card">
        <h3>Times she actually has</h3>
        <p className="sub">
          Read straight from the calendar tool, in its own words. She is not allowed to invent one.
        </p>
        <div className="slot-list">
          {panels.slots.slots.map((slot, i) => (
            <button
              key={slot.starts_at}
              className="slot"
              data-failed={panels.slots?.failed === slot.starts_at}
              onClick={() => onPick(i)}
            >
              {slot.spoken}
            </button>
          ))}
        </div>
      </div>
    );
  }

  if (active === "pricing" && panels.pricing) {
    return (
      <div className="stage-card">
        <h3>Pricing{panels.pricing.company ? ` for ${panels.pricing.company}` : ""}</h3>
        <p className="sub">{panels.pricing.note}</p>
        <div className="tiers">
          {panels.pricing.tiers.map((tier) => (
            <div className="tier" key={tier.name}>
              <b>{tier.name}</b>
              <span className="tier-price">{tier.per_seat}</span>
              <span className="tiny muted">{tier.for}</span>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (active === "comparison" && panels.comparison) {
    return (
      <div className="stage-card">
        <h3>How she answers &ldquo;why you?&rdquo;</h3>
        <p className="sub">
          Every line here was written by the company that owns the agent. She is allowed to read
          it and talk around it, and not to invent a word of it.
        </p>
        <div className="versus">
          {panels.comparison.rivals.map((rival) => (
            <div className="versus-row" key={rival.name}>
              <div className="versus-head">
                <b>{panels.comparison?.company}</b> vs <span>{rival.name}</span>
                <p className="tiny muted">Good at: {rival.positioning}</p>
              </div>
              <ul className="fact-list">
                {rival.against.map((line) => (
                  <li key={line.dimension}>
                    <span className="tag">{line.dimension}</span> {line.ours}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (active === "quote" && panels.quote) {
    const quote = panels.quote;
    return (
      <div className="stage-card">
        <h3>Your number{quote.company ? `, ${quote.company}` : ""}</h3>
        <p className="sub">
          {quote.assumed
            ? "Sized from what she found about your business — tell her the real number and it updates."
            : `Sized from the ${quote.units} you mentioned.`}
        </p>
        <div className="quote">
          <div className="quote-total">
            <span className="quote-figure">{quote.total_display}</span>
            <span className="tiny muted">per {quote.period}</span>
          </div>
          <dl className="quote-lines">
            <div>
              <dt>Plan</dt>
              <dd>{quote.tier}</dd>
            </div>
            <div>
              <dt style={{ textTransform: "capitalize" }}>{quote.unit_plural}</dt>
              <dd>{quote.seats.toLocaleString()}</dd>
            </div>
            <div>
              <dt>Per {quote.unit_name}</dt>
              <dd>{quote.unit_display}</dd>
            </div>
            {quote.discount_display && (
              <div>
                <dt>Annual saving</dt>
                <dd>−{quote.discount_display}</dd>
              </div>
            )}
          </dl>
        </div>
        <p className="tiny muted" style={{ marginBlockStart: "var(--s-3)" }}>
          Every figure here is arithmetic on published pricing. The model never writes a number.
        </p>
      </div>
    );
  }

  if (active === "checkout" && panels.checkout) {
    const checkout = panels.checkout;
    return (
      <div className="stage-card stage-centred">
        <h3>Ready when you are</h3>
        <p style={{ fontSize: "var(--t-lg)" }}>
          {checkout.amount_display}
          {checkout.period ? ` per ${checkout.period}` : ""}
          {checkout.description ? ` — ${checkout.description}` : ""}
        </p>
        <a className="btn" data-variant="primary" href={checkout.url} target="_blank" rel="noreferrer">
          Open the checkout
        </a>
        <p className="tiny muted" style={{ marginBlockStart: "var(--s-3)" }}>
          The card is entered on the payment provider&rsquo;s own page. She never sees it, and
          neither does the transcript.
          {checkout.test_mode && " This one is the local mock — no money moves."}
        </p>
      </div>
    );
  }

  if (active === "booking" && panels.booking) {
    return (
      <div className="stage-card stage-centred">
        <span className="big-tick" aria-hidden>
          ✓
        </span>
        <h3>Booked</h3>
        <p style={{ fontSize: "var(--t-lg)" }}>{panels.booking.spoken}</p>
        <p className="tiny muted">Reference {panels.booking.booking_id}</p>
      </div>
    );
  }

  if (active === "draft" && panels.draft) {
    return (
      <div className="stage-card">
        <h3>The follow-up she wrote</h3>
        <p className="sub">
          {panels.draft.can_send
            ? "Ready to send."
            : `Drafted, not sent — ${panels.draft.why_not ?? "no mail server configured"}.`}
        </p>
        <p className="tiny muted">Subject: {panels.draft.subject}</p>
        <pre className="draft">{panels.draft.body}</pre>
      </div>
    );
  }

  if (panels.facts) {
    return (
      <div className="stage-card">
        <h3>
          What she found on {panels.facts.company || panels.facts.domain}
        </h3>
        <p className="sub">
          Read live, from their own pages, before she said a word — the part a rep would spend
          twenty minutes on.
        </p>
        <ul className="fact-list">
          {panels.facts.facts.map((fact) => (
            <li key={fact}>{fact}</li>
          ))}
        </ul>
        {panels.facts.pages_read && panels.facts.pages_read.length > 0 && (
          <p className="tiny muted" style={{ marginBlockStart: "var(--s-3)" }}>
            {panels.facts.pages_read.length} pages read
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="stage-card stage-centred">
      <p className="tiny muted">{panels.note?.text ?? "Listening…"}</p>
    </div>
  );
}
