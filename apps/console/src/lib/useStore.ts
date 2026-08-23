/**
 * React bindings for the local store.
 *
 * `useSyncExternalStore` rather than a state library. The store is already the source of
 * truth and already has a subscribe/snapshot shape; wrapping it in Redux or Zustand would add
 * a second copy of state whose only job is to be kept in sync with the first — which is the
 * bug class this whole application is about.
 *
 * The one rule that makes it work: snapshots must be REFERENTIALLY STABLE between changes.
 * `useSyncExternalStore` compares with Object.is and re-renders forever if a fresh object
 * comes back each call. `LocalStore.getStatus` caches for that reason; the derived selectors
 * here memoise against a version counter for the same reason.
 */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { EntityKind } from "@rainmaker/crdt";
import { LocalStore, type StoreStatus } from "./store";

export function useStoreStatus(store: LocalStore): StoreStatus {
  return useSyncExternalStore(store.subscribe, store.getStatus, store.getStatus);
}

/**
 * Re-render on any store change, returning a monotonically increasing version.
 *
 * Deliberately coarse. The alternative — per-entity subscriptions — is a large amount of
 * bookkeeping to avoid re-rendering a board that has at most a few hundred cards, and the
 * bookkeeping is where staleness bugs come from. If a workspace ever gets big enough for this
 * to matter, the fix is virtualisation, not finer subscriptions.
 */
export function useStoreVersion(store: LocalStore): number {
  const version = useRef(0);
  const bump = useCallback(() => {
    version.current += 1;
  }, []);
  return useSyncExternalStore(
    (onChange) =>
      store.subscribe(() => {
        bump();
        onChange();
      }),
    () => version.current,
    () => version.current,
  );
}

export interface DealView {
  id: string;
  name: string;
  account: string;
  stage: string;
  amount: number;
  owner: string;
  tags: string[];
  intent: number;
  noteLength: number;
}

export function useDeals(store: LocalStore): DealView[] {
  const version = useStoreVersion(store);
  return useMemo(() => {
    void version;
    return store.replica.list("deal").map((entity) => {
      const id = entity.id;
      const f = <T,>(name: string, fallback: T): T =>
        (store.replica.field<T>("deal", id, name) ?? fallback) as T;
      return {
        id,
        name: f("name", "Untitled deal"),
        account: f("account", ""),
        stage: f("stage", "discovery"),
        amount: Number(f("amount", 0)) || 0,
        owner: f("owner", ""),
        tags: store.replica.tags("deal", id),
        intent: Number(f("intent", 0)) || 0,
        noteLength: store.replica.text("deal", id, "notes").length,
      };
    });
  }, [store, version]);
}

export function useDeal(store: LocalStore, id: string | null): DealView | null {
  const deals = useDeals(store);
  return useMemo(() => deals.find((d) => d.id === id) ?? null, [deals, id]);
}

export function useText(store: LocalStore, kind: EntityKind, id: string | null, field: string) {
  const version = useStoreVersion(store);
  return useMemo(() => {
    void version;
    return id ? store.replica.text(kind, id, field) : "";
  }, [store, kind, id, field, version]);
}

/** Theme, persisted, tolerant of a browser that refuses storage (private mode). */
export function useTheme(): [string, () => void] {
  const [theme, setTheme] = useState<string>(() => {
    try {
      return localStorage.getItem("rainmaker-theme") ?? "dark";
    } catch {
      return "dark";
    }
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("rainmaker-theme", theme);
    } catch {
      /* private mode: the theme simply does not persist */
    }
  }, [theme]);
  return [theme, () => setTheme((t) => (t === "dark" ? "light" : "dark"))];
}

/**
 * A store that is opened once and closed on unmount.
 *
 * Guarded against React 18 StrictMode's deliberate double-invoke in development: without the
 * guard the second mount opens a second WebSocket and a second IndexedDB handle, and the
 * symptom is duplicated ops that only appear in dev.
 */
export function useLocalStore(actor: string): LocalStore | null {
  const [store, setStore] = useState<LocalStore | null>(null);
  useEffect(() => {
    const instance = new LocalStore(actor);
    let cancelled = false;
    void instance.open().then(() => {
      if (!cancelled) setStore(instance);
    });
    return () => {
      cancelled = true;
      instance.close();
    };
  }, [actor]);
  return store;
}
