/**
 * The operation vocabulary.
 *
 * Everything a replica does is one of these, and every one of them is designed to be
 * COMMUTATIVE and IDEMPOTENT. That pair of properties is what lets the server be a dumb,
 * durable relay instead of a merge engine:
 *
 *   commutative  replicas that receive the same ops in different orders converge
 *   idempotent   a retried delivery (the common case on a flaky connection) is a no-op
 *
 * Neither property is free. `set` is commutative only because the winner is decided by HLC
 * rather than by arrival order. `addTag`/`removeTag` are commutative only because removal
 * carries the exact tag instances it observed — a naive "remove by value" is NOT commutative
 * and loses concurrent re-adds. `insertText`/`deleteText` are commutative only because
 * positions are identifiers rather than indices; an index-based edit is the classic
 * non-convergent operation.
 *
 * If you add an op type, the test suite's convergence property test will tell you whether you
 * got it right: it replays random permutations across replicas and asserts identical state.
 */

import type { HLC } from "./clock";

/** Every entity the console can edit. Ops are namespaced by (entity, id). */
export type EntityKind = "deal" | "contact" | "account" | "note" | "task";

export interface OpBase {
  /** Globally unique: HLC encoding plus a per-op sequence, so retries are detectable. */
  readonly id: string;
  readonly ts: HLC;
  readonly actor: string;
  readonly kind: EntityKind;
  readonly entityId: string;
}

/** Last-writer-wins register. Deal stage, amount, owner, close date. */
export interface SetOp extends OpBase {
  readonly type: "set";
  readonly field: string;
  readonly value: unknown;
}

/**
 * Observed-remove set. `addTag` mints a unique instance id; `removeTag` names the instances
 * it saw.
 *
 * The alternative — removing by value — loses data under concurrency: replica A removes
 * "enterprise" while replica B adds it, and the remove wins even though it never observed
 * B's add. Under OR-Set semantics B's add survives, which is the intuitively correct outcome
 * ("someone added it after, or without seeing, the removal").
 */
export interface AddTagOp extends OpBase {
  readonly type: "addTag";
  readonly tag: string;
  readonly instance: string;
}

export interface RemoveTagOp extends OpBase {
  readonly type: "removeTag";
  readonly tag: string;
  /** The instance ids this replica had observed at removal time. */
  readonly instances: readonly string[];
}

/**
 * Sequence insert, RGA-style. `after` is the id of the character to insert following, or
 * null for the head of the document.
 *
 * Identifier-based rather than index-based: indices shift under concurrent edits and two
 * replicas applying the same index-based ops in different orders produce different text.
 * This is the single most common way a hand-rolled collaborative editor breaks.
 */
export interface InsertTextOp extends OpBase {
  readonly type: "insertText";
  readonly field: string;
  readonly after: string | null;
  readonly charId: string;
  readonly char: string;
}

/** Tombstone a character by id. Idempotent by construction. */
export interface DeleteTextOp extends OpBase {
  readonly type: "deleteText";
  readonly field: string;
  readonly charId: string;
}

export type Op = SetOp | AddTagOp | RemoveTagOp | InsertTextOp | DeleteTextOp;

/**
 * Materialised entity state. Derived from ops; never edited directly.
 *
 * THE TOMBSTONE FIELDS ARE NOT OPTIONAL BOOKKEEPING. A cancelling operation — a tag removal,
 * a character deletion — routinely arrives BEFORE the operation it cancels. On a network with
 * no ordering guarantee this is not an edge case, it is a Tuesday.
 *
 * If a cancellation with nothing to cancel is dropped, the thing it was meant to remove comes
 * back when it finally arrives, and the two replicas disagree forever. Both `removedInstances`
 * and `TextState.pendingDeletes` exist so a cancellation is durable regardless of when it
 * lands. This was caught by the convergence property test, not by inspection; the shrunk
 * counterexample was two operations long — insert a character, delete it — and it diverged.
 */
export interface EntityState {
  readonly kind: EntityKind;
  readonly id: string;
  fields: Record<string, { value: unknown; ts: HLC }>;
  tags: Map<string, Set<string>>;
  /** Tag instances cancelled by some removal, whether or not the add has arrived yet. */
  removedInstances: Map<string, Set<string>>;
  text: Record<string, TextState>;
  /** True once any op has touched this entity — distinguishes "empty" from "absent". */
  exists: boolean;
}

export interface TextChar {
  readonly id: string;
  readonly char: string;
  readonly after: string | null;
  deleted: boolean;
}

export interface TextState {
  chars: Map<string, TextChar>;
  /** Character ids deleted by an op that arrived before the insert it cancels. */
  pendingDeletes: Set<string>;
  /** Rendered-order cache, invalidated on write. */
  cache: string | null;
}

export const OP_TYPES = [
  "set",
  "addTag",
  "removeTag",
  "insertText",
  "deleteText",
] as const;
