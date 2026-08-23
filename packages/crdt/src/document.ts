/**
 * The replica: applies ops to materialised state, and produces ops from user intent.
 *
 * DESIGN RULE — the reducer is total and order-independent. `apply()` must accept any op at
 * any time, including ops it has already seen, ops for entities it has never heard of, and
 * ops whose causal predecessors have not arrived. There is no "invalid order" branch, because
 * on a real network there is no order. Anything that would need one is a bug in the op design,
 * not something to handle at apply time.
 *
 * That is why there is no causal buffering here. RGA inserts are the only ops with a
 * dependency (`after` must exist), and they are handled by parking orphans until their parent
 * arrives rather than by rejecting them — see `insertChar`.
 */

import { compare, encode, HybridClock, type HLC } from "./clock";
import type {
  AddTagOp,
  DeleteTextOp,
  EntityKind,
  EntityState,
  InsertTextOp,
  Op,
  RemoveTagOp,
  SetOp,
  TextChar,
  TextState,
} from "./types";

const key = (kind: EntityKind, id: string) => `${kind}:${id}`;

export class Replica {
  readonly clock: HybridClock;
  private readonly entities = new Map<string, EntityState>();
  /** Op ids already applied. The idempotence guard. */
  private readonly seen = new Set<string>();
  /** RGA inserts whose `after` has not arrived yet, keyed by the missing parent id. */
  private readonly orphans = new Map<string, InsertTextOp[]>();
  private seq = 0;

  constructor(
    readonly actor: string,
    now: () => number = () => Date.now(),
  ) {
    this.clock = new HybridClock(actor, now);
  }

  // ─────────────────────────────────────────────────────────── reading
  get(kind: EntityKind, id: string): EntityState | undefined {
    return this.entities.get(key(kind, id));
  }

  field<T = unknown>(kind: EntityKind, id: string, name: string): T | undefined {
    return this.entities.get(key(kind, id))?.fields[name]?.value as T | undefined;
  }

  tags(kind: EntityKind, id: string): string[] {
    const e = this.entities.get(key(kind, id));
    if (!e) return [];
    // A tag is present iff at least one un-removed instance survives.
    return [...e.tags.entries()]
      .filter(([, instances]) => instances.size > 0)
      .map(([tag]) => tag)
      .sort();
  }

  text(kind: EntityKind, id: string, field: string): string {
    const e = this.entities.get(key(kind, id));
    const t = e?.text[field];
    if (!t) return "";
    if (t.cache !== null) return t.cache;
    const rendered = renderText(t);
    t.cache = rendered;
    return rendered;
  }

  list(kind: EntityKind): EntityState[] {
    return [...this.entities.values()].filter((e) => e.kind === kind && e.exists);
  }

  // ─────────────────────────────────────────────────────────── writing
  set(kind: EntityKind, id: string, field: string, value: unknown): SetOp {
    return this.emit<SetOp>({ type: "set", kind, entityId: id, field, value });
  }

  addTag(kind: EntityKind, id: string, tag: string): AddTagOp {
    return this.emit<AddTagOp>({
      type: "addTag",
      kind,
      entityId: id,
      tag,
      instance: `${this.actor}:${this.seq}`,
    });
  }

  removeTag(kind: EntityKind, id: string, tag: string): RemoveTagOp {
    // Name the instances THIS replica can see. Instances added concurrently elsewhere are
    // not named, so they survive — the defining property of an observed-remove set.
    const observed = this.entities.get(key(kind, id))?.tags.get(tag);
    return this.emit<RemoveTagOp>({
      type: "removeTag",
      kind,
      entityId: id,
      tag,
      instances: observed ? [...observed] : [],
    });
  }

  /** Insert `str` at character offset `index` of a text field. Emits one op per character. */
  insertText(kind: EntityKind, id: string, field: string, index: number, str: string): Op[] {
    const state = this.ensure(kind, id).text[field] ?? this.ensureText(kind, id, field);
    const visible = visibleIds(state);
    let after = index > 0 ? (visible[index - 1] ?? null) : null;
    const ops: Op[] = [];
    for (const char of str) {
      const op = this.emit<InsertTextOp>({
        type: "insertText",
        kind,
        entityId: id,
        field,
        after,
        charId: `${this.actor}:${this.seq}`,
        char,
      });
      ops.push(op);
      after = op.charId;
    }
    return ops;
  }

  deleteText(kind: EntityKind, id: string, field: string, index: number, count: number): Op[] {
    const state = this.ensure(kind, id).text[field];
    if (!state) return [];
    const visible = visibleIds(state);
    const ops: Op[] = [];
    for (const charId of visible.slice(index, index + count)) {
      ops.push(
        this.emit<DeleteTextOp>({ type: "deleteText", kind, entityId: id, field, charId }),
      );
    }
    return ops;
  }

  private emit<T extends Op>(partial: Omit<T, "id" | "ts" | "actor">): T {
    const ts = this.clock.tick();
    this.seq += 1;
    const op = {
      ...partial,
      ts,
      actor: this.actor,
      id: `${encode(ts)}#${this.seq}`,
    } as T;
    this.apply(op);
    return op;
  }

  // ─────────────────────────────────────────────────────────── applying
  /** Returns true if the op changed state (false = duplicate, already applied). */
  apply(op: Op): boolean {
    if (this.seen.has(op.id)) return false;
    this.seen.add(op.id);

    // Remote ops advance the clock so every subsequent local edit sorts after what we saw.
    if (op.actor !== this.actor) this.clock.observe(op.ts);

    const entity = this.ensure(op.kind, op.entityId);
    switch (op.type) {
      case "set":
        return this.applySet(entity, op);
      case "addTag":
        return this.applyAddTag(entity, op);
      case "removeTag":
        return this.applyRemoveTag(entity, op);
      case "insertText":
        return this.applyInsert(entity, op);
      case "deleteText":
        return this.applyDelete(entity, op);
    }
  }

  applyAll(ops: Iterable<Op>): number {
    let n = 0;
    for (const op of ops) if (this.apply(op)) n += 1;
    return n;
  }

  private applySet(entity: EntityState, op: SetOp): boolean {
    const existing = entity.fields[op.field];
    // Strictly-greater, not greater-or-equal. With the actor tiebreak in `compare`, equality
    // means the identical op — so re-applying must not "win" and must not mutate.
    if (existing && compare(op.ts, existing.ts) <= 0) return false;
    entity.fields[op.field] = { value: op.value, ts: op.ts };
    return true;
  }

  private applyAddTag(entity: EntityState, op: AddTagOp): boolean {
    // A removal may already have cancelled this exact instance, arriving before the add.
    // Re-adding it here would resurrect a tag the user deleted, purely because of network
    // ordering -- and it would resurrect it on only SOME replicas, which is divergence.
    if (entity.removedInstances.get(op.tag)?.has(op.instance)) return false;
    const set = entity.tags.get(op.tag) ?? new Set<string>();
    set.add(op.instance);
    entity.tags.set(op.tag, set);
    return true;
  }

  private applyRemoveTag(entity: EntityState, op: RemoveTagOp): boolean {
    // Record the cancellation FIRST and unconditionally. Whether the corresponding adds have
    // arrived is irrelevant: what matters is that these instances can never become live again
    // on any replica, in any delivery order. Only cancelling instances that happen to be
    // present right now makes the outcome depend on arrival order, which is divergence.
    const cancelled = entity.removedInstances.get(op.tag) ?? new Set<string>();
    for (const instance of op.instances) cancelled.add(instance);
    entity.removedInstances.set(op.tag, cancelled);

    const live = entity.tags.get(op.tag);
    if (live) for (const instance of op.instances) live.delete(instance);
    return true;
  }

  private applyInsert(entity: EntityState, op: InsertTextOp): boolean {
    const state = entity.text[op.field] ?? (entity.text[op.field] = emptyText());
    if (state.chars.has(op.charId)) return false;

    // Park the insert until its parent arrives. Rejecting instead would lose the character
    // permanently on any out-of-order delivery, which a lossy channel produces routinely.
    if (op.after !== null && !state.chars.has(op.after)) {
      const queue = this.orphans.get(op.after) ?? [];
      queue.push(op);
      this.orphans.set(op.after, queue);
      return false;
    }
    insertChar(state, {
      id: op.charId,
      char: op.char,
      after: op.after,
      deleted: state.pendingDeletes.delete(op.charId),
    });
    state.cache = null;
    this.drainOrphans(entity, op.charId);
    return true;
  }

  private drainOrphans(entity: EntityState, parentId: string): void {
    const queue = this.orphans.get(parentId);
    if (!queue) return;
    this.orphans.delete(parentId);
    for (const op of queue) {
      // The op is already in `seen`; re-run the insert body directly.
      const state = entity.text[op.field] ?? (entity.text[op.field] = emptyText());
      insertChar(state, {
        id: op.charId,
        char: op.char,
        after: op.after,
        deleted: state.pendingDeletes.delete(op.charId),
      });
      state.cache = null;
      this.drainOrphans(entity, op.charId);
    }
  }

  private applyDelete(entity: EntityState, op: DeleteTextOp): boolean {
    const state = entity.text[op.field] ?? (entity.text[op.field] = emptyText());
    const ch = state.chars.get(op.charId);
    if (!ch) {
      // The delete beat its insert. Park the tombstone; applyInsert honours it on arrival.
      // Dropping it would make the character reappear on this replica only.
      state.pendingDeletes.add(op.charId);
      return true;
    }
    if (ch.deleted) return false;
    // Tombstone rather than remove: a concurrent insert may name this character as its
    // `after`, and dropping it outright would orphan that insert forever.
    ch.deleted = true;
    state.cache = null;
    return true;
  }

  private ensure(kind: EntityKind, id: string): EntityState {
    const k = key(kind, id);
    let e = this.entities.get(k);
    if (!e) {
      e = {
        kind, id,
        fields: {},
        tags: new Map(),
        removedInstances: new Map(),
        text: {},
        exists: true,
      };
      this.entities.set(k, e);
    }
    e.exists = true;
    return e;
  }

  private ensureText(kind: EntityKind, id: string, field: string): TextState {
    const e = this.ensure(kind, id);
    return (e.text[field] ??= emptyText());
  }

  /** Diagnostics: ops parked waiting for a causal parent. Should trend to zero. */
  get pendingOrphans(): number {
    return [...this.orphans.values()].reduce((n, q) => n + q.length, 0);
  }
}

// ───────────────────────────────────────────────────────────── RGA internals
function emptyText(): TextState {
  return { chars: new Map(), pendingDeletes: new Set(), cache: null };
}

/**
 * Insert into the RGA sequence.
 *
 * Concurrent inserts with the same `after` are ordered by character id, descending. Descending
 * so that a later id sorts FIRST among siblings, which reproduces the standard RGA behaviour:
 * the most recent concurrent insert appears closest to the anchor. Ascending would also
 * converge — the requirement is only that every replica picks the same rule — but descending
 * matches what users expect when two people type at the same spot.
 */
function insertChar(state: TextState, ch: TextChar): void {
  state.chars.set(ch.id, ch);
}

function childrenOf(state: TextState): Map<string | null, TextChar[]> {
  const byParent = new Map<string | null, TextChar[]>();
  for (const ch of state.chars.values()) {
    const list = byParent.get(ch.after) ?? [];
    list.push(ch);
    byParent.set(ch.after, list);
  }
  for (const list of byParent.values()) {
    list.sort((a, b) => (a.id < b.id ? 1 : a.id > b.id ? -1 : 0));
  }
  return byParent;
}

/** Depth-first walk of the insertion tree, yielding document order. */
function walk(state: TextState, includeDeleted: boolean): TextChar[] {
  const byParent = childrenOf(state);
  const out: TextChar[] = [];
  // Iterative rather than recursive: a long note is thousands of characters deep and
  // recursion blows the stack in the browser well before the document feels large.
  const stack: (string | null)[] = [null];
  const emitted = new Set<string | null>();
  const pending: TextChar[][] = [[...(byParent.get(null) ?? [])]];
  stack.pop();

  while (pending.length) {
    const frame = pending[pending.length - 1];
    const next = frame.shift();
    if (!next) {
      pending.pop();
      continue;
    }
    if (emitted.has(next.id)) continue;
    emitted.add(next.id);
    if (includeDeleted || !next.deleted) out.push(next);
    pending.push([...(byParent.get(next.id) ?? [])]);
  }
  return out;
}

function visibleIds(state: TextState): string[] {
  return walk(state, false).map((c) => c.id);
}

function renderText(state: TextState): string {
  return walk(state, false)
    .map((c) => c.char)
    .join("");
}

export { renderText, visibleIds };
export type { HLC };
