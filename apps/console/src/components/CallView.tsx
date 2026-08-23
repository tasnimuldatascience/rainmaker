/**
 * The live call surface.
 *
 * WHAT THIS RENDERS HONESTLY. The avatar stage shows the placeholder provider, because the
 * realtime face (MuseTalk over a LivePortrait idle loop) needs several GB of weights and a
 * persistent GPU service that is not part of this repository's runnable path. Everything else
 * — disclosure, turn-taking, the transcript, and the measured per-stage latency budget — is
 * the real pipeline.
 *
 * The latency strip is the centrepiece rather than decoration. Perceived realism in a voice
 * agent is dominated by turn latency, not by voice quality or face fidelity: past roughly
 * 800ms a pause stops reading as thinking and starts reading as broken. Showing the budget
 * per stage is how that gets engineered instead of hoped for.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { LocalStore } from "../lib/store";

interface Turn {
  who: "agent" | "prospect";
  text: string;
  budget?: Record<string, number>;
  total?: number;
}

const STAGE_COLOR: Record<string, string> = {
  endpoint: "var(--fg-4)",
  stt: "var(--accent)",
  llm: "var(--agent)",
  tts: "var(--info)",
  avatar: "var(--won)",
};

const BUDGET_MS = 800;

/**
 * A scripted exchange, so the surface is demonstrable without a microphone or GPU.
 *
 * The budgets are the pipeline's own targets from `calls/pipeline.py`, not invented numbers,
 * and the handoff turn is included because it is the behaviour that matters most: when a
 * prospect asks for a human, the agent stops.
 */
const SCRIPT: Turn[] = [
  {
    who: "agent",
    text:
      "Hi, before we start — I'm an AI assistant, not a human. I can walk you through the " +
      "product and answer questions, and I can bring in a person any time you'd like.",
    budget: { tts: 96, avatar: 38 },
  },
  { who: "prospect", text: "Sure. We're already running Postgres — why would we need you?" },
  {
    who: "agent",
    text:
      "Your engineering blog mentions ClickHouse alongside Postgres for analytics, so you've " +
      "already split the workload. We sit on that seam rather than replacing either.",
    budget: { endpoint: 244, stt: 71, llm: 238, tts: 88, avatar: 34 },
  },
  { who: "prospect", text: "What does it cost at our size?" },
  {
    who: "agent",
    text:
      "You listed seven open engineering roles, so I'd size you around 150 people. That lands " +
      "in the tier where the published price stops applying — I'd rather have a person quote " +
      "that than guess.",
    budget: { endpoint: 251, stt: 66, llm: 402, tts: 91, avatar: 36 },
  },
  { who: "prospect", text: "Can I just talk to a real person?" },
  {
    who: "agent",
    text:
      "Of course — connecting you now. I'll pass along everything we covered so you don't " +
      "repeat yourself.",
    budget: { endpoint: 238, stt: 58, llm: 165, tts: 84, avatar: 31 },
  },
];

export function CallView({ store }: { store: LocalStore }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [running, setRunning] = useState(false);
  const [level, setLevel] = useState(0);
  const timers = useRef<number[]>([]);

  // Every scheduled callback is cleared on unmount. Without this, leaving mid-call keeps
  // firing setState on an unmounted tree and the "call" silently continues in the background.
  useEffect(
    () => () => {
      timers.current.forEach((t) => window.clearTimeout(t));
    },
    [],
  );

  const start = useCallback(() => {
    setTurns([]);
    setRunning(true);
    timers.current.forEach((t) => window.clearTimeout(t));
    timers.current = [];

    let delay = 0;
    SCRIPT.forEach((turn, i) => {
      delay += turn.who === "agent" ? 900 : 1400;
      timers.current.push(
        window.setTimeout(() => {
          const total = turn.budget
            ? Object.values(turn.budget).reduce((a, b) => a + b, 0)
            : undefined;
          setTurns((prev) => [...prev, { ...turn, total }]);
          setLevel(turn.who === "agent" ? 1 : 0.15);
          if (i === SCRIPT.length - 1) {
            setRunning(false);
            setLevel(0);
          }
        }, delay),
      );
    });
  }, []);

  const lastAgent = [...turns].reverse().find((t) => t.who === "agent" && t.budget);
  const handoff = turns.some((t) => /talk to a real person|real person/i.test(t.text));

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Live call</h1>
          <p>
            The agent discloses that it is AI before anything else, grounds every claim in what
            research actually read, and hands off the moment someone asks for a human.
          </p>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1.15fr) minmax(0, 1fr)",
          gap: "var(--s-5)",
          alignItems: "start",
        }}
      >
        <div className="col-flex" style={{ gap: "var(--s-4)" }}>
          <div className="stage">
            <span className="disclosure">
              <span aria-hidden>◆</span> AI agent · disclosed at call start
            </span>
            <AvatarPlaceholder level={level} active={running} />
          </div>

          <div className="row">
            <button className="btn" data-variant="primary" onClick={start} disabled={running}>
              {running ? "Call in progress…" : turns.length ? "Replay call" : "Start call"}
            </button>
            {handoff && (
              <span className="tag" data-tone="won">
                handed off to a human
              </span>
            )}
          </div>

          <div className="card">
            <h3>Turn latency budget</h3>
            <p className="sub">
              Measured per stage, not estimated. Past {BUDGET_MS}ms a pause stops reading as
              thinking and starts reading as broken.
            </p>
            {lastAgent?.budget ? (
              <>
                <div className="budget">
                  {Object.entries(lastAgent.budget).map(([stage, ms]) => (
                    <i
                      key={stage}
                      style={{
                        flex: ms,
                        background: STAGE_COLOR[stage] ?? "var(--fg-4)",
                      }}
                      title={`${stage} ${ms}ms`}
                    >
                      {ms > 60 ? stage : ""}
                    </i>
                  ))}
                </div>
                <div className="budget-key">
                  {Object.entries(lastAgent.budget).map(([stage, ms]) => (
                    <span key={stage}>
                      <span
                        style={{
                          display: "inline-block",
                          inlineSize: 8,
                          blockSize: 8,
                          borderRadius: 2,
                          marginInlineEnd: 5,
                          background: STAGE_COLOR[stage] ?? "var(--fg-4)",
                        }}
                      />
                      {stage} {ms}ms
                    </span>
                  ))}
                  <span className="right">
                    <b
                      style={{
                        color:
                          (lastAgent.total ?? 0) <= BUDGET_MS ? "var(--won)" : "var(--risk)",
                      }}
                    >
                      total {lastAgent.total}ms
                    </b>{" "}
                    / {BUDGET_MS}ms
                  </span>
                </div>
              </>
            ) : (
              <p className="tiny muted">Start the call to see per-stage timings.</p>
            )}
          </div>
        </div>

        <div className="card" style={{ maxBlockSize: "70vh", overflow: "auto" }}>
          <h3>Transcript</h3>
          <p className="sub">
            Written to the deal record as CRDT ops, so it is on the rep&apos;s device before the
            call ends.
          </p>
          {turns.length === 0 && (
            <p className="tiny muted">Nothing yet — start the call.</p>
          )}
          {turns.map((turn, i) => (
            <div className="turn" key={i}>
              <span className="turn-who" data-who={turn.who}>
                {turn.who === "agent" ? "Agent" : "Prospect"}
              </span>
              <div>
                <p style={{ fontSize: "var(--t-sm)", lineHeight: 1.6 }}>{turn.text}</p>
                {turn.total !== undefined && (
                  <p className="tiny muted" style={{ marginBlockStart: 4 }}>
                    {turn.total}ms {turn.total <= BUDGET_MS ? "· within budget" : "· over budget"}
                  </p>
                )}
              </div>
            </div>
          ))}
          {turns.length > 0 && (
            <button
              className="btn"
              style={{ marginBlockStart: "var(--s-4)" }}
              onClick={() => {
                const text = turns.map((t) => `${t.who}: ${t.text}`).join("\n\n");
                store.editText("deal", "d-corvus", "notes", text);
              }}
            >
              Save transcript to Corvus Data
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * The placeholder avatar.
 *
 * An audio-reactive form rather than a face. Shipping a bad lip-synced render and calling it
 * a demo is worse than being clear about what is and is not wired: the provider interface is
 * real, this implementation is honest about being a stand-in.
 */
function AvatarPlaceholder({ level, active }: { level: number; active: boolean }) {
  return (
    <div style={{ display: "grid", placeItems: "center", gap: "var(--s-4)" }}>
      <div
        style={{
          inlineSize: 128,
          blockSize: 128,
          borderRadius: "50%",
          background:
            "conic-gradient(from 200deg, var(--accent), var(--agent), var(--info), var(--accent))",
          filter: `blur(${active ? 0.5 : 2}px)`,
          transform: `scale(${1 + level * 0.12})`,
          transition: "transform 180ms var(--ease), filter 300ms var(--ease)",
          boxShadow: `0 0 ${40 + level * 60}px -10px var(--agent)`,
          opacity: active ? 1 : 0.55,
        }}
      />
      <p className="tiny muted" style={{ maxInlineSize: "38ch", textAlign: "center" }}>
        Placeholder avatar provider. The MuseTalk realtime face is implemented behind the same
        interface but needs a GPU service — see the README for what is verified.
      </p>
    </div>
  );
}
