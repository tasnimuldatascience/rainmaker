/**
 * Hybrid Logical Clocks (Kulkarni et al., 2014).
 *
 * WHY NOT WALL CLOCK. A local-first console writes while disconnected, so two replicas routinely
 * produce edits with no communication between them. Last-write-wins on `Date.now()` means the
 * winner is decided by whichever laptop's clock drifted further ahead — and clock skew on
 * consumer machines is regularly seconds, sometimes minutes. A rep whose clock runs fast wins
 * every conflict for as long as the skew lasts, silently.
 *
 * WHY NOT A LAMPORT COUNTER. Pure logical clocks order events correctly but carry no relation
 * to real time, so a UI cannot say "edited 3 minutes ago" and a compaction job cannot say
 * "drop ops older than 90 days". Both are requirements here.
 *
 * HLC gives both: it tracks physical time but is monotonic under message receipt, so causality
 * is never violated even when the wall clock jumps backwards (NTP correction, VM suspend,
 * daylight-saving on a misconfigured host). The logical counter absorbs the difference.
 *
 * The invariant that matters: if event A happened-before event B, then hlc(A) < hlc(B). The
 * converse does not hold — concurrent events get an arbitrary but *consistent* total order,
 * which is exactly what LWW needs to be deterministic across replicas.
 */

/** Milliseconds a remote timestamp may lead local physical time before we refuse it. */
export const MAX_DRIFT_MS = 60_000;

export interface HLC {
  /** Physical component, milliseconds since epoch. */
  readonly wall: number;
  /** Logical component; breaks ties within the same millisecond. */
  readonly counter: number;
  /** Replica identifier; breaks ties when wall and counter are both equal. */
  readonly actor: string;
}

export class ClockDriftError extends Error {
  constructor(remote: HLC, localWall: number) {
    super(
      `remote timestamp ${remote.wall} leads local clock ${localWall} by ` +
        `${remote.wall - localWall}ms, exceeding MAX_DRIFT_MS=${MAX_DRIFT_MS}. ` +
        `Refusing to advance — a replica with a badly wrong clock would otherwise pin ` +
        `this one into the future permanently.`,
    );
    this.name = "ClockDriftError";
  }
}

export class HybridClock {
  private wall = 0;
  private counter = 0;

  constructor(
    readonly actor: string,
    private readonly now: () => number = () => Date.now(),
  ) {}

  /** Timestamp for a locally-originated event. */
  tick(): HLC {
    const physical = this.now();
    if (physical > this.wall) {
      this.wall = physical;
      this.counter = 0;
    } else {
      // Physical time did not advance (same millisecond, or the clock went backwards).
      // Keep the logical component moving so ordering is still strict.
      this.counter += 1;
    }
    return { wall: this.wall, counter: this.counter, actor: this.actor };
  }

  /**
   * Merge a received timestamp, then produce one for the receive event.
   *
   * This is the step that makes causality hold: after observing a remote event, every
   * subsequent local event sorts after it, regardless of what the local wall clock says.
   */
  observe(remote: HLC): HLC {
    const physical = this.now();

    // A replica whose clock is wildly ahead would drag every other replica's clock forward
    // and keep it there — the bound turns a silent corruption into a loud, attributable error.
    if (remote.wall - physical > MAX_DRIFT_MS) {
      throw new ClockDriftError(remote, physical);
    }

    const maxWall = Math.max(physical, this.wall, remote.wall);
    if (maxWall === this.wall && maxWall === remote.wall) {
      this.counter = Math.max(this.counter, remote.counter) + 1;
    } else if (maxWall === this.wall) {
      this.counter += 1;
    } else if (maxWall === remote.wall) {
      this.counter = remote.counter + 1;
    } else {
      this.counter = 0;
    }
    this.wall = maxWall;
    return { wall: this.wall, counter: this.counter, actor: this.actor };
  }
}

/**
 * Total order over HLCs. Returns <0, 0, >0.
 *
 * The actor tiebreak is what makes this a TOTAL order rather than a partial one. Two replicas
 * can produce identical (wall, counter) pairs while disconnected; without a deterministic third
 * key they would disagree about which won, and LWW would converge to different values on
 * different machines — the exact failure a CRDT exists to prevent.
 */
export function compare(a: HLC, b: HLC): number {
  if (a.wall !== b.wall) return a.wall - b.wall;
  if (a.counter !== b.counter) return a.counter - b.counter;
  return a.actor < b.actor ? -1 : a.actor > b.actor ? 1 : 0;
}

export const isAfter = (a: HLC, b: HLC): boolean => compare(a, b) > 0;

/** Compact sortable encoding, used as a map key and for stable sort in the op log. */
export function encode(t: HLC): string {
  return `${t.wall.toString(16).padStart(12, "0")}:${t.counter
    .toString(16)
    .padStart(8, "0")}:${t.actor}`;
}

export function decode(s: string): HLC {
  // An actor id may itself contain ":", so the split is bounded to the two fixed-width
  // fields and everything after is the actor. `?? ""` keeps this total under
  // noUncheckedIndexedAccess rather than asserting non-null on parser output.
  const parts = s.split(":");
  return {
    wall: parseInt(parts[0] ?? "0", 16),
    counter: parseInt(parts[1] ?? "0", 16),
    actor: parts.slice(2).join(":"),
  };
}
