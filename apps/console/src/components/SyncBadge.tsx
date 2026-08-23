/**
 * The sync indicator.
 *
 * This is the most important pixel in a local-first app. The user needs to know, without
 * asking, whether their work is safe — and the honest answer has three states, not two:
 *
 *   live      everything is on the server
 *   offline   nothing is being lost; N edits are queued locally and will send
 *   syncing   catching up right now
 *
 * The offline state is deliberately NOT styled as an error. It is a normal, supported mode of
 * operation, and a red alarm would train reps to distrust a system that is working correctly.
 * It is amber and it states the queue depth, because "3 changes waiting" is reassuring in a
 * way that "offline" alone is not.
 */

import type { StoreStatus } from "../lib/store";

const LABEL: Record<StoreStatus["connection"], string> = {
  live: "Synced",
  syncing: "Syncing",
  connecting: "Connecting",
  offline: "Offline",
};

export function SyncBadge({ status }: { status: StoreStatus }) {
  const { connection, pending, lastSyncedAt } = status;
  const title =
    connection === "offline" && pending > 0
      ? `${pending} change${pending === 1 ? "" : "s"} saved on this device, waiting to sync`
      : lastSyncedAt
        ? `Last synced ${new Date(lastSyncedAt).toLocaleTimeString()}`
        : "Not yet synced";

  return (
    <span className="sync" data-state={connection} title={title}>
      <span className="sync-dot" aria-hidden />
      {LABEL[connection]}
      {pending > 0 && <span className="sync-pending">{pending}</span>}
    </span>
  );
}
