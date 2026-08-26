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
import type {
  CallState,
  FrontDoor,
  Intake as IntakeFields,
  Panels,
  Step,
  Turn,
} from "../lib/call";
import type { AgentRow } from "../lib/call";
import { frontDoor, publishedAgents } from "../lib/call";
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
  // WHICH AGENT ANSWERS IS A CHOICE, and the default is the one with something to sell. The
  // console used to always reach tenant zero, so the demo was Rainmaker selling Rainmaker —
  // recursive, and the weakest of the agents to show, because its own tour opens this console.
  // An agent that sells GPU-hours has a rate card, a competitor and a site to walk you round.
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [agentKey, setAgentKey] = useState("");
  const call = useMemo(() => new LiveCall(agentKey), [agentKey]);
  const [state, setState] = useState<CallState | null>(null);
  const [draft, setDraft] = useState("");
  const [who, setWho] = useState<IntakeFields>({ name: "", email: "", company: "" });
  const [door, setDoor] = useState<FrontDoor | null>(null);
  // The telemetry drawer. Closed by default: on a call the thing worth looking at is the call.
  const [showSide, setShowSide] = useState(false);

  useEffect(() => {
    let live = true;
    void publishedAgents().then((found) => {
      if (!live) return;
      setAgents(found);
      const best = found.find((agent) => agent.complete) ?? found[0];
      if (best) setAgentKey(best.key);
    });
    return () => {
      live = false;
    };
  }, []);

  // Re-asked whenever the agent changes: the fields, the face and the disclosure are theirs.
  useEffect(() => {
    let live = true;
    void frontDoor(agentKey).then((found) => live && setDoor(found));
    return () => {
      live = false;
    };
  }, [agentKey]);
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

  const sharing = live && state.active !== null && state.panels[state.active] !== undefined;
  // CAPTIONS SHOW WHAT IS BEING SAID NOW, NOT THE WHOLE TURN. The caption accumulates clause by
  // clause for the transcript's benefit, and rendering all of it overflowed the box and cut off
  // mid-word — the one part a viewer is actually reading. Trimmed to its tail, at a word
  // boundary, the way a caption track scrolls.
  const spoken = state.caption || (state.partial ? `${state.partial}…` : "");
  const caption =
    (spoken.length > 200 ? `…${spoken.slice(spoken.length - 200).replace(/^\S+\s/, "")}` : spoken) ||
    (state.phase === "thinking" ? "…" : "");

  if (!live) {
    return (
      <div className="lobby">
        <div className="lobby-preview">
          {agents.length > 1 && (
            <div className="lobby-switch" role="group" aria-label="Which agent answers">
              {agents.map((agent) => (
                <button
                  key={agent.key}
                  data-on={agent.key === agentKey}
                  onClick={() => setAgentKey(agent.key)}
                >
                  {agent.company}
                  {agent.sells && <span>per {agent.sells}</span>}
                </button>
              ))}
            </div>
          )}
          <div className="lobby-tile">
            <Portrait level={() => 0} speaking={false} listening={false} fill />
            <span className="pip-name">
              <span className="live-dot" />
              {door ? `${door.name} · ${door.company}` : "…"}
            </span>
          </div>
          <p className="lobby-note">
            {door?.disclosure ??
              "You will be talking to an AI. It says so before anything else, and that cannot be switched off."}
          </p>
        </div>

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
      </div>
    );
  }

  return (
    <div className="meet" data-side={showSide}>
      <div className="meet-stage">
        <div className="meet-top">
          <span className="meet-chip" data-live>
            <span className="live-dot" />
            {state.speaking ? "Speaking" : state.listening ? "Listening" : "On a call"}
          </span>
          {state.step && <StepRail step={state.step} />}
        </div>

        <div className="meet-main">
          {sharing ? (
            <Stage panels={state.panels} active={state.active} onPick={(i) => call.pickSlot(i)} />
          ) : (
            <div className="meet-hero">
              <Portrait
                level={() => call.level()}
                mouth={() => call.mouthFrame()}
                speaking={state.speaking}
                listening={!state.speaking}
                fill
              />
              <span className="pip-name">
                <span className="live-dot" />
                {state.agent ? `${state.agent.name} · ${state.agent.company}` : "Liv"}
              </span>
            </div>
          )}
        </div>

        {sharing && (
          <div className="meet-self" data-speaking={state.speaking}>
            <Portrait
              level={() => call.level()}
              mouth={() => call.mouthFrame()}
              speaking={state.speaking}
              listening={!state.speaking}
              fill
            />
            <span className="pip-name">
              <span className="live-dot" />
              {state.agent?.name ?? "Liv"}
            </span>
          </div>
        )}

        {caption && (
          <div className="meet-caption" data-partial={!state.caption || undefined}>
            {caption}
          </div>
        )}

        {(state.refused || state.error) && (
          <div className="meet-toast">{state.refused ?? state.error}</div>
        )}
      </div>

      {/* ── the control bar ─────────────────────────────────────────── */}
      <div className="meet-bar">
        <div className="meet-input">
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
            placeholder={
              state.handsFree ? "She's listening — or type" : "Type, or turn the microphone on"
            }
            aria-label="Say something to the agent"
          />
          <button className="meet-send" onClick={send} disabled={!draft.trim()}>
            Send
          </button>
        </div>

        <div className="meet-controls">
          <button
            className="meet-round"
            data-on={state.handsFree}
            data-live={state.listening}
            disabled={!state.micSupported}
            title={
              state.micSupported
                ? state.handsFree
                  ? "Turn the microphone off"
                  : "Leave the microphone open and just talk"
                : "This browser has no speech recognition — typing works"
            }
            onClick={() => call.setHandsFree(!state.handsFree)}
          >
            {state.handsFree ? "🎙" : "🎤"}
            <span>{state.handsFree ? "Mic on" : "Mic off"}</span>
          </button>

          <button className="meet-round" data-end onClick={() => call.hangUp()} title="End the call">
            ✕<span>End</span>
          </button>

          <button
            className="meet-round"
            data-on={showSide}
            onClick={() => setShowSide((open: boolean) => !open)}
            title="What is running, the latency budget, and the transcript"
          >
            ⓘ<span>Details</span>
          </button>
        </div>

        <div className="meet-flags">
          {state.booked && <span className="tag" data-tone="won">meeting booked</span>}
          {state.handoff && <span className="tag" data-tone="won">handing over</span>}
        </div>
      </div>

      {showSide && (
        <aside className="meet-side">
          <Instruments
            state={state}
            stages={stages}
            lastAgent={lastAgent}
            store={store}
            transcriptEnd={transcriptEnd}
          />
        </aside>
      )}
    </div>
  );
}

/**
 * The engineering read-out: what is running, where the time went, and what was said.
 *
 * A DRAWER RATHER THAN A COLUMN. On a call the thing worth looking at is the call, and three
 * cards of telemetry permanently beside it is a debugging surface with a face in the corner.
 * It is one button away and it stays where it was put.
 */
function Instruments({
  state,
  stages,
  lastAgent,
  store,
  transcriptEnd,
}: {
  state: CallState;
  stages: [string, number][];
  lastAgent: Turn | undefined;
  store: LocalStore;
  transcriptEnd: React.MutableRefObject<HTMLDivElement | null>;
}) {
  return (
    <>
      <div className="card">
        <h3>What is running</h3>
        <p className="sub">
          Reported by the server, not assumed. A demo that has quietly fallen back to the browser
          voice should say so.
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
          reading as broken.
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

      <div className="card meet-transcript">
        <h3>Transcript</h3>
        <p className="sub">
          Written to the deal record when the call ends — as CRDT ops, through the CRM tool
          server, so a rep&apos;s laptop sees it like any other edit.
        </p>
        {state.turns.length === 0 && <p className="tiny muted">Nothing yet.</p>}
        {state.turns.map((turn, i) => (
          <div className="turn" key={i}>
            <span className="turn-who" data-who={turn.who}>
              {turn.who === "agent" ? (state.agent?.name ?? "Liv") : "You"}
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
            Copy transcript into the deal
          </button>
        )}
      </div>
    </>
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
 * What research found, as a card rather than as a list of sentences.
 *
 * THIS IS THE FIRST THING A PROSPECT SEES AFTER TYPING THEIR EMAIL, and it was a bulleted dump
 * of "Company name: Stripe", "What they do: ...", "Technology: ..." — the schema's field names,
 * read out to the person whose company it is. The facts arrive as "Label: value" strings, so
 * they split cleanly, and a label belongs in a column rather than in the sentence.
 *
 * Ordered by what a seller would look at first: what they do, how big they are, what they run
 * on, what they are hiring for. Anything unrecognised keeps its place at the end rather than
 * being dropped — a fact nobody anticipated is still a fact she read.
 */
const FACT_ORDER = ["What they do", "Company size", "Technology", "Currently hiring", "Buying signal"];

function Dossier({ facts }: { facts: NonNullable<Panels["facts"]> }) {
  const rows = facts.facts
    .map((fact) => {
      const at = fact.indexOf(": ");
      // 40, not 28: "Currently hiring (2 open roles)" is a real label and it is 30 characters.
      return at > 0 && at < 40
        ? { label: fact.slice(0, at), value: fact.slice(at + 2) }
        : { label: "", value: fact };
    })
    .filter((row) => row.label !== "Company name")
    .sort((a, b) => {
      const rank = (label: string) => {
        const found = FACT_ORDER.indexOf(label);
        return found === -1 ? FACT_ORDER.length : found;
      };
      return rank(a.label) - rank(b.label);
    });

  return (
    <div className="stage-card dossier">
      <div className="dossier-head">
        <div>
          <span className="dossier-kicker">Read live, before she spoke</span>
          <h3>{facts.company || facts.domain}</h3>
          <p className="tiny muted">{facts.domain}</p>
        </div>
        <span className="dossier-count">
          <b>{facts.pages_read?.length ?? 0}</b>
          pages read
        </span>
      </div>

      <dl className="dossier-rows">
        {rows.map((row) => (
          <div key={row.value}>
            {row.label && <dt>{row.label}</dt>}
            <dd>{row.value}</dd>
          </div>
        ))}
      </dl>

      {rows.length === 0 && (
        <p className="tiny muted">
          Nothing worth repeating came back from their site. She will ask instead of guessing.
        </p>
      )}
    </div>
  );
}

/**
 * A page she is driving, shown the way a shared screen looks.
 *
 * IT SCROLLS IN FRONT OF YOU, WHICH IS THE WHOLE POINT. The browse tool returns the entire page
 * as one image plus the position within it that she is about to talk about, and this animates
 * from the top of the page down to that position. The earlier version cut straight to a still of
 * the right part of the page, which is a screenshot — and every person shown it said, correctly,
 * that the browser was not doing anything.
 *
 * The window is a viewport, not a picture frame: the image inside it is the full page and is
 * translated, so what you see is a page moving under a fixed window, exactly as it would in a
 * real screen share.
 */
function SharedScreen({ page }: { page: NonNullable<Panels["browser"]> }) {
  const viewport = useRef<HTMLDivElement | null>(null);
  const [offset, setOffset] = useState(0);

  const target = page.scroll_ratio ?? 0;
  const frame = page.frame;

  useEffect(() => {
    if (!frame || !page.full_page) {
      setOffset(0);
      return;
    }
    // A beat of the top of the page before it moves, so the destination is not the first thing
    // you see — the pause is what makes it read as "she is looking for something".
    setOffset(0);
    const start = window.setTimeout(() => setOffset(target), 700);
    return () => window.clearTimeout(start);
  }, [frame, page.full_page, target]);

  const shown = Math.round((page.viewport_ratio ?? 1) * 100);

  return (
    <div className="screen" ref={viewport}>
      <div className="screen-bar">
        <span className="screen-dots" aria-hidden>
          <i /> <i /> <i />
        </span>
        <span className="screen-url">{page.url}</span>
        {page.scrolled_to && <span className="tag">scrolled to “{page.scrolled_to}”</span>}
      </div>
      <div className="screen-viewport">
        {frame ? (
          <img
            className="screen-frame"
            data-scrolling={page.full_page || undefined}
            src={`data:image/jpeg;base64,${frame}`}
            alt={page.title || page.url}
            style={
              page.full_page
                ? {
                    // The image is the whole page; `shown`% of it is one screenful.
                    blockSize: `${(100 / Math.max(shown, 1)) * 100}%`,
                    transform: `translateY(${-offset * 100}%)`,
                  }
                : undefined
            }
          />
        ) : (
          <div className="screen-loading">
            <span className="screen-spinner" aria-hidden />
            Opening {page.url}…
          </div>
        )}
      </div>
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
    return <SharedScreen page={panels.browser} />;
  }

  if (active === "slots" && panels.slots) {
    return (
      <div className="stage-card">
        <h3>Pick a time and she&apos;ll book it</h3>
        <p className="sub">
          Read straight from the calendar tool, in its own words. She is not allowed to invent
          one — and clicking is what confirms it, not saying yes.
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
    return <Dossier facts={panels.facts} />;
  }

  return (
    <div className="stage-card stage-centred">
      <p className="tiny muted">{panels.note?.text ?? "Listening…"}</p>
    </div>
  );
}
