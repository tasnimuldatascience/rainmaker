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
  /**
   * Current output loudness, 0..1, polled every frame.
   *
   * THE SHAPE IS A GUESS AND THE OPENING IS NOT. Visemes derived from spelling are the weakest
   * link in this rig — English orthography barely predicts pronunciation — but the amplitude of
   * the audio actually playing is measured, and it is the half a viewer notices. Driving the
   * opening from it means the mouth moves when there is sound and stops when there is not,
   * which is the difference between a face talking and a face chewing.
   */
  level?: () => number;
  /** Fill the parent instead of taking a fixed size. */
  fill?: boolean;
}

export function Avatar({
  speech = "",
  speaking,
  listening = false,
  size = 260,
  level,
  fill = false,
}: AvatarProps) {
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

  const [mw, mhShape, curve, lift] = MOUTH[viseme];

  // The measured loudness, smoothed. Read on an animation frame and kept in a ref so the SVG
  // re-renders on the viseme tick rather than sixty times a second.
  const loud = useRef(0);
  useEffect(() => {
    if (!level) return;
    let frame = 0;
    const tick = () => {
      const now = level();
      // Asymmetric smoothing: a mouth opens faster than it closes, and a symmetric filter makes
      // speech look like it is being mumbled underwater.
      loud.current += (now - loud.current) * (now > loud.current ? 0.55 : 0.18);
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [level]);

  // Amplitude scales the opening between a quarter of the viseme's height and slightly past it,
  // so a loud vowel is visibly wider than a quiet one and silence closes the mouth completely.
  const gain = level ? 0.25 + Math.min(loud.current, 1) * 0.95 : 1;
  const mh = speaking ? Math.max(mhShape * gain, 1.2) : mhShape;
  const tilt = listening ? 4 : 0;

  return (
    <svg
      width={fill ? "100%" : size}
      height={fill ? "100%" : size}
      viewBox="0 0 200 200"
      preserveAspectRatio="xMidYMid slice"
      role="img"
      aria-label={speaking ? "Liv, the AI agent, speaking" : "Liv, the AI agent, listening"}
      style={{ display: "block", background: "#171320" }}
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
          <stop offset="0%" stopColor="#242c40" />
          <stop offset="100%" stopColor="#131722" />
        </linearGradient>
        <linearGradient id="liv-hair" x1="0.15" y1="0" x2="0.9" y2="1">
          <stop offset="0%" stopColor="#57424b" />
          <stop offset="45%" stopColor="#3b2c33" />
          <stop offset="100%" stopColor="#251b20" />
        </linearGradient>
        <linearGradient id="liv-skin" x1="0.2" y1="0" x2="0.8" y2="1">
          <stop offset="0%" stopColor="#f3cdb0" />
          <stop offset="100%" stopColor="#dcaa8a" />
        </linearGradient>
        <linearGradient id="liv-jacket" x1="0.1" y1="0" x2="0.9" y2="1">
          <stop offset="0%" stopColor="#3b4664" />
          <stop offset="100%" stopColor="#1f2534" />
        </linearGradient>
        <linearGradient id="liv-shell" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#aab5cc" />
          <stop offset="100%" stopColor="#8d99b4" />
        </linearGradient>
        <radialGradient id="liv-cheek" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0%" stopColor="#d98474" stopOpacity="0.20" />
          <stop offset="100%" stopColor="#d98474" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="liv-glow" cx="0.5" cy="0.42" r="0.55">
          <stop offset="0%" stopColor="var(--agent)" stopOpacity="0.22" />
          <stop offset="100%" stopColor="var(--agent)" stopOpacity="0" />
        </radialGradient>
        <clipPath id="liv-face">
          {/* Must match the face path below: the fringe is drawn over the face and clipped to
              it, so the hairline is a curve rather than a helmet edge. */}
          <path d="M100 55 C81 55 72 70 72 91 C72 112 83 132 100 138 C117 132 128 112 128 91 C128 70 119 55 100 55 Z" />
        </clipPath>
      </defs>

      <rect x="0" y="0" width="200" height="200" fill="url(#liv-bg)" />
      {/* Speaking glow — a cheap, legible "she has the floor" cue. */}
      <rect
        x="0"
        y="0"
        width="200"
        height="200"
        fill="url(#liv-glow)"
        opacity={speaking ? 1 : 0.3}
        style={{ transition: "opacity 260ms ease" }}
      />

      <g
        transform={`translate(${sway.x} ${sway.y}) rotate(${sway.r + tilt} 100 130)`}
        style={{ transition: "transform 90ms linear" }}
      >
        {/* HAIR, BACK. A bob with volume at the crown, turning in at the jaw. It used to fall
            straight past the shoulders on both sides, which reads as a curtain rather than as a
            haircut, and left two pointed strands hanging in front of the collar. */}
        <path
          d="M100 34 C72 34 60 56 61 84 C62 100 60 116 63 130 C66 142 74 150 84 152
             C74 142 72 130 72 118 C72 100 70 88 74 78 C79 62 87 56 100 56
             C113 56 121 62 126 78 C130 88 128 100 128 118 C128 130 126 142 116 152
             C126 150 134 142 137 130 C140 116 138 100 139 84 C140 56 128 34 100 34 Z"
          fill="url(#liv-hair)"
        />

        {/* NECK — SHE DID NOT HAVE ONE. A head sat straight on a collar, and that single missing
            shape is most of why she read as a sticker rather than a person: the eye takes in the
            head-neck-shoulder line before it takes in a face. The shadow is cast by the jaw,
            not painted as a band across the throat, which is what the first attempt did and it
            looked like a collar sitting on a block. */}
        <path d="M88 112 C88 128 86 138 82 145 C90 152 110 152 118 145 C114 138 112 128 112 112 Z" fill="url(#liv-skin)" />
        <ellipse cx="100" cy="118" rx="15" ry="9" fill="#b07a58" opacity="0.30" />

        {/* SHOULDERS. A jacket, and its collar crosses in front of the neck base so the head
            sits INTO the clothes rather than balancing on them. The shell is muted rather than
            white: at tile size a bright triangle took the eye straight off her face. */}
        <path d="M100 143 C133 144 158 157 164 200 L36 200 C42 157 67 144 100 143 Z" fill="url(#liv-jacket)" />
        <path d="M100 154 L110 200 L90 200 Z" fill="url(#liv-shell)" />
        <path d="M78 148 C86 143 94 141 100 141 L100 152 C94 154 90 158 88 164 L82 200 L66 200 Z" fill="url(#liv-jacket)" />
        <path d="M122 148 C114 143 106 141 100 141 L100 152 C106 154 110 158 112 164 L118 200 L134 200 Z" fill="url(#liv-jacket)" />
        <path d="M78 148 C86 143 94 141 100 141 L100 145 C93 148 89 154 87 162 L82 200 L76 200 L82 158 C84 152 87 150 78 148 Z" fill="#000" opacity="0.20" />
        <path d="M122 148 C114 143 106 141 100 141 L100 145 C107 148 111 154 113 162 L118 200 L124 200 L118 158 C116 152 113 150 122 148 Z" fill="#fff" opacity="0.06" />

        {/* face */}
        <path
          d="M100 55 C81 55 72 70 72 91 C72 112 83 132 100 138 C117 132 128 112 128 91 C128 70 119 55 100 55 Z"
          fill="url(#liv-skin)"
        />
        <ellipse cx="82" cy="104" rx="9" ry="6" fill="url(#liv-cheek)" />
        <ellipse cx="118" cy="104" rx="9" ry="6" fill="url(#liv-cheek)" />

        {/* ears — part of why a head reads as a head rather than as an oval */}
        <ellipse cx="71.6" cy="96" rx="3.4" ry="5.6" fill="url(#liv-skin)" />
        <ellipse cx="128.4" cy="96" rx="3.4" ry="5.6" fill="url(#liv-skin)" />

        {/* fringe: a side sweep, clipped to the face */}
        <path
          d="M69 94 C67 62 82 50 100 50 C120 50 132 61 131 92
             C127 73 117 65 102 66 C90 67 80 74 74 86 C71 90 70 92 69 94 Z"
          fill="url(#liv-hair)"
          clipPath="url(#liv-face)"
        />

        {/* brows */}
        <g style={{ transition: "transform 90ms ease" }} transform={`translate(0 ${-brow * 1.5})`}>
          <path d="M82.5 82.4 Q88.5 78.6 94.8 81" stroke="#33262c" strokeWidth="2.1" fill="none" strokeLinecap="round" />
          <path d="M105.2 81 Q111.5 78.6 117.5 82.4" stroke="#33262c" strokeWidth="2.1" fill="none" strokeLinecap="round" />
        </g>

        {/* eyes — smaller and set wider than the old rig's, which is most of "less cartoon" */}
        <g>
          <ellipse cx="88.5" cy="92.5" rx="6" ry={blink ? 0.6 : 4} fill="#fdfbf9"
            style={{ transition: "ry 70ms ease" }} />
          <ellipse cx="111.5" cy="92.5" rx="6" ry={blink ? 0.6 : 4} fill="#fdfbf9"
            style={{ transition: "ry 70ms ease" }} />
          {!blink && (
            <g style={{ transition: "transform 380ms ease" }}
               transform={`translate(${gaze.x} ${gaze.y})`}>
              <circle cx="88.5" cy="92.5" r="2.8" fill="#4d392f" />
              <circle cx="111.5" cy="92.5" r="2.8" fill="#4d392f" />
              <circle cx="88.5" cy="92.5" r="1.3" fill="#251a15" />
              <circle cx="111.5" cy="92.5" r="1.3" fill="#251a15" />
              {/* catchlights — a face without them reads as flat and dead */}
              <circle cx="89.7" cy="91.3" r="0.95" fill="#fff" opacity="0.95" />
              <circle cx="112.7" cy="91.3" r="0.95" fill="#fff" opacity="0.95" />
            </g>
          )}
          {/* upper lash line, heavier than the lower — the way an eye actually reads */}
          <path d="M82.4 90.4 Q88.5 87.1 94.6 90.4" stroke="#33262c" strokeWidth="1.6" fill="none" strokeLinecap="round" />
          <path d="M105.4 90.4 Q111.5 87.1 117.6 90.4" stroke="#33262c" strokeWidth="1.6" fill="none" strokeLinecap="round" />
        </g>

        {/* nose — a shadow down one side and a base, rather than a hook */}
        <path d="M98.2 99 C97.5 105 97.4 108 99 109.4" stroke="#c78e6a" strokeWidth="1.3" fill="none" strokeLinecap="round" opacity="0.7" />
        <path d="M97.2 110.2 Q100 111.8 102.8 110.2" stroke="#c78e6a" strokeWidth="1.1" fill="none" strokeLinecap="round" opacity="0.45" />

        {/* mouth — the viseme. `mw`, `mh`, `curve` and `lift` are the rig; only the palette
            changed here. */}
        <g style={{ transition: "all 70ms ease-out" }}>
          <path
            d={`M${100 - mw / 2} 118.5
                Q100 ${118.5 - curve} ${100 + mw / 2} 118.5
                Q100 ${118.5 + mh} ${100 - mw / 2} 118.5 Z`}
            fill="#ab5f65"
          />
          {mh > 8 && (
            <path
              d={`M${100 - mw / 2 + 4} 118.5 Q100 ${118.5 + mh * 0.55} ${100 + mw / 2 - 4} 118.5 Z`}
              fill="#5e2833"
            />
          )}
          {mh > 6 && (
            <path
              d={`M${100 - mw / 2 + 3} 119 Q100 ${117} ${100 + mw / 2 - 3} 119 Z`}
              fill="#fff"
              opacity="0.92"
            />
          )}
          {/* corner lift — the difference between a neutral mouth and a pleasant one */}
          <path
            d={`M${100 - mw / 2} 118.5 q-1.4 ${-lift} -2.6 ${-lift * 0.7}`}
            stroke="#b56d73" strokeWidth="1.1" fill="none" strokeLinecap="round"
          />
          <path
            d={`M${100 + mw / 2} 118.5 q1.4 ${-lift} 2.6 ${-lift * 0.7}`}
            stroke="#b56d73" strokeWidth="1.1" fill="none" strokeLinecap="round"
          />
        </g>

        {/* hair over the shoulder, which is what puts the head IN the scene rather than on it */}
        <path d="M72 118 C71 134 74 146 84 152 C76 154 68 148 65 138 C63 130 64 122 66 116 Z" fill="url(#liv-hair)" />
        <path d="M128 118 C129 134 126 146 116 152 C124 154 132 148 135 138 C137 130 136 122 134 116 Z" fill="url(#liv-hair)" />
      </g>
    </svg>
  );
}
