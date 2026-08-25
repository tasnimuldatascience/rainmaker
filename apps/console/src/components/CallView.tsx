/**
 * The live call.
 *
 * WHAT CHANGED HERE, because the file's history is the interesting part: this view used to play
 * a hardcoded `SCRIPT` of four exchanges on `setTimeout`, and animate the mouth at 52ms per
 * character. Everything on screen was a drawing of a call. It is now a real one — the prospect
 * types or holds the talk button, a 1.5B model on this machine answers, Kokoro speaks the reply
 * a clause at a time, and the face moves to the audio that is actually playing.
 *
 * THE LAYOUT IS THE PRODUCT SURFACE, NOT A DEBUGGING ONE. A prospect meets this agent on a page
 * with a headline and a small floating window, so that is what is rendered: the demo IS the
 * call. The measurements a reviewer wants — which engines are loaded, where the 800ms went —
 * sit beside it rather than in the middle of it.
 *
 * THE MICROPHONE IS HOLD-TO-TALK on purpose. Open-mic voice agents have to guess when a person
 * has finished a sentence, and every guess is either an interruption or a pause. Holding a
 * button removes the guess entirely, which is the same reason River's own demo does it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CallState } from "../lib/call";
import { LiveCall } from "../lib/call";
import type { LocalStore } from "../lib/store";
import { Avatar } from "./Avatar";

const STAGE_COLOR: Record<string, string> = {
  stt: "var(--accent)",
  llm: "var(--agent)",
  tts: "var(--info)",
  avatar: "var(--won)",
};

const BUDGET_MS = 800;
const STAGE_ORDER = ["stt", "llm", "tts", "avatar"];

/**
 * The research the agent is briefed with.
 *
 * Written by the Research panel and read here. Deliberately localStorage rather than lifted
 * component state: the brief should survive a reload the same way the pipeline does, and a
 * rep who researched a company yesterday should not have to do it again to call them.
 */
function readBrief(): { company: string; domain: string; enrichment: unknown } | undefined {
  try {
    const raw = localStorage.getItem("rainmaker-last-brief");
    return raw ? JSON.parse(raw) : undefined;
  } catch {
    return undefined;
  }
}

export function CallView({ store }: { store: LocalStore }) {
  const call = useMemo(() => new LiveCall(), []);
  const [state, setState] = useState<CallState | null>(null);
  const [draft, setDraft] = useState("");
  const transcriptEnd = useRef<HTMLDivElement | null>(null);

  useEffect(() => call.subscribe(setState), [call]);

  // Leaving the view ends the call. Without this the socket stays open, the agent keeps
  // talking into a component that no longer exists, and the next visit joins mid-sentence.
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
  const brief = readBrief();
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
            The agent says it is an AI before anything else, answers from what research actually
            read, and stops selling the moment someone asks for a person. Type, or hold the talk
            button.
          </p>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1.35fr) minmax(0, 1fr)",
          gap: "var(--s-5)",
          alignItems: "start",
        }}
      >
        {/* ── the stage ───────────────────────────────────────────────── */}
        <div className="demo" data-live={live}>
          <span className="pill">
            <i />
            {live ? "Live demo" : "Demo"}
          </span>

          <div className="hero">
            <h2>
              Meet <span>Liv</span>.
            </h2>
            <p>
              The AI account executive that answers every buyer the moment they reach out. This
              call is the demo.
            </p>

            <div className="presenter">
              <span className="presenter-face">
                <Avatar speaking={false} listening size={44} />
              </span>
              <span className="presenter-meta">
                <small>Presented by</small>
                <b>
                  Liv · Rainmaker
                  {live && <span className="live-dot" aria-label="on the call" />}
                </b>
              </span>
            </div>
          </div>

          <div className="caption" data-empty={!state.caption && !state.partial}>
            {state.caption ||
              (state.partial && <em style={{ color: "var(--fg-3)" }}>{state.partial}</em>) ||
              (live
                ? state.phase === "thinking"
                  ? "…"
                  : "Ask her anything — pricing, security, how the offline part works."
                : "Start the call and she introduces herself.")}
          </div>

          <div className="composer">
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
              placeholder={
                live ? "Type below, or hold the talk button to speak" : "Start the call first"
              }
              disabled={!live}
              aria-label="Say something to the agent"
            />
            <button className="btn" onClick={send} disabled={!live || !draft.trim()}>
              Send
            </button>
            <button
              className="talk"
              data-listening={state.listening}
              disabled={!live || !state.micSupported}
              title={
                state.micSupported
                  ? "Hold to talk. Chrome sends the audio to Google to transcribe it; typing stays on this machine."
                  : "This browser has no speech recognition. Typing works everywhere."
              }
              onPointerDown={() => call.startListening()}
              onPointerUp={() => call.stopListening()}
              onPointerLeave={() => state.listening && call.stopListening()}
            >
              {state.listening ? <i /> : "🎤"}
              {state.listening ? "Listening" : "Hold to talk"}
            </button>
          </div>

          <div className="pip" data-speaking={state.speaking}>
            <Avatar
              speech={state.caption}
              speaking={state.speaking}
              listening={live && !state.speaking}
              size={190}
            />
            <span className="pip-menu" aria-hidden>
              •••
            </span>
            <span className="pip-name">
              {live && <span className="live-dot" />}
              Liv · Rainmaker
            </span>
          </div>
        </div>

        {/* ── the instruments ─────────────────────────────────────────── */}
        <div className="col-flex" style={{ gap: "var(--s-4)" }}>
          <div className="row">
            {!live ? (
              <button
                className="btn"
                data-variant="primary"
                onClick={() => void call.connect(brief)}
              >
                Start call
              </button>
            ) : (
              <button className="btn" onClick={() => call.hangUp()}>
                End call
              </button>
            )}
            {state.handoff && (
              <span className="tag" data-tone="won">
                handed off to a human
              </span>
            )}
            {brief && !live && (
              <span className="tiny muted">briefed on {brief.company || brief.domain}</span>
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
              <p className="tiny muted">Start the call to see which engines loaded.</p>
            )}
          </div>

          <div className="card">
            <h3>Turn latency budget</h3>
            <p className="sub">
              Measured per stage. Past {BUDGET_MS}ms a pause stops reading as thinking and starts
              reading as broken. Transcription and the face are timed in the browser; the model
              and the voice on the server. Typing has no transcription stage because nothing was
              transcribed.
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
                        color:
                          (lastAgent?.total ?? 0) <= BUDGET_MS ? "var(--won)" : "var(--risk)",
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

          <div className="card" style={{ maxBlockSize: "42vh", overflow: "auto" }}>
            <h3>Transcript</h3>
            <p className="sub">
              Written to the deal record as CRDT ops, so it is on the rep&apos;s device before the
              call ends.
            </p>
            {state.turns.length === 0 && (
              <p className="tiny muted">Nothing yet — start the call.</p>
            )}
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
                Save transcript to Corvus Data
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
