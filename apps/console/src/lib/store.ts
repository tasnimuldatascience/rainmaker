/**
 * The store: a CRDT replica that survives a page reload and a dead network.
 *
 * THE INVARIANT THIS FILE EXISTS TO HOLD: a write succeeds locally, always, immediately, with
 * no network involved. Everything else — persistence, queueing, reconnection, replay — is
 * machinery in service of that one promise. The moment a write can fail because the server is
 * unreachable, the product is a normal web app with a spinner, and the rep on a train is stuck.
 *
 * The pieces:
 *
 *   Replica          in-memory CRDT (packages/crdt). The source of truth for rendering.
 *   IndexedDB        durable op log, written BEFORE the UI updates. A reload replays it.
 *   outbox           ops not yet acknowledged by the server. Drained on reconnect.
 *   checkpoint       the last server sequence we have. Resume point, so a reconnect is one
 *                    round trip rather than a full resync.
 *
 * ORDERING RULE, and it is the one that matters: persist locally, then apply, then attempt to
 * send. Applying before persisting means a crash between the two shows the user a change that
 * no longer exists after reload. Sending before persisting means the server can hold an op the
 * client has forgotten.
 */

import { Replica, type EntityKind, type Op } from "@rainmaker/crdt";

const DB_NAME = "rainmaker";
const DB_VERSION = 1;
const OPS = "ops";
const META = "meta";

export type ConnectionState = "offline" | "connecting" | "live" | "syncing";

export interface StoreStatus {
  connection: ConnectionState;
  pending: number;
  checkpoint: number;
  actor: string;
  lastError: string | null;
  lastSyncedAt: number | null;
  /**
   * The relay refused this replica, and retrying will not change that.
   *
   * DISTINCT FROM OFFLINE ON PURPOSE. "We cannot reach the server" resolves itself; "you are
   * not a member of this workspace" does not, and showing the first when the second is true
   * leaves someone watching a spinner for a permission problem.
   */
  forbidden: boolean;
}

type Listener = () => void;

// ─────────────────────────────────────────────────────────────── IndexedDB
function openDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(OPS)) {
        // Keyed by op id: replaying a retried op is a put over itself, not a duplicate row.
        const store = db.createObjectStore(OPS, { keyPath: "id" });
        store.createIndex("acked", "acked", { unique: false });
      }
      if (!db.objectStoreNames.contains(META)) db.createObjectStore(META);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function tx<T>(
  db: IDBDatabase,
  store: string,
  mode: IDBTransactionMode,
  fn: (s: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(store, mode);
    const request = fn(transaction.objectStore(store));
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    transaction.onabort = () => reject(transaction.error);
  });
}

interface StoredOp {
  id: string;
  op: Op;
  acked: 0 | 1;
}

// ─────────────────────────────────────────────────────────────── the store
export class LocalStore {
  readonly replica: Replica;
  readonly clientId = crypto.randomUUID();

  private db: IDBDatabase | null = null;
  private socket: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private outbox = new Map<string, Op>();
  private checkpoint = 0;
  private connection: ConnectionState = "offline";
  private lastError: string | null = null;
  private lastSyncedAt: number | null = null;
  private reconnectAttempt = 0;
  private reconnectTimer: number | null = null;
  private closed = false;
  /**
   * The signed grant that says this actor may write to this workspace.
   *
   * NOT A SECRET THE USER TYPES, and not one this client mints. It comes from the relay, it
   * names the actor, and the relay checks it on every socket and every flush. The actor id in
   * the query string used to BE the claim; now it is only a hint the token has to agree with.
   */
  private token = "";
  /**
   * Resolves once we know what the server has — or once it is clear we will not find out.
   *
   * Exists for exactly one caller: the demo seed, which must not write to a shared workspace
   * on the strength of a local replica that has simply not caught up yet.
   */
  private firstSyncResolve: (() => void) | null = null;
  private readonly firstSyncDone = new Promise<void>((resolve) => {
    this.firstSyncResolve = resolve;
  });
  /** Refused rather than disconnected: retrying will not help until membership changes. */
  private forbidden = false;

  constructor(
    readonly actor: string,
    private readonly workspace = "demo",
  ) {
    this.replica = new Replica(actor);
  }

  // ── lifecycle ──────────────────────────────────────────────────────────
  async open(): Promise<void> {
    this.db = await openDB();
    await this.hydrate();
    // Browser online/offline events are a hint, not truth — a captive portal reports
    // "online" while nothing routes. They are used to retry SOONER, never to conclude the
    // connection is healthy; only a working socket does that.
    window.addEventListener("online", this.onOnline);
    window.addEventListener("offline", this.onOffline);
    // A TOKEN IS NOT REQUIRED TO START WORKING. Fetching it is a network call, and this
    // console's whole premise is that an edit lands on the device whether or not the network
    // is there. So the token is fetched on the way to the socket and the failure to get one is
    // an ordinary disconnected state: edits queue, and the next attempt asks again.
    await this.authorise();
    this.connect();
  }

  /**
   * Exchange this replica's actor id for a signed grant.
   *
   * Kept separate from `connect` because it is also the recovery path: a token expires or a
   * membership is revoked, the relay closes with `unauthorised`, and the next attempt has to
   * ask for a new one rather than reconnecting with the same dead credential forever.
   */
  private async authorise(): Promise<boolean> {
    try {
      const res = await fetch("/api/sync/token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace: this.workspace, actor: this.actor }),
      });
      if (!res.ok) throw new Error(`token refused: ${res.status}`);
      const body = (await res.json()) as { token?: string };
      this.token = body.token ?? "";
      this.forbidden = false;
      return this.token !== "";
    } catch (err) {
      this.lastError = err instanceof Error ? err.message : String(err);
      this.emit();
      return false;
    }
  }

  close(): void {
    this.closed = true;
    window.removeEventListener("online", this.onOnline);
    window.removeEventListener("offline", this.onOffline);
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer);
    this.socket?.close();
    this.db?.close();
  }

  private onOnline = () => {
    this.reconnectAttempt = 0;
    this.connect();
  };

  private onOffline = () => {
    this.setConnection("offline");
  };

  /** Replay the durable log into the in-memory replica. The reload path. */
  private async hydrate(): Promise<void> {
    if (!this.db) return;
    const rows = await tx<StoredOp[]>(this.db, OPS, "readonly", (s) => s.getAll());
    // Sorted by op id, which encodes the HLC — so replay order is causal even though
    // IndexedDB returns key order. The CRDT would converge regardless; this just avoids
    // parking every insert as an orphan and re-draining it.
    rows.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    for (const row of rows) {
      this.replica.apply(row.op);
      if (!row.acked) this.outbox.set(row.id, row.op);
    }
    const saved = await tx<number | undefined>(this.db, META, "readonly", (s) =>
      s.get("checkpoint"),
    );
    this.checkpoint = saved ?? 0;
    this.emit();
  }

  // ── writing ────────────────────────────────────────────────────────────
  /**
   * Record locally-produced ops. Durable first, then visible, then sent.
   *
   * Returns immediately after the durable write — the caller never awaits the network.
   */
  async record(ops: Op[]): Promise<void> {
    if (!ops.length) return;
    if (this.db) {
      await Promise.all(
        ops.map((op) =>
          tx(this.db!, OPS, "readwrite", (s) => s.put({ id: op.id, op, acked: 0 })),
        ),
      );
    }
    for (const op of ops) this.outbox.set(op.id, op);
    this.emit();
    void this.flush();
  }

  /** Mutation helpers. Each emits ops, applies them locally, and returns synchronously. */
  setField(kind: EntityKind, id: string, field: string, value: unknown): void {
    void this.record([this.replica.set(kind, id, field, value)]);
  }

  addTag(kind: EntityKind, id: string, tag: string): void {
    void this.record([this.replica.addTag(kind, id, tag)]);
  }

  removeTag(kind: EntityKind, id: string, tag: string): void {
    void this.record([this.replica.removeTag(kind, id, tag)]);
  }

  editText(kind: EntityKind, id: string, field: string, next: string): void {
    // Diff the rendered text against the new value and emit the minimal edit. Replacing the
    // whole field would delete and re-insert every character on every keystroke, which
    // destroys concurrent edits from anyone else in the same note.
    const current = this.replica.text(kind, id, field);
    const ops = diffToOps(this.replica, kind, id, field, current, next);
    if (ops.length) void this.record(ops);
  }

  // ── network ────────────────────────────────────────────────────────────
  private connect(): void {
    if (this.closed || this.socket) return;
    // WITHOUT A GRANT THERE IS NOTHING TO CONNECT WITH. Opening the socket anyway would be
    // refused, count as a failed attempt, and back the retry off — so a missing token would
    // look like a flaky network and get slower instead of being fixed.
    if (!this.token) {
      void this.authorise().then((ok) => {
        if (ok && !this.closed && !this.socket) this.connect();
        else this.scheduleReconnect();
      });
      return;
    }
    this.setConnection("connecting");

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url =
      `${proto}://${location.host}/api/sync/ws` +
      `?workspace=${encodeURIComponent(this.workspace)}` +
      `&token=${encodeURIComponent(this.token)}` +
      `&since=${this.checkpoint}`;

    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch (err) {
      this.fail(err);
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.reconnectAttempt = 0;
      this.lastError = null;
      this.setConnection("syncing");
      void this.flush();
    };

    socket.onmessage = (event) => {
      try {
        this.receive(JSON.parse(event.data as string));
      } catch (err) {
        console.error("malformed sync frame", err);
      }
    };

    socket.onerror = () => {
      // Deliberately quiet: `onclose` always follows and owns the retry. Handling both
      // schedules two reconnects for one failure.
      this.lastError = "connection error";
    };

    socket.onclose = () => {
      this.socket = null;
      if (this.closed) return;
      this.setConnection("offline");
      this.scheduleReconnect();
    };
  }

  /**
   * Wait until this replica has heard from the relay, or give up after `ms`.
   *
   * The timeout resolves rather than rejects: a caller asking "has the server spoken" wants an
   * answer, and "no" is one. What it must never do is hang the console on a network that is not
   * coming back.
   */
  async whenSynced(ms = 4000): Promise<void> {
    let timer: number | undefined;
    await Promise.race([
      this.firstSyncDone,
      new Promise<void>((resolve) => {
        timer = window.setTimeout(resolve, ms);
      }),
    ]);
    if (timer !== undefined) window.clearTimeout(timer);
  }

  /** True once the relay has sent us anything at all. */
  get hasHeardFromServer(): boolean {
    return this.firstSyncResolve === null;
  }

  private settleFirstSync(): void {
    if (this.firstSyncResolve) {
      this.firstSyncResolve();
      this.firstSyncResolve = null;
    }
  }

  private scheduleReconnect(): void {
    if (this.closed || this.reconnectTimer !== null) return;
    // Exponential backoff with jitter, capped at 30s. Jitter matters: without it every
    // client that dropped during one server restart reconnects in the same millisecond and
    // knocks it over again.
    const base = Math.min(30_000, 500 * 2 ** this.reconnectAttempt);
    const delay = base * (0.5 + Math.random() * 0.5);
    this.reconnectAttempt += 1;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private receive(message: {
    type: string;
    ops?: Op[];
    head?: number;
    more?: boolean;
    detail?: string;
  }): void {
    switch (message.type) {
      // THE GRANT WENT STALE, WHICH IS NOT THE NETWORK FAILING. A token expires, or a
      // membership is revoked, and reconnecting with the same credential produces the same
      // refusal forever. Drop it so the next attempt asks for a new one; if the relay refuses
      // to issue, the outbox keeps the edits and the badge stops claiming to be syncing.
      case "unauthorised": {
        this.token = "";
        this.lastError = message.detail ?? "not authorised for this workspace";
        this.emit();
        return;
      }
      // One message was refused, the socket is still good. The ops stay in the outbox, so
      // this is a report rather than a recovery.
      case "rejected": {
        this.lastError = message.detail ?? "write rejected";
        this.emit();
        return;
      }
      case "catchup":
      case "ops": {
        // The first frame from the relay is the answer to "what is already here".
        this.settleFirstSync();
        const ops = message.ops ?? [];
        for (const op of ops) this.replica.apply(op);
        if (typeof message.head === "number" && message.head > this.checkpoint) {
          this.checkpoint = message.head;
          void this.persistCheckpoint();
        }
        this.lastSyncedAt = Date.now();
        this.setConnection(message.more ? "syncing" : "live");
        this.emit();
        break;
      }
      case "resync":
        // The server dropped us for falling behind. Reconnecting from our checkpoint is the
        // whole recovery: the log is sequenced, so nothing is lost.
        this.socket?.close();
        break;
      case "pong":
        break;
    }
  }

  /** Send everything unacknowledged. Safe to call at any time; the server deduplicates. */
  private async flush(): Promise<void> {
    if (!this.outbox.size) return;
    const pending = [...this.outbox.values()];

    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: "ops", ops: pending }));
      await this.ack(pending);
      return;
    }

    // No socket: try the HTTP path. It works in situations the WebSocket does not (some
    // corporate proxies), so it is a genuine fallback rather than a duplicate.
    try {
      const res = await fetch("/api/sync/append", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workspace: this.workspace,
          ops: pending,
          client_id: this.clientId,
          token: this.token,
        }),
      });
      // 401 IS NOT A NETWORK PROBLEM. Retrying the same dead token forever is how a revoked
      // member's edits sit in an outbox that never drains and never says why. Ask for a new
      // grant once; if that fails the ops stay queued and the badge stays honest.
      if (res.status === 401) {
        this.token = "";
        this.forbidden = !(await this.authorise());
        throw new Error("append rejected: not authorised for this workspace");
      }
      if (!res.ok) throw new Error(`append failed: ${res.status}`);
      await this.ack(pending);
      this.lastSyncedAt = Date.now();
    } catch (err) {
      // Staying in the outbox IS the retry. No error surfaces to the user, because from
      // their point of view the write already succeeded — which it did, locally.
      this.lastError = err instanceof Error ? err.message : String(err);
      this.emit();
    }
  }

  private async ack(ops: Op[]): Promise<void> {
    for (const op of ops) this.outbox.delete(op.id);
    if (this.db) {
      await Promise.all(
        ops.map((op) =>
          tx(this.db!, OPS, "readwrite", (s) => s.put({ id: op.id, op, acked: 1 })),
        ),
      );
    }
    this.emit();
  }

  private async persistCheckpoint(): Promise<void> {
    if (!this.db) return;
    await tx(this.db, META, "readwrite", (s) => s.put(this.checkpoint, "checkpoint"));
  }

  private fail(err: unknown): void {
    this.lastError = err instanceof Error ? err.message : String(err);
    this.setConnection("offline");
    this.scheduleReconnect();
  }

  // ── subscription ───────────────────────────────────────────────────────
  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private cachedStatus: StoreStatus | null = null;

  getStatus = (): StoreStatus => {
    // useSyncExternalStore compares snapshots by identity and will loop forever if a new
    // object is returned every call. The cache is invalidated only in emit().
    if (this.cachedStatus === null) {
      this.cachedStatus = {
        connection: this.connection,
        pending: this.outbox.size,
        checkpoint: this.checkpoint,
        actor: this.actor,
        lastError: this.lastError,
        lastSyncedAt: this.lastSyncedAt,
        forbidden: this.forbidden,
      };
    }
    return this.cachedStatus;
  };

  private setConnection(next: ConnectionState): void {
    if (this.connection !== next) {
      this.connection = next;
      this.emit();
    }
  }

  private emit(): void {
    this.cachedStatus = null;
    for (const listener of this.listeners) listener();
  }
}

// ─────────────────────────────────────────────────────────────── text diffing
/**
 * Minimal edit between two strings, as CRDT ops.
 *
 * Common prefix + common suffix, then delete the middle and insert the replacement. Not a
 * full Myers diff: for interactive typing the changed region is almost always one character,
 * and the prefix/suffix scan finds that in O(n) with no allocation. A full diff would buy
 * better behaviour only for paste-over-selection, which this already handles correctly if
 * less minimally.
 *
 * INDICES ARE CODE POINTS, NOT UTF-16 UNITS, and that distinction is the whole reason this
 * function is not four lines shorter. `Replica.insertText` builds its sequence with
 * `for (const char of str)`, which iterates code points -- so an emoji is ONE element in the
 * text and every index the replica accepts is an index into code points.
 *
 * A string's `.length` and `[i]` count UTF-16 units instead, where an emoji is two. Mixing the
 * two puts every edit after an emoji one position too far right, per emoji. It does not throw:
 * it produces valid operations that quietly corrupt the note, and only for people who type
 * emoji -- which, in sales notes, is most of them.
 */
function diffToOps(
  replica: Replica,
  kind: EntityKind,
  id: string,
  field: string,
  before: string,
  after: string,
): Op[] {
  if (before === after) return [];

  // Array.from splits on code points, matching how the replica indexes its text.
  const a = Array.from(before);
  const b = Array.from(after);

  let start = 0;
  const max = Math.min(a.length, b.length);
  while (start < max && a[start] === b[start]) start += 1;

  let end = 0;
  while (end < max - start && a[a.length - 1 - end] === b[b.length - 1 - end]) {
    end += 1;
  }

  const removed = a.length - start - end;
  const inserted = b.slice(start, b.length - end).join("");

  const ops: Op[] = [];
  if (removed > 0) ops.push(...replica.deleteText(kind, id, field, start, removed));
  if (inserted) ops.push(...replica.insertText(kind, id, field, start, inserted));
  return ops;
}

export { diffToOps };
