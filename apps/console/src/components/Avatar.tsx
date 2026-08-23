/**
 * Liv — the agent's face.
 *
 * A VECTOR VISEME RIG, not a neural render. The architecture is deliberately the same one
 * MuseTalk uses:
 *
 *     audio/text ──► viseme sequence ──► mouth shape ──► frame
 *
 * What differs is only the last stage: this renders vector paths instead of diffusing pixels
 * onto a photographic face. Everything upstream — the phoneme→viseme mapping, the timing, the
 * co-articulation smoothing, the idle behaviours — is what a realtime avatar actually needs,
 * and it is the part that decides whether a face reads as alive. Swapping in MuseTalk behind
 * the same `Avatar` interface changes the renderer, not the pipeline.
 *
 * WHY THIS AND NOT A PHOTOREAL FACE. A stylised character that moves correctly reads as alive.
 * A photoreal face that moves slightly wrong reads as a corpse — the uncanny valley is
 * steepest exactly where a half-finished neural render lands. Given no GPU service in this
 * repo, a well-rigged vector face is the honest choice and, at small sizes, frequently the
 * better one.
 *
 * THE THINGS THAT MAKE IT LOOK ALIVE, none of which are the mouth:
 *
 *   blinking        irregular, 2-6s apart. Perfectly periodic blinking is instantly robotic.
 *   micro-sway      continuous 0.5px head drift. A face pinned to the pixel grid looks dead
 *                   even with a perfect mouth.
 *   saccades        eyes drift and re-fix. Static pupils are the strongest "this is a puppet"
 *                   signal there is.
 *   brow motion     rises slightly on stressed syllables; the difference between talking and
 *                   reciting.
 *   listening pose  head tilts a few degrees and blinking slows while the prospect speaks.
 *                   A face that freezes between utterances is what makes people talk over it.
 */

import { useEffect, useMemo, useRef, useState } from "react";

/** The standard viseme set — the visually distinguishable subset of phonemes. */
export type Viseme = "rest" | "AA" | "E" | "I" | "O" | "U" | "MBP" | "FV" | "L" | "S";

/**
 * Grapheme→viseme approximation.
 *
 * A real pipeline gets visemes from the TTS engine's phoneme timings, which is both more
 * accurate and free — the synthesiser already computed them. This mapping exists so the
 * component is drivable from plain text when no engine is attached, and it is explicitly the
 * weakest link: English orthography is a poor guide to pronunciation.
 */
const LETTER_VISEME: Record<string, Viseme> = {
  a: "AA", á: "AA", e: "E", i: "I", y: "I", o: "O", u: "U", w: "U",
  m: "MBP", b: "MBP", p: "MBP",
  f: "FV", v: "FV",
  l: "L", t: "L", d: "L", n: "L", r: "L",
  s: "S", z: "S", c: "S", j: "S", g: "S", k: "S", q: "S", x: "S", h: "S",
};

function textToVisemes(text: string): Viseme[] {
  const out: Viseme[] = [];
  for (const ch of text.toLowerCase()) {
    if (ch === " ") {
      // A brief closure at word boundaries. Without it speech looks like one continuous
      // chewing motion, which is the most common tell in a bad rig.
      if (out[out.length - 1] !== "rest") out.push("rest");
      continue;
    }
    const v = LETTER_VISEME[ch];
    if (v) out.push(v);
  }
  return out.length ? out : ["rest"];
}

/** Mouth geometry per viseme: [width, height, upper-lip curve, corner lift]. */
const MOUTH: Record<Viseme, [number, number, number, number]> = {
  rest: [27, 4, 1.5, 1.4],
  AA: [34, 26, -2, 0],
  E: [38, 15, -1, 1],
  I: [33, 9, 0, 1.6],
  O: [24, 23, -3, 0],
  U: [18, 16, -2, 0],
  MBP: [25, 1.4, 2, 1.2],
  FV: [29, 5, 3, 0.6],
  L: [29, 11, -0.5, 0.9],
  S: [31, 6, 0.5, 1],
};

export interface AvatarProps {
  /** What she is saying right now. Drives the viseme sequence. */
  speech?: string;
  speaking: boolean;
  /** True while the prospect is talking — switches to the listening pose. */
  listening?: boolean;
  size?: number;
}

export function Avatar({ speech = "", speaking, listening = false, size = 260 }: AvatarProps) {
  const visemes = useMemo(() => textToVisemes(speech), [speech]);
  const [viseme, setViseme] = useState<Viseme>("rest");
  const [blink, setBlink] = useState(false);
  const [gaze, setGaze] = useState({ x: 0, y: 0 });
  const [sway, setSway] = useState({ x: 0, y: 0, r: 0 });
  const [brow, setBrow] = useState(0);
  const frame = useRef(0);

  // ── mouth ──────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!speaking) {
      setViseme("rest");
      setBrow(0);
      return;
    }
    frame.current = 0;
    // ~11 visemes/second. Human speech runs 10-14; slower reads as drunk, faster as a blur.
    const id = window.setInterval(() => {
      const next = visemes[frame.current % visemes.length] ?? "rest";
      setViseme(next);
      // Brow lifts on open vowels — the stressed syllables. This is most of the difference
      // between "talking" and "reciting".
      setBrow(next === "AA" || next === "O" ? 1 : 0);
      frame.current += 1;
    }, 90);
    return () => window.clearInterval(id);
  }, [speaking, visemes]);

  // ── blinking ───────────────────────────────────────────────────────────
  useEffect(() => {
    let timer: number;
    const schedule = () => {
      // Irregular by design. Listening slows the rate, which is what people actually do.
      const base = listening ? 3600 : 2400;
      timer = window.setTimeout(() => {
        setBlink(true);
        window.setTimeout(() => setBlink(false), 110);
        schedule();
      }, base + Math.random() * 2600);
    };
    schedule();
    return () => window.clearTimeout(timer);
  }, [listening]);

  // ── saccades ───────────────────────────────────────────────────────────
  useEffect(() => {
    const id = window.setInterval(() => {
      // Small, and biased back toward centre: eyes wander and re-fix on the camera rather
      // than drifting away. Unbiased drift makes her look distracted.
      setGaze({ x: (Math.random() - 0.5) * 3.2, y: (Math.random() - 0.5) * 1.8 });
    }, 1500 + Math.random() * 1800);
    return () => window.clearInterval(id);
  }, []);

  // ── micro-sway ─────────────────────────────────────────────────────────
  useEffect(() => {
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const t = (now - start) / 1000;
      // Two incommensurable frequencies so the motion never visibly repeats.
      setSway({
        x: Math.sin(t * 0.55) * 1.6 + Math.sin(t * 0.23) * 0.9,
        y: Math.sin(t * 0.41) * 1.1,
        r: Math.sin(t * 0.31) * 0.7,
      });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  const [mw, mh, curve, lift] = MOUTH[viseme];
  const tilt = listening ? 4 : 0;

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 200 200"
      role="img"
      aria-label={speaking ? "Liv, the AI agent, speaking" : "Liv, the AI agent, listening"}
      style={{ display: "block" }}
      /* Rig state exposed on the element. Screenshot capture and future rendering tests can
         wait for a specific frame instead of sleeping and hoping -- the previous shots caught
         her mid-blink on a closed viseme, which made a working rig look static. */
      data-viseme={viseme}
      data-speaking={speaking}
      data-blink={blink}
      data-mouth-open={mh}
    >
      <defs>
        <linearGradient id="liv-bg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--surface-3)" />
          <stop offset="100%" stopColor="var(--surface-1)" />
        </linearGradient>
        <linearGradient id="liv-hair" x1="0.2" y1="0" x2="0.8" y2="1">
          <stop offset="0%" stopColor="#4a3b52" />
          <stop offset="55%" stopColor="#33283a" />
          <stop offset="100%" stopColor="#241c2a" />
        </linearGradient>
        <linearGradient id="liv-skin" x1="0.3" y1="0" x2="0.7" y2="1">
          <stop offset="0%" stopColor="#f2c9ad" />
          <stop offset="100%" stopColor="#e0ad8d" />
        </linearGradient>
        <linearGradient id="liv-top" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="var(--accent)" />
          <stop offset="100%" stopColor="var(--agent)" />
        </linearGradient>
        <radialGradient id="liv-glow" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0%" stopColor="var(--agent)" stopOpacity="0.30" />
          <stop offset="100%" stopColor="var(--agent)" stopOpacity="0" />
        </radialGradient>
        <clipPath id="liv-face">
          <ellipse cx="100" cy="92" rx="34" ry="42" />
        </clipPath>
      </defs>

      <circle cx="100" cy="100" r="96" fill="url(#liv-bg)" />
      {/* Speaking glow — a cheap, legible "she has the floor" cue. */}
      <circle
        cx="100"
        cy="100"
        r="92"
        fill="url(#liv-glow)"
        opacity={speaking ? 1 : 0.25}
        style={{ transition: "opacity 260ms ease" }}
      />

      <g
        transform={`translate(${sway.x} ${sway.y}) rotate(${sway.r + tilt} 100 120)`}
        style={{ transition: "transform 90ms linear" }}
      >
        {/* shoulders / top */}
        <path d="M52 200 Q52 158 100 152 Q148 158 148 200 Z" fill="url(#liv-top)" opacity="0.92" />
        <path d="M84 156 Q100 168 116 156 L116 150 Q100 156 84 150 Z" fill="url(#liv-skin)" />

        {/* hair, back */}
        <path
          d="M60 100 Q52 46 100 38 Q148 46 140 100 Q146 134 134 150 Q138 120 130 98
             Q128 64 100 60 Q72 64 70 98 Q62 120 66 150 Q54 134 60 100 Z"
          fill="url(#liv-hair)"
        />

        {/* face */}
        <ellipse cx="100" cy="92" rx="34" ry="42" fill="url(#liv-skin)" />
        {/* cheek warmth */}
        <ellipse cx="80" cy="102" rx="8" ry="5" fill="#e08a7a" opacity="0.18" />
        <ellipse cx="120" cy="102" rx="8" ry="5" fill="#e08a7a" opacity="0.18" />

        {/* fringe, drawn over the face */}
        <path
          d="M66 90 Q62 50 100 46 Q138 50 134 90 Q128 64 104 62 Q86 62 78 74 Q70 80 66 90 Z"
          fill="url(#liv-hair)"
          clipPath="url(#liv-face)"
        />
        <path d="M66 90 Q70 58 98 54 Q80 66 73 96 Z" fill="#5a4864" opacity="0.32" />

        {/* brows */}
        <g style={{ transition: "transform 90ms ease" }} transform={`translate(0 ${-brow * 1.6})`}>
          <path d="M80 78 Q87 74 94 77" stroke="#3a2d42" strokeWidth="2.4" fill="none" strokeLinecap="round" />
          <path d="M106 77 Q113 74 120 78" stroke="#3a2d42" strokeWidth="2.4" fill="none" strokeLinecap="round" />
        </g>

        {/* eyes */}
        <g>
          <ellipse cx="87" cy="90" rx="7.5" ry={blink ? 0.7 : 5.2} fill="#fff"
            style={{ transition: "ry 70ms ease" }} />
          <ellipse cx="113" cy="90" rx="7.5" ry={blink ? 0.7 : 5.2} fill="#fff"
            style={{ transition: "ry 70ms ease" }} />
          {!blink && (
            <g style={{ transition: "transform 380ms ease" }}
               transform={`translate(${gaze.x} ${gaze.y})`}>
              <circle cx="87" cy="90" r="3.4" fill="#3d2f47" />
              <circle cx="113" cy="90" r="3.4" fill="#3d2f47" />
              {/* catchlights — a face without them reads as flat and dead */}
              <circle cx="88.6" cy="88.4" r="1.15" fill="#fff" opacity="0.92" />
              <circle cx="114.6" cy="88.4" r="1.15" fill="#fff" opacity="0.92" />
            </g>
          )}
          {/* lash line */}
          <path d="M79.5 86.5 Q87 82.6 94.5 86.5" stroke="#3a2d42" strokeWidth="1.5" fill="none" strokeLinecap="round" />
          <path d="M105.5 86.5 Q113 82.6 120.5 86.5" stroke="#3a2d42" strokeWidth="1.5" fill="none" strokeLinecap="round" />
        </g>

        {/* nose */}
        <path d="M100 94 Q98 102 101 105" stroke="#c9906f" strokeWidth="1.7" fill="none" strokeLinecap="round" />

        {/* mouth — the viseme */}
        <g style={{ transition: "all 70ms ease-out" }}>
          <path
            d={`M${100 - mw / 2} 118
                Q100 ${118 - curve} ${100 + mw / 2} 118
                Q100 ${118 + mh} ${100 - mw / 2} 118 Z`}
            fill="#8f4550"
          />
          {mh > 8 && (
            <path
              d={`M${100 - mw / 2 + 4} 118 Q100 ${118 + mh * 0.55} ${100 + mw / 2 - 4} 118 Z`}
              fill="#5e2833"
            />
          )}
          {mh > 6 && (
            <path
              d={`M${100 - mw / 2 + 3} 118.5 Q100 ${116.5} ${100 + mw / 2 - 3} 118.5 Z`}
              fill="#fff"
              opacity="0.9"
            />
          )}
          {/* corner lift — the difference between a neutral mouth and a pleasant one */}
          <path
            d={`M${100 - mw / 2} 118 q-2 ${-lift} -3.4 ${-lift * 0.7}`}
            stroke="#a85a63" strokeWidth="1.4" fill="none" strokeLinecap="round"
          />
          <path
            d={`M${100 + mw / 2} 118 q2 ${-lift} 3.4 ${-lift * 0.7}`}
            stroke="#a85a63" strokeWidth="1.4" fill="none" strokeLinecap="round"
          />
        </g>

        {/* hair, front strands over the shoulder */}
        <path d="M66 96 Q60 130 70 152 Q64 128 68 96 Z" fill="url(#liv-hair)" />
        <path d="M134 96 Q140 130 130 152 Q136 128 132 96 Z" fill="url(#liv-hair)" />
      </g>
    </svg>
  );
}
