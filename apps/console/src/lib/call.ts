/**
 * The live call, client side: one socket, one audio queue, one microphone.
 *
 * THE AGENT ALWAYS TALKS. Whether the prospect typed or spoke, the reply comes back as audio —
 * from Kokoro when the server has it, from the browser's own `speechSynthesis` when it does
 * not. The two paths are unified here rather than in the UI, so nothing above this file has to
 * know which voice is running, and a machine without the weights still holds a conversation.
 *
 * PLAYBACK IS SCHEDULED, NOT CHAINED. Clips arrive one clause at a time while the model is
 * still generating, and playing them with `audio.onended -> play next` leaves an audible gap at
 * every clause boundary: the agent sounds like it is reading a list. Web Audio lets each clip be
 * scheduled at the exact sample the previous one ends, so a reply synthesised in six pieces is
 * heard as one sentence.
 *
 * THE MOUTH IS DRIVEN BY THE AUDIO THAT IS ACTUALLY PLAYING. Every caption change and every
 * `speaking` flag is fired from a timer anchored to the scheduled start of a clip, not from the
 * moment a message arrived. Before this the console animated the face on a hardcoded 52ms per
 * character while holding the real audio in its hand — the single most obvious tell that the
 * animation was decorative.
 *
 * WHAT THE MICROPHONE COSTS, stated plainly because the rest of this product is offline-first:
 * `SpeechRecognition` in Chrome sends audio to Google. Typing works with the network off and
 * the whole agent — model and voice — runs locally either way. The console says so next to the
 * button rather than in a footnote.
 */

export type CallPhase =
  | "idle"
  | "connecting"
  | "greeting"
  | "listening"
  | "thinking"
  | "speaking"
  | "ended";

/** What the front door asks for. Whichever fields the agent asks for are required. */
export interface Intake {
  name: string;
  email: string;
  company: string;
}

/**
 * Who is answering, and what their form asks — fetched before the socket is opened.
 *
 * THE AGENT DECIDES WHICH FIELDS EXIST. Ours needs a work address because the domain is what
 * its research reads; a dental practice asking a patient for their employer is our form wearing
 * somebody else's brand. Everything else about an agent arrives on the socket after the call
 * starts, which is too late to draw the form that starts it.
 */
export interface FrontDoor {
  name: string;
  company: string;
  portrait?: string;
  disclosure?: string;
  fields: string[];
  ask_company: boolean;
  require_work_email: boolean;
}

const DEFAULT_DOOR: FrontDoor = {
  name: "",
  company: "",
  fields: ["name", "email", "company"],
  ask_company: true,
  require_work_email: true,
};

/** One published agent, as the console's picker lists them. */
export interface AgentRow {
  key: string;
  name: string;
  company: string;
  portrait?: string;
  /** What their pricing counts: "GPU-hour", "seat". Empty when nothing is priced. */
  sells?: string;
  /** Has a tour, a comparison and a price — so it can run the whole call rather than three
   *  quarters of it. The console opens on one of these. */
  complete?: boolean;
  version: number;
}

/** Every published agent. Console-only — a stranger on a customer's site gets `frontDoor`. */
export async function publishedAgents(): Promise<AgentRow[]> {
  try {
    const response = await fetch("/api/agents");
    if (!response.ok) return [];
    return (await response.json()).agents ?? [];
  } catch {
    return [];
  }
}

/** Ask who is answering. Never throws: a form that cannot render is worse than a plain one. */
export async function frontDoor(agentKey = ""): Promise<FrontDoor> {
  try {
    const query = agentKey ? `?key=${encodeURIComponent(agentKey)}` : "";
    const response = await fetch(`/api/agents/front-door${query}`);
    if (!response.ok) return DEFAULT_DOOR;
    const body = (await response.json()) as Partial<FrontDoor>;
    return { ...DEFAULT_DOOR, ...body, fields: body.fields?.length ? body.fields : DEFAULT_DOOR.fields };
  } catch {
    return DEFAULT_DOOR;
  }
}

export interface Turn {
  who: "agent" | "prospect";
  text: string;
  budget?: Record<string, number>;
  total?: number;
}

export interface Engines {
  llm: { name: string; model?: string | null; device?: string | null; local: boolean };
  tts: { name: string; local: boolean; voice?: string | null };
  stt: { name: string; local: boolean };
}

/** Something Nadia put on the stage. The most recent of each kind is kept. */
export interface Panels {
  facts?: { company: string; domain: string; facts: string[]; pages_read?: string[] };
  browser?: {
    state: string;
    url: string;
    title?: string;
    label?: string;
    frame?: string;
    scrolled_to?: string;
    /** True when `frame` is the whole page rather than one screenful. */
    full_page?: boolean;
    /** 0..1 — how far down that image the part she is talking about begins. */
    scroll_ratio?: number;
    /** 0..1 — how much of the image is one screenful. */
    viewport_ratio?: number;
  };
  slots?: { slots: Slot[]; failed?: string };
  pricing?: { company: string; size?: string; tiers: Tier[]; note?: string };
  comparison?: { company: string; rivals: Rival[] };
  quote?: Quote;
  checkout?: Checkout;
  booking?: { spoken: string; booking_id: string; starts_at: string };
  draft?: { subject: string; body: string; can_send: boolean; why_not?: string };
  note?: { text: string };
}

export interface Slot {
  starts_at: string;
  ends_at: string;
  spoken: string;
}

export interface Tier {
  name: string;
  per_seat: string;
  for: string;
}

/** How the agent's owner positions one competitor. Every line is theirs, never generated. */
export interface Rival {
  name: string;
  positioning: string;
  against: { dimension: string; ours: string }[];
}

/** A number with somebody's name on it. Computed on the server; the client only renders it. */
export interface Quote {
  tier: string;
  seats: number;
  /** The quantity in the tenant's own words: "40 seats", "2,000 GPU-hours". */
  units: string;
  unit_name: string;
  unit_plural: string;
  seats_from: string;
  assumed: boolean;
  company: string;
  currency: string;
  period: string;
  term: string;
  unit_display: string;
  subtotal_display: string;
  discount_display: string;
  total_display: string;
  spoken: string;
}

/** A hosted checkout. The card is entered on the processor's page, never here. */
export interface Checkout {
  checkout_id: string;
  url: string;
  provider: string;
  amount_display: string;
  period: string;
  description: string;
  test_mode: boolean;
  quote?: Quote;
}

export type Step =
  | "researching"
  | "opening"
  | "discovery"
  | "guide"
  | "compare"
  | "quote"
  | "close"
  | "pay"
  | "booking"
  | "wrap"
  | "handoff";

export interface CallState {
  phase: CallPhase;
  turns: Turn[];
  /** What the agent has said so far this turn — the caption bubble, built clause by clause. */
  caption: string;
  /** True only while audio is actually coming out. Drives the mouth. */
  speaking: boolean;
  /** True while the microphone is open. */
  listening: boolean;
  /** True when the mic stays open by itself — a call rather than a walkie-talkie. */
  handsFree: boolean;
  /** Interim transcript, shown as the prospect speaks. */
  partial: string;
  engines: Engines | null;
  handoff: boolean;
  budget: Record<string, number> | null;
  error: string | null;
  micSupported: boolean;
  /** Who she thinks she is talking to, from the address they typed. */
  contact: {
    email: string;
    domain: string;
    first_name: string;
    company: string;
    researchable: boolean;
  } | null;
  /** Which agent answered. On an embed this is the customer's own, not ours. */
  agent: { name: string; company: string; portrait: string; version: number } | null;
  /** Set when the server turned the call away — at capacity, too many just now. */
  refused: string | null;
  /** Where the call is in the plan. */
  step: Step | null;
  panels: Panels;
  /** Which panel the stage is showing. The newest one that is worth looking at. */
  active: keyof Panels | null;
  /** Set when a field was rejected at the front door, with the field it belongs under. */
  intakeError: string | null;
  intakeField: string | null;
  booked: boolean;
}

const IDLE: CallState = {
  phase: "idle",
  turns: [],
  caption: "",
  speaking: false,
  listening: false,
  handsFree: false,
  partial: "",
  engines: null,
  handoff: false,
  budget: null,
  error: null,
  micSupported: false,
  contact: null,
  agent: null,
  refused: null,
  step: null,
  panels: {},
  active: null,
  intakeError: null,
  intakeField: null,
  booked: false,
};

/** Chrome exposes this prefixed; Safari does too. Neither ships types for it. */
interface RecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((event: RecognitionEventLike) => void) | null;
  onerror: ((event: { error?: string }) => void) | null;
  onend: (() => void) | null;
}

interface RecognitionEventLike {
  resultIndex: number;
  results: {
    length: number;
    [index: number]: { isFinal: boolean; 0: { transcript: string } };
  };
}

type RecognitionCtor = new () => RecognitionLike;

function recognitionCtor(): RecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: RecognitionCtor;
    webkitSpeechRecognition?: RecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

function base64ToBytes(base64: string): ArrayBuffer {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export class LiveCall {
  private socket: WebSocket | null = null;
  private ctx: AudioContext | null = null;
  private sources: AudioBufferSourceNode[] = [];
  private timers: number[] = [];
  /** Where in the AudioContext's timeline the next clip should start. */
  private nextStart = 0;
  /** Taps the output so the face can move to the audio actually coming out. */
  private analyser: AnalyserNode | null = null;
  private levelBuffer: Uint8Array<ArrayBuffer> | null = null;
  private smoothed = 0;
  private recognition: RecognitionLike | null = null;
  /** Set while hands-free wants the mic open. Distinct from `listening`, which is whether it
   *  IS open — the two differ every time she speaks. */
  private wantsMic = false;
  private restart: number | null = null;
  private listeners = new Set<(state: CallState) => void>();
  private state: CallState = { ...IDLE, micSupported: recognitionCtor() !== null };

  /**
   * Decoded mouth frames per clip, and when that clip starts on the audio clock.
   *
   * Keyed by clip index because the frames arrive AFTER their audio — see `send_mouth` on the
   * server. By the time they land the clip may already be playing, or finished, and both are
   * handled by seeking on the clock rather than by playing from frame zero.
   */
  private mouths = new Map<number, { frames: ImageBitmap[]; fps: number }>();
  private clipStarts = new Map<number, number>();
  /** The clauses spoken so far in the current turn, joined to make the caption. */
  private captionParts: string[] = [];
  /** Set when a turn's first clip arrives; cleared once the face has moved. */
  private clipArrivedAt: number | null = null;
  private avatarMs: number | null = null;
  /** When the prospect stopped speaking, so recognition can be measured rather than guessed. */
  private speechEndedAt: number | null = null;
  private sttMs: number | null = null;

  subscribe(listener: (state: CallState) => void): () => void {
    this.listeners.add(listener);
    listener(this.state);
    return () => this.listeners.delete(listener);
  }

  private set(patch: Partial<CallState>): void {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((listener) => listener(this.state));
  }

  // ── the socket ─────────────────────────────────────────────────────────
  /**
   * Open the socket and, if the form was filled in, start the guided call.
   *
   * WITHOUT AN INTAKE THIS IS STILL A CALL — the plain conversation, no research, no plan. That
   * path exists because it is the smallest thing that exercises the whole voice stack, and the
   * tests and the screenshot script both use it.
   */
  /** Which published agent to reach. Empty in the console, which gets tenant zero. */
  constructor(private readonly agentKey: string = "") {}

  async connect(who?: Intake): Promise<void> {
    if (this.socket) return;
    this.set({
      phase: "connecting",
      error: null,
      intakeError: null,
      intakeField: null,
      turns: [],
      caption: "",
      handoff: false,
      refused: null,
      panels: {},
      active: null,
      booked: false,
      step: null,
    });

    const proto = location.protocol === "https:" ? "wss" : "ws";
    // The key selects a published agent and authorises nothing. It is in the page source of
    // the customer's website already, so it is not a secret and is not treated as one.
    const query = this.agentKey ? `?key=${encodeURIComponent(this.agentKey)}` : "";
    const socket = new WebSocket(`${proto}://${location.host}/api/calls/ws${query}`);
    this.socket = socket;

    socket.onmessage = (event) => this.receive(JSON.parse(event.data));
    socket.onerror = () =>
      this.set({
        phase: "idle",
        error: "Could not reach the call service. Is the API running on port 8000?",
      });
    socket.onclose = () => {
      this.socket = null;
      if (this.state.phase !== "ended") this.set({ phase: "ended" });
    };

    await new Promise<void>((resolve) => {
      socket.onopen = () => resolve();
      // A socket that never opens must not leave the button spinning forever.
      window.setTimeout(resolve, 4000);
    });

    if (socket.readyState !== WebSocket.OPEN) {
      this.set({ phase: "idle", error: "The call service did not answer." });
      this.socket = null;
      return;
    }

    this.captionParts = [];
    socket.send(
      JSON.stringify(
        who
          ? { type: "intake", name: who.name, email: who.email, company: who.company }
          : { type: "start" },
      ),
    );
    this.set({ phase: "greeting" });
  }

  /** Accept one of the times she offered. */
  pickSlot(index: number): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    this.stopAudio();
    this.socket.send(JSON.stringify({ type: "pick_slot", index }));
    this.set({ phase: "thinking" });
  }

  private receive(message: Record<string, unknown>): void {
    switch (message.type) {
      case "disclosure":
        // METADATA, NOT A TURN. The disclosure is also spoken, and speaking it produces clips
        // and a `done` like any other utterance — pushing a turn here too put it in the
        // transcript twice, once before it was said and once after.
        this.set({
          engines: (message.engines as Engines) ?? null,
          contact: (message.contact as CallState["contact"]) ?? null,
          agent: (message.agent as CallState["agent"]) ?? null,
        });
        break;

      case "refused":
        // Admission turned the call away, or ended one that had run too long. The server
        // writes the sentence: to a visitor on a customer's website this is the practice not
        // picking up, and a status code is not a thing you say to somebody.
        this.stopAudio();
        this.set({ refused: String(message.spoken ?? ""), phase: "ended" });
        break;

      case "no_agent":
        this.set({
          refused: "This assistant isn't available right now.",
          phase: "ended",
        });
        break;

      case "heard":
        this.set({ partial: String(message.text) });
        break;

      case "intake_error":
        // Not a socket error. Someone mistyped their address and is watching a form.
        this.set({
          intakeError: String(message.spoken),
          intakeField: message.field ? String(message.field) : null,
          phase: "idle",
        });
        this.socket?.close();
        break;

      case "phase":
        // A NEW STEP IS A NEW THING BEING SAID. The caption accumulates clause by clause and was
        // only cleared per turn, so a turn that moves through research, greeting and narration
        // ran all three together into one paragraph — ending, on screen, with two greetings and
        // a sentence from a minute earlier. The transcript keeps every word; the caption shows
        // the current one.
        this.captionParts = [];
        this.set({ step: message.step as Step, caption: "" });
        break;

      case "panel": {
        const kind = String(message.panel) as keyof Panels;
        const { type: _type, panel: _panel, ...data } = message;
        const panels = { ...this.state.panels, [kind]: data } as Panels;
        this.set({
          panels,
          // A browser frame outranks whatever was there: it is the thing she is talking about
          // right now. Everything else simply becomes the newest panel.
          active: kind === "browser" && data.state === "opening" ? this.state.active : kind,
          booked: kind === "booking" ? true : this.state.booked,
        });
        break;
      }

      case "token":
        // Tokens are not rendered as they arrive. The caption follows the AUDIO — showing text
        // the prospect has not heard yet puts the subtitles ahead of the voice, which reads as
        // a bug even though both are correct.
        break;

      case "clip":
        this.play(message as unknown as ClipMessage);
        break;

      case "mouth":
        void this.acceptMouth(message as unknown as MouthMessage);
        break;

      case "done": {
        const budget = (message.budget ?? null) as Record<string, number> | null;
        const response = String(message.response ?? "");
        if (response) {
          this.pushTurn({
            who: "agent",
            text: response,
            budget: budget ?? undefined,
            total: budget?.total_ms,
          });
        }
        this.set({
          budget,
          handoff: Boolean(message.handoff) || this.state.handoff,
          phase: this.state.speaking ? "speaking" : "listening",
          // The interim transcript is a live view of somebody talking. Once the turn is
          // answered it is history, and leaving it up showed the visitor their own last
          // sentence a second time, in italics, under the reply to it.
          partial: "",
        });
        break;
      }

      case "interrupted":
        this.stopAudio();
        break;

      default:
        break;
    }
  }

  // ── saying something ───────────────────────────────────────────────────
  say(text: string): void {
    const trimmed = text.trim();
    if (!trimmed || !this.socket || this.socket.readyState !== WebSocket.OPEN) return;

    // Barge-in. Someone who starts talking has stopped listening, and an agent that finishes
    // its sentence anyway is the most robotic thing it can do.
    this.stopAudio();

    this.pushTurn({ who: "prospect", text: trimmed });
    this.captionParts = [];
    this.set({ phase: "thinking", partial: "", caption: "" });
    this.socket.send(
      JSON.stringify({
        type: "say",
        text: trimmed,
        // The two stages only this side can see. See `LatencyBudget.adopt` on the server.
        ...(this.sttMs !== null ? { stt_ms: this.sttMs } : {}),
        ...(this.avatarMs !== null ? { avatar_ms: this.avatarMs } : {}),
      }),
    );
    // Cleared either way: a typed message has no transcription cost, and carrying the last
    // spoken turn's number into it would attribute time to a stage that did not run.
    this.sttMs = null;
  }

  // ── the microphone ─────────────────────────────────────────────────────
  /**
   * Hands-free: the mic stays open and she is interrupted by talking, not by a button.
   *
   * HALF DUPLEX, AND THAT IS THE HONEST LIMIT. The browser's `SpeechRecognition` opens its own
   * capture and gives no way to set `echoCancellation` on it, so with speakers and an open mic
   * her own voice is transcribed and she answers herself. The mic is therefore CLOSED for
   * exactly as long as she is speaking and reopened the moment she stops, which is the whole
   * difference between this and holding a button: nothing to press, and the only thing you
   * cannot do is talk over her.
   *
   * Full duplex — real barge-in — needs `getUserMedia` with echo cancellation and a
   * transcriber that is not the Web Speech API. That is a different engine, not a flag.
   */
  setHandsFree(on: boolean): void {
    this.wantsMic = on;
    this.set({ handsFree: on });
    if (on) this.openMic();
    else this.closeMic();
  }

  /** Called whenever she starts or stops speaking, to keep the mic out of her way. */
  private syncMic(): void {
    if (!this.wantsMic) return;
    if (this.state.speaking) this.closeMic();
    else if (!this.recognition) this.openMic();
  }

  private closeMic(): void {
    if (this.restart !== null) {
      window.clearTimeout(this.restart);
      this.restart = null;
    }
    const active = this.recognition;
    this.recognition = null;
    // `abort` rather than `stop`: `stop` finalises whatever it has heard, and what it has heard
    // while she was talking is her.
    active?.abort();
    this.set({ listening: false, partial: "" });
  }

  private openMic(): void {
    if (this.recognition || this.state.speaking) return;
    this.startListening();
  }

  startListening(): void {
    const Ctor = recognitionCtor();
    if (!Ctor || this.recognition) return;

    this.stopAudio(); // holding the button is a barge-in
    const recognition = new Ctor();
    recognition.lang = "en-US";
    recognition.continuous = false;
    recognition.interimResults = true;

    recognition.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        if (!result) continue;
        if (result.isFinal) {
          this.sttMs =
            this.speechEndedAt !== null ? performance.now() - this.speechEndedAt : null;
          this.speechEndedAt = null;
          this.say(result[0].transcript);
          return;
        }
        interim += result[0].transcript;
      }
      this.set({ partial: interim });
    };

    recognition.onerror = (event) => {
      // `no-speech` and `aborted` are ordinary: silence, or the mic being closed while she
      // speaks. Neither is worth telling anybody about.
      if (event.error === "not-allowed" || event.error === "service-not-allowed") {
        // Permission refused. Stop asking — a loop that reopens the mic every 250ms after a
        // denial is how a page gets a permission prompt burned into it.
        this.wantsMic = false;
        this.set({ handsFree: false, error: "Microphone permission was refused." });
        return;
      }
      if (event.error && event.error !== "no-speech" && event.error !== "aborted") {
        this.set({
          error:
            event.error === "network"
              ? "Speech recognition needs a connection. Typing works offline."
              : `Microphone: ${event.error}`,
        });
      }
    };

    recognition.onend = () => {
      this.recognition = null;
      this.set({ listening: false });
      // Chrome ends a recognition session after a few seconds of silence whatever
      // `continuous` says, so hands-free is a loop rather than a setting. The small delay
      // keeps a permission failure from becoming a busy loop.
      if (this.wantsMic && !this.state.speaking) {
        this.restart = window.setTimeout(() => this.openMic(), 250);
      }
    };

    try {
      recognition.start();
      this.recognition = recognition;
      this.set({ listening: true, partial: "", error: null, phase: "listening" });
    } catch {
      this.recognition = null;
    }
  }

  stopListening(): void {
    if (!this.recognition) return;
    // The clock for endpointing starts the instant the button is released — that is the moment
    // the prospect believes they have finished talking.
    this.speechEndedAt = performance.now();
    this.recognition.stop();
  }

  // ── playback ───────────────────────────────────────────────────────────
  private audioContext(): AudioContext {
    if (!this.ctx) {
      const Ctor =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      this.ctx = new Ctor();
      // Everything is routed through an analyser so the face has something REAL to move to.
      // A small FFT: this is used for loudness, not spectrum, and a big window adds latency
      // between the sound and the movement, which is the one thing that must not happen.
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 256;
      // Typed against a plain ArrayBuffer: lib.dom now narrows getByteTimeDomainData to
      // Uint8Array<ArrayBuffer>, and the default Uint8Array is over ArrayBufferLike.
      this.levelBuffer = new Uint8Array(new ArrayBuffer(this.analyser.fftSize));
      this.analyser.connect(this.ctx.destination);
    }
    if (this.ctx.state === "suspended") void this.ctx.resume();
    return this.ctx;
  }

  /**
   * How loud she is right now, 0 to 1.
   *
   * READ, NOT PUSHED. Loudness changes every frame, and putting it in React state would
   * re-render the whole call view sixty times a second to move one element a pixel. The face
   * polls this from its own animation frame and writes styles directly.
   *
   * Returns 0 when the browser voice is speaking, because `speechSynthesis` output does not go
   * through the audio graph and there is nothing real to measure. Inventing a wobble there
   * would be animating to a number nobody can hear.
   */
  level(): number {
    if (!this.analyser || !this.levelBuffer || !this.state.speaking) {
      this.smoothed *= 0.85;
      return this.smoothed;
    }
    this.analyser.getByteTimeDomainData(this.levelBuffer);
    let sum = 0;
    for (let i = 0; i < this.levelBuffer.length; i += 1) {
      const centred = (this.levelBuffer[i]! - 128) / 128;
      sum += centred * centred;
    }
    const rms = Math.sqrt(sum / this.levelBuffer.length);
    // Asymmetric smoothing: quick to rise so a consonant lands on time, slow to fall so the
    // face does not flicker between syllables.
    const scaled = Math.min(1, rms * 3.2);
    this.smoothed = scaled > this.smoothed ? scaled : this.smoothed * 0.82 + scaled * 0.18;
    return this.smoothed;
  }

  /**
   * Take delivery of a clip's mouth frames and decode them off the main thread.
   *
   * `createImageBitmap` rather than `<img>` elements: decoding twenty-five JPEGs a second on the
   * main thread while a call is running drops frames in the rest of the UI, and a bitmap can be
   * handed straight to `drawImage` with no further work.
   */
  private async acceptMouth(message: MouthMessage): Promise<void> {
    try {
      const bitmaps = await Promise.all(
        message.frames.map(async (data) => {
          const bytes = base64ToBytes(data);
          return createImageBitmap(new Blob([bytes], { type: "image/jpeg" }));
        }),
      );
      this.mouths.set(message.index, { frames: bitmaps, fps: message.fps || 25 });
    } catch {
      // A clip without a mouth is a still face for a second. Not worth failing a call over.
    }
  }

  /**
   * The mouth frame for this instant, or null when there is nothing to show.
   *
   * Read on the face's own animation frame, like `level()`. Seeks by the audio clock rather
   * than counting frames, so frames that arrived late are skipped rather than played behind.
   */
  mouthFrame(): ImageBitmap | null {
    if (!this.ctx || this.mouths.size === 0) return null;
    const now = this.ctx.currentTime;

    for (const [index, mouth] of this.mouths) {
      const start = this.clipStarts.get(index);
      if (start === undefined) continue;
      const elapsed = now - start;
      if (elapsed < 0) continue;
      const frame = Math.floor(elapsed * mouth.fps);
      if (frame < mouth.frames.length) return mouth.frames[frame] ?? null;
    }
    return null;
  }

  private async play(clip: ClipMessage): Promise<void> {
    if (this.clipArrivedAt === null) this.clipArrivedAt = performance.now();

    if (clip.browser_voice || !clip.wav) {
      this.speakInBrowser(clip);
      return;
    }

    const ctx = this.audioContext();
    let buffer: AudioBuffer;
    try {
      buffer = await ctx.decodeAudioData(base64ToBytes(clip.wav));
    } catch {
      // A clip that will not decode must not stop the call; the prospect loses a clause, not
      // the conversation.
      this.speakInBrowser(clip);
      return;
    }

    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.analyser ?? ctx.destination);

    // A small lead so the first clip is not scheduled in the past — decoding took real time.
    const startAt = Math.max(ctx.currentTime + 0.03, this.nextStart);
    source.start(startAt);
    this.nextStart = startAt + buffer.duration;
    this.sources.push(source);
    // Recorded on the SAME clock the audio is scheduled on. Wall-clock time drifts against an
    // AudioContext, and a mouth that drifts is worse than no mouth.
    this.clipStarts.set(clip.index, startAt);

    const delay = Math.max(0, (startAt - ctx.currentTime) * 1000);
    this.at(delay, () => this.mouthOn(clip.text));
    this.at(delay + buffer.duration * 1000, () => this.mouthOffIfLast(source));
  }

  private speakInBrowser(clip: ClipMessage): void {
    if (typeof speechSynthesis === "undefined") {
      // No voice at all. Show the caption for as long as the words would have taken, so the
      // transcript still reads as a conversation rather than appearing all at once.
      this.mouthOn(clip.text);
      this.at(clip.duration_ms, () => this.mouthOff());
      return;
    }
    // READ THE SPOKEN SPELLING, SHOW THE WRITTEN ONE. `speechSynthesis` says "asterisk
    // asterisk" and "aitch tee tee pee colon" as readily as any other engine, so the server
    // sends a pronounceable version alongside the caption. `mouthOn` still gets `text`,
    // because that is what goes on screen.
    const utterance = new SpeechSynthesisUtterance(clip.spoken || clip.text);
    utterance.rate = 1.05;
    utterance.onstart = () => this.mouthOn(clip.text);
    utterance.onend = () => this.mouthOff();
    speechSynthesis.speak(utterance);
  }

  private mouthOn(text: string): void {
    // She is about to be audible; the mic must not hear her.
    if (this.wantsMic) this.closeMic();
    if (this.clipArrivedAt !== null) {
      this.avatarMs = performance.now() - this.clipArrivedAt;
      this.clipArrivedAt = null;
    }
    // THE CAPTION ACCUMULATES ACROSS THE TURN rather than being replaced per clip. Clips are
    // cut for synthesis latency, not for reading: the first one is a dozen characters, so a
    // caption that showed only the clip currently playing rendered "me bring someone" on its
    // own — a fragment that is correct, is in sync with the audio, and reads as broken.
    // Appending keeps it in sync AND leaves a whole sentence on screen.
    this.captionParts.push(text);
    this.set({ caption: this.captionParts.join(" "), speaking: true, phase: "speaking" });
  }

  private mouthOff(): void {
    this.set({ speaking: false, phase: "listening" });
    // Her turn is over, so the microphone is yours again.
    this.syncMic();
  }

  private mouthOffIfLast(source: AudioBufferSourceNode): void {
    // Only stop the mouth if nothing else is scheduled behind this clip. Stopping between
    // clauses makes the face stutter through a sentence it is still saying.
    const index = this.sources.indexOf(source);
    if (index >= 0) this.sources.splice(index, 1);
    if (this.sources.length === 0) this.mouthOff();
  }

  private at(ms: number, run: () => void): void {
    this.timers.push(window.setTimeout(run, ms));
  }

  private stopAudio(): void {
    this.sources.forEach((source) => {
      try {
        source.stop();
      } catch {
        // Already finished. Stopping a stopped source throws in some browsers and means
        // nothing here.
      }
    });
    this.sources = [];
    this.smoothed = 0;
    this.mouths.forEach((mouth) => mouth.frames.forEach((frame) => frame.close()));
    this.mouths.clear();
    this.clipStarts.clear();
    this.timers.forEach((timer) => window.clearTimeout(timer));
    this.timers = [];
    this.nextStart = this.ctx ? this.ctx.currentTime : 0;
    if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
    this.captionParts = [];
    this.set({ speaking: false, caption: "" });
  }

  private pushTurn(turn: Turn): void {
    this.set({ turns: [...this.state.turns, turn] });
  }

  // ── ending ─────────────────────────────────────────────────────────────
  hangUp(): void {
    this.wantsMic = false;
    this.stopAudio();
    this.closeMic();
    this.socket?.close();
    this.socket = null;
    void this.ctx?.close();
    this.ctx = null;
    this.analyser = null;
    this.levelBuffer = null;
    this.set({ phase: "ended", listening: false, handsFree: false, partial: "" });
  }
}

interface MouthMessage {
  type: "mouth";
  index: number;
  fps: number;
  frames: string[];
}

interface ClipMessage {
  type: "clip";
  index: number;
  /** What to show. Optional `spoken` is what to read aloud, when the two differ. */
  text: string;
  spoken?: string;
  duration_ms: number;
  generate_ms: number;
  wav: string;
  browser_voice: boolean;
}
