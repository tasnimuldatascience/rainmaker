/**
 * The agent, as it appears on a customer's own website.
 *
 * A SEPARATE ENTRY POINT, NOT A PROP ON THE CONSOLE. This renders inside a 390-pixel iframe on
 * somebody else's marketing page, and almost nothing the console shows belongs there: no
 * pipeline, no latency strip, no engine badges, no research panel. A rep wants instruments; a
 * buyer wants a conversation. Sharing a component and hiding nine tenths of it with flags is how
 * both surfaces end up bad.
 *
 * What it does share is everything underneath — `LiveCall`, `Portrait`, the socket protocol. The
 * call is identical; only the room it happens in is different.
 *
 * THE VISITOR IS A STRANGER, which is the difference from the console and the reason several
 * things here are deliberately missing. There is no way to type an arbitrary "brief", no way to
 * pick an agent other than the one this page's key names, and no way to see what any other
 * tenant configured. The key in the URL selects a published agent and authorises nothing else.
 */

import { StrictMode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

import { Portrait } from "./components/Portrait";
import type { CallState, FrontDoor, Intake } from "./lib/call";
import { frontDoor } from "./lib/call";
import { LiveCall } from "./lib/call";
import "./styles/app.css";
import "./styles/embed.css";

function Widget() {
  const key = new URLSearchParams(window.location.search).get("key") ?? "";
  const call = useMemo(() => new LiveCall(key), [key]);
  const [state, setState] = useState<CallState | null>(null);
  const [draft, setDraft] = useState("");
  const [who, setWho] = useState<Intake>({ name: "", email: "", company: "" });
  const [door, setDoor] = useState<FrontDoor | null>(null);

  // WHOSE AGENT THIS IS, BEFORE THE FIRST FRAME. The widget is on somebody else's website, so
  // the name in the header and the fields in the form both belong to them, and both are needed
  // before a socket exists to carry them.
  useEffect(() => {
    let live = true;
    void frontDoor(key).then((found) => live && setDoor(found));
    return () => {
      live = false;
    };
  }, [key]);
  const log = useRef<HTMLDivElement | null>(null);

  useEffect(() => call.subscribe(setState), [call]);
  useEffect(() => () => call.hangUp(), [call]);
  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight, behavior: "smooth" });
  }, [state?.turns.length]);

  const start = useCallback(
    () =>
      void call.connect({
        name: who.name.trim(),
        email: who.email.trim(),
        company: who.company.trim(),
      }),
    [call, who],
  );

  const send = useCallback(() => {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    call.say(text);
  }, [call, draft]);

  if (!state) return null;
  const live = state.phase !== "idle" && state.phase !== "ended";

  if (!key) {
    return (
      <div className="w-shell w-centre">
        <p className="w-note">This widget was loaded without an agent key.</p>
      </div>
    );
  }

  return (
    <div className="w-shell">
      <header className="w-head">
        <span className="w-who">
          {live && <span className="w-dot" />}
          {(() => {
            const who = state.agent ?? door;
            return who?.name ? `${who.name} · ${who.company}` : "Talk to us";
          })()}
        </span>
        <button
          className="w-x"
          aria-label="Close"
          onClick={() => {
            call.hangUp();
            window.parent.postMessage({ rainmaker: "close" }, "*");
          }}
        >
          ×
        </button>
      </header>

      <div className="w-face" data-live={live}>
        <Portrait
          level={() => call.level()}
          mouth={() => call.mouthFrame()}
          speaking={state.speaking}
          listening={live && !state.speaking}
          fill
          src={state.agent?.portrait ?? door?.portrait ?? "/agent/liv.jpg"}
        />
      </div>
      {state.caption && <div className="w-caption">{state.caption}</div>}

      {!live ? (
        <div className="w-start">
          <p className="w-lead">
            Ask us anything — you&apos;ll be talking to an AI assistant, and it will say so.
          </p>
          {/* THE SAME THREE FIELDS AS THE CONSOLE, in a 380px box. Stacked, unlabelled and
              placeholder-led, because a widget that opens with a form has about four seconds
              before it is closed, and three short lines read faster than three labels. */}
          <input
            className="w-input"
            value={who.name}
            autoComplete="name"
            placeholder="Your name"
            aria-label="Your name"
            data-bad={state.intakeField === "name" || undefined}
            onChange={(e) => setWho({ ...who, name: e.target.value })}
            onKeyDown={(e) => e.key === "Enter" && start()}
          />
          <input
            className="w-input"
            type="email"
            value={who.email}
            autoComplete="email"
            placeholder={door && !door.require_work_email ? "Email" : "Work email"}
            aria-label="Your work email"
            data-bad={state.intakeField === "email" || undefined}
            onChange={(e) => setWho({ ...who, email: e.target.value })}
            onKeyDown={(e) => e.key === "Enter" && start()}
          />
          {(!door || door.ask_company) && (
            <input
              className="w-input"
              value={who.company}
              autoComplete="organization"
              placeholder="Company"
              aria-label="Your company"
              data-bad={state.intakeField === "company" || undefined}
              onChange={(e) => setWho({ ...who, company: e.target.value })}
              onKeyDown={(e) => e.key === "Enter" && start()}
            />
          )}
          <button
            className="w-go"
            onClick={start}
            disabled={state.phase === "connecting"}
          >
            {state.phase === "connecting" ? "Connecting…" : "Start the conversation"}
          </button>
          {(state.refused ?? state.error ?? state.intakeError) && (
            <p className="w-note">{state.refused ?? state.error ?? state.intakeError}</p>
          )}
        </div>
      ) : (
        <>
          <div className="w-log" ref={log}>
            {state.turns.map((turn, i) => (
              <p key={i} className="w-turn" data-who={turn.who}>
                {turn.text}
              </p>
            ))}
            {state.partial && <p className="w-turn w-partial">{state.partial}</p>}
          </div>

          <div className="w-bar">
            <input
              className="w-input"
              value={draft}
              placeholder={state.handsFree ? "Listening — or type" : "Type, or tap the mic"}
              aria-label="Say something"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
            />
            <button className="w-send" onClick={send} disabled={!draft.trim()}>
              Send
            </button>
            <button
              className="w-mic"
              data-on={state.handsFree}
              data-live={state.listening}
              disabled={!state.micSupported}
              title={
                state.micSupported
                  ? state.handsFree
                    ? "Turn the microphone off"
                    : "Talk instead of typing"
                  : "This browser has no speech recognition — typing works"
              }
              onClick={() => call.setHandsFree(!state.handsFree)}
            >
              🎤
            </button>
          </div>
        </>
      )}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Widget />
  </StrictMode>,
);
