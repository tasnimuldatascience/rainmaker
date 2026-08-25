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
  async connect(brief?: { company: string; domain: string; enrichment: unknown }): Promise<void> {
    if (this.socket) return;
    this.set({ phase: "connecting", error: null, turns: [], caption: "", handoff: false });

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
    if (brief) socket.send(JSON.stringify({ type: "brief", ...brief }));
    socket.send(JSON.stringify({ type: "start" }));
    this.set({ phase: "greeting" });
  }

  private receive(message: Record<string, unknown>): void {
    switch (message.type) {
      case "disclosure":
        this.set({ engines: (message.engines as Engines) ?? null });
        this.pushTurn({ who: "agent", text: String(message.text) });
        break;

      case "heard":
        this.set({ partial: String(message.text) });
        break;

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
    }
    if (this.ctx.state === "suspended") void this.ctx.resume();
    return this.ctx;
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
    source.connect(ctx.destination);

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
