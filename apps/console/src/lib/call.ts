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

/** Something Liv put on the stage. The most recent of each kind is kept. */
export interface Panels {
  facts?: { company: string; domain: string; facts: string[]; pages_read?: string[] };
  browser?: { state: string; url: string; title?: string; label?: string; frame?: string };
  slots?: { slots: Slot[]; failed?: string };
  pricing?: { company: string; size?: string; tiers: Tier[]; note?: string };
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

export type Step =
  | "researching"
  | "opening"
  | "discovery"
  | "showing"
  | "proposing"
  | "booking"
  | "pricing"
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
  /** Interim transcript, shown as the prospect speaks. */
  partial: string;
  engines: Engines | null;
  handoff: boolean;
  budget: Record<string, number> | null;
  error: string | null;
  micSupported: boolean;
  /** Who she thinks she is talking to, from the address they typed. */
  contact: { email: string; domain: string; first_name: string; researchable: boolean } | null;
  /** Where the call is in the plan. */
  step: Step | null;
  panels: Panels;
  /** Which panel the stage is showing. The newest one that is worth looking at. */
  active: keyof Panels | null;
  /** Set when the address was rejected at the front door. */
  intakeError: string | null;
  booked: boolean;
}

const IDLE: CallState = {
  phase: "idle",
  turns: [],
  caption: "",
  speaking: false,
  listening: false,
  partial: "",
  engines: null,
  handoff: false,
  budget: null,
  error: null,
  micSupported: false,
  contact: null,
  step: null,
  panels: {},
  active: null,
  intakeError: null,
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
  private listeners = new Set<(state: CallState) => void>();
  private state: CallState = { ...IDLE, micSupported: recognitionCtor() !== null };

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
   * Open the socket and, if an address was given, start the guided call.
   *
   * WITHOUT AN EMAIL THIS IS STILL A CALL — the plain conversation, no research, no plan. That
   * path exists because it is the smallest thing that exercises the whole voice stack, and the
   * tests and the screenshot script both use it.
   */
  async connect(email?: string): Promise<void> {
    if (this.socket) return;
    this.set({
      phase: "connecting",
      error: null,
      intakeError: null,
      turns: [],
      caption: "",
      handoff: false,
      panels: {},
      active: null,
      booked: false,
      step: null,
    });

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${proto}://${location.host}/api/calls/ws`);
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
      JSON.stringify(email ? { type: "email", email } : { type: "start" }),
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
        });
        break;

      case "heard":
        this.set({ partial: String(message.text) });
        break;

      case "intake_error":
        // Not a socket error. Someone mistyped their address and is watching a form.
        this.set({ intakeError: String(message.spoken), phase: "idle" });
        this.socket?.close();
        break;

      case "phase":
        this.set({ step: message.step as Step });
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
      // `no-speech` and `aborted` are ordinary: the button was tapped, or held in silence.
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
    const utterance = new SpeechSynthesisUtterance(clip.text);
    utterance.rate = 1.05;
    utterance.onstart = () => this.mouthOn(clip.text);
    utterance.onend = () => this.mouthOff();
    speechSynthesis.speak(utterance);
  }

  private mouthOn(text: string): void {
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
    this.stopAudio();
    this.recognition?.abort();
    this.recognition = null;
    this.socket?.close();
    this.socket = null;
    void this.ctx?.close();
    this.ctx = null;
    this.analyser = null;
    this.levelBuffer = null;
    this.set({ phase: "ended", listening: false, partial: "" });
  }
}

interface ClipMessage {
  type: "clip";
  index: number;
  text: string;
  duration_ms: number;
  generate_ms: number;
  wav: string;
  browser_voice: boolean;
}
