// @vitest-environment jsdom
/**
 * The two things about a live call that only fail when nobody is watching.
 *
 * A DROPPED SOCKET USED TO END THE CALL SILENTLY. `onclose` set the phase to "ended" and that
 * was the whole recovery: a wifi blip mid-sentence returned the visitor to the lobby with the
 * transcript gone from screen. It now reconnects with backoff — and, just as importantly, does
 * NOT reconnect a call somebody deliberately ended, because every attempt is a fresh socket and
 * the server counts a fresh socket as one of the six calls a visitor is allowed in an hour.
 *
 * CLIPS USED TO RACE EACH OTHER TO THE SPEAKER. `play` is async and awaits `decodeAudioData`,
 * which takes as long as its buffer is big; one message handler per clip meant a short later
 * clause could finish decoding first and take the earlier clause's slot on the timeline. The
 * sentence came out in the wrong order, intermittently, on whichever clause happened to be
 * short. Both of those are timing bugs, which is exactly the kind that a reviewer meets before
 * a test does unless the test is written.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LiveCall } from "../src/lib/call";
import type { CallState } from "../src/lib/call";

// ── the fakes ────────────────────────────────────────────────────────────────

/** Every socket the call has opened this test, in order. */
let sockets: FakeSocket[] = [];

/** Whether a newly constructed socket ever opens. False stands in for a server that is down. */
let socketOpens = true;

class FakeSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState = FakeSocket.CONNECTING;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;

  constructor(readonly url: string) {
    sockets.push(this);
    // On a microtask, not synchronously: `open()` assigns `onopen` inside a promise executor
    // that runs after the constructor returns, and a socket that opened before then would
    // resolve nothing and hang on the four-second timeout instead.
    queueMicrotask(() => {
      if (!socketOpens) return;
      this.readyState = FakeSocket.OPEN;
      this.onopen?.();
    });
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    if (this.readyState === FakeSocket.CLOSED) return;
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.();
  }

  /** The server, or the network, going away without being asked. */
  drop(): void {
    this.close();
  }

  deliver(message: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  /** The types this socket was asked to send, in order. */
  types(): string[] {
    return this.sent.map((raw) => String(JSON.parse(raw).type));
  }
}

/** How long a given clip takes to decode, and how long it plays for. Keyed by its payload. */
const DECODES = new Map<string, { duration: number; ticks: number }>();

class FakeAudioContext {
  static last: FakeAudioContext | null = null;
  currentTime = 0;
  state = "running";
  destination = {};
  /** Every buffer source made on this context, in the order `play` made them. */
  sources: { startedAt: number | null; buffer: unknown }[] = [];

  constructor() {
    FakeAudioContext.last = this;
  }

  createAnalyser() {
    return {
      fftSize: 256,
      connect: () => undefined,
      getByteTimeDomainData: () => undefined,
    };
  }

  createBufferSource() {
    const source = {
      startedAt: null as number | null,
      buffer: null as unknown,
      connect: () => undefined,
      start: (when: number) => {
        source.startedAt = when;
      },
      stop: () => undefined,
    };
    this.sources.push(source);
    return source;
  }

  async decodeAudioData(bytes: ArrayBuffer) {
    const key = String.fromCharCode(...new Uint8Array(bytes));
    const spec = DECODES.get(key) ?? { duration: 1, ticks: 0 };
    // A decode that takes real time, expressed in microtasks so it does not need the clock.
    for (let i = 0; i < spec.ticks; i += 1) await Promise.resolve();
    return { duration: spec.duration };
  }

  resume() {
    return Promise.resolve();
  }

  close() {
    return Promise.resolve();
  }
}

/** ImageBitmaps that say when they were closed, which is how eviction is observed. */
let bitmapsClosed = 0;

/** Let every pending microtask run. The decode chain is promises, not timers. */
async function settle(ticks = 30): Promise<void> {
  for (let i = 0; i < ticks; i += 1) await Promise.resolve();
}

function clip(index: number, payload: string, text = "…"): Record<string, unknown> {
  return {
    type: "clip",
    index,
    text,
    duration_ms: 1000,
    generate_ms: 1,
    wav: btoa(payload),
    browser_voice: false,
  };
}

const WHO = { name: "Sam", email: "sam@acme.com", company: "Acme" };

beforeEach(() => {
  sockets = [];
  socketOpens = true;
  bitmapsClosed = 0;
  DECODES.clear();
  FakeAudioContext.last = null;
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("AudioContext", FakeAudioContext);
  vi.stubGlobal("createImageBitmap", async () => ({
    close: () => {
      bitmapsClosed += 1;
    },
  }));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// ── the socket ───────────────────────────────────────────────────────────────

describe("a call whose socket drops", () => {
  it("reopens it, replays the intake, and keeps the transcript", async () => {
    const call = new LiveCall("acme");
    let state = {} as CallState;
    call.subscribe((next) => (state = next));

    await call.connect(WHO);
    expect(sockets).toHaveLength(1);
    sockets[0]!.deliver({ type: "done", response: "Hello — I read your site." });
    expect(state.turns).toHaveLength(1);

    sockets[0]!.drop();
    expect(state.reconnecting).toBe(true);
    expect(state.turns).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(1_000);

    expect(sockets).toHaveLength(2);
    expect(JSON.parse(sockets[1]!.sent[0]!)).toEqual({ type: "intake", ...WHO });
    expect(state.phase).toBe("greeting");
    expect(state.reconnecting).toBe(false);
    // The transcript is the client's. The server's memory of it is not, and the console says so.
    expect(state.turns).toHaveLength(1);
    expect(state.error).toMatch(/dropped/);

    call.hangUp();
  });

  it("does not resurrect a call the user ended", async () => {
    const call = new LiveCall();
    let state = {} as CallState;
    call.subscribe((next) => (state = next));

    await call.connect(WHO);
    call.hangUp();

    await vi.advanceTimersByTimeAsync(60_000);
    expect(sockets).toHaveLength(1);
    expect(state.phase).toBe("ended");
    expect(state.reconnecting).toBe(false);
  });

  it("does not retry a refusal, which is a decision rather than a dropped packet", async () => {
    const call = new LiveCall();
    let state = {} as CallState;
    call.subscribe((next) => (state = next));

    await call.connect(WHO);
    sockets[0]!.deliver({ type: "refused", spoken: "We're taking too many calls just now." });
    sockets[0]!.drop();

    await vi.advanceTimersByTimeAsync(60_000);
    expect(sockets).toHaveLength(1);
    expect(state.refused).toBe("We're taking too many calls just now.");
    expect(state.phase).toBe("ended");
  });

  it("gives up after a bounded number of attempts and says the call is over", async () => {
    const call = new LiveCall();
    let state = {} as CallState;
    call.subscribe((next) => (state = next));

    await call.connect(WHO);
    socketOpens = false;
    sockets[0]!.drop();

    await vi.advanceTimersByTimeAsync(120_000);

    // The original, plus MAX_RECONNECTS attempts. A number, not a loop: every one of these
    // spends a slice of the visitor's hourly allowance on the server.
    expect(sockets).toHaveLength(4);
    expect(state.phase).toBe("ended");
    expect(state.reconnecting).toBe(false);
    expect(state.error).toMatch(/stopped answering/);
  });

  it("waits to be told the network is back rather than spending attempts offline", async () => {
    const call = new LiveCall();
    let state = {} as CallState;
    call.subscribe((next) => (state = next));

    await call.connect(WHO);
    Object.defineProperty(navigator, "onLine", { configurable: true, get: () => false });
    sockets[0]!.drop();

    await vi.advanceTimersByTimeAsync(120_000);
    expect(sockets).toHaveLength(1);
    expect(state.reconnecting).toBe(true);

    Object.defineProperty(navigator, "onLine", { configurable: true, get: () => true });
    window.dispatchEvent(new Event("online"));
    await vi.advanceTimersByTimeAsync(1_000);

    expect(sockets).toHaveLength(2);
    expect(state.phase).toBe("greeting");
    call.hangUp();
  });

  it("holds what was typed into the dead socket and sends it once there is one", async () => {
    const call = new LiveCall();
    let state = {} as CallState;
    call.subscribe((next) => (state = next));

    await call.connect(WHO);
    sockets[0]!.drop();
    call.say("are you still there?");

    // Shown immediately, because they did say it.
    expect(state.turns.at(-1)).toMatchObject({ who: "prospect", text: "are you still there?" });

    await vi.advanceTimersByTimeAsync(1_000);
    expect(sockets[1]!.types()).toEqual(["intake", "say"]);
    call.hangUp();
  });
});

// ── the audio schedule ───────────────────────────────────────────────────────

describe("the audio schedule", () => {
  it("plays clauses in the order they arrived, not the order they decoded", async () => {
    DECODES.set("first", { duration: 1, ticks: 8 });
    DECODES.set("second", { duration: 2, ticks: 0 });

    const call = new LiveCall();
    call.subscribe(() => undefined);
    await call.connect(WHO);

    sockets[0]!.deliver(clip(0, "first", "Yes,"));
    sockets[0]!.deliver(clip(1, "second", "we do."));
    await settle();

    const ctx = FakeAudioContext.last!;
    expect(ctx.sources).toHaveLength(2);
    // A small lead so the first clip is not scheduled in the past, then back to back.
    expect(ctx.sources[0]!.startedAt).toBeCloseTo(0.03, 5);
    expect(ctx.sources[1]!.startedAt).toBeCloseTo(1.03, 5);
    call.hangUp();
  });

  it("drops a clause that finished decoding after the turn was interrupted", async () => {
    DECODES.set("slow", { duration: 1, ticks: 12 });

    const call = new LiveCall();
    call.subscribe(() => undefined);
    await call.connect(WHO);

    sockets[0]!.deliver(clip(0, "slow", "As I was saying"));
    // Far enough in that the decode has started and is not yet finished.
    await settle(3);
    call.say("actually, hold on");
    await settle();

    const ctx = FakeAudioContext.last!;
    expect(ctx.sources).toHaveLength(0);
    call.hangUp();
  });

  it("does not schedule a second call's greeting on the first call's clock", async () => {
    // A two-minute clause is not realistic; a call that ran two minutes is, and `nextStart`
    // carried the end of it into the next AudioContext, whose clock starts again at zero.
    DECODES.set("long", { duration: 120, ticks: 0 });
    DECODES.set("hello", { duration: 1, ticks: 0 });

    const call = new LiveCall();
    call.subscribe(() => undefined);

    await call.connect(WHO);
    sockets[0]!.deliver(clip(0, "long"));
    await settle();
    expect(FakeAudioContext.last!.sources[0]!.startedAt).toBeCloseTo(0.03, 5);
    // The call ran. `nextStart` is now a position two minutes into a clock that is about to be
    // thrown away, which is the whole bug.
    FakeAudioContext.last!.currentTime = 130;
    call.hangUp();

    await call.connect(WHO);
    sockets[1]!.deliver(clip(0, "hello", "Hello again."));
    await settle();

    expect(FakeAudioContext.last!.sources[0]!.startedAt).toBeCloseTo(0.03, 5);
    call.hangUp();
  });

  it("closes a clip's mouth frames once its audio has finished", async () => {
    DECODES.set("short", { duration: 0.4, ticks: 0 });

    const call = new LiveCall();
    call.subscribe(() => undefined);
    await call.connect(WHO);

    sockets[0]!.deliver(clip(0, "short"));
    sockets[0]!.deliver({ type: "mouth", index: 0, fps: 25, frames: ["", "", ""] });
    await settle();

    const ctx = FakeAudioContext.last!;
    // Mid-clip: there is a frame, and nothing has been thrown away.
    ctx.currentTime = 0.06;
    expect(call.mouthFrame()).not.toBeNull();
    expect(bitmapsClosed).toBe(0);

    // Past the end of the frames. They are spent, and holding them for the rest of the call is
    // what turned a twenty-minute conversation into thousands of decoded bitmaps.
    ctx.currentTime = 5;
    expect(call.mouthFrame()).toBeNull();
    expect(bitmapsClosed).toBe(3);
    call.hangUp();
  });
});
