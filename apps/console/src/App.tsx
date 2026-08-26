/**
 * The console shell.
 *
 * Three surfaces, one local replica behind all of them:
 *
 *   Pipeline  the board. Every edit is a local CRDT op; the network is incidental.
 *   Calendar  what the agent booked, read back through the same tool it books with.
 *   Research  the browser agent's output, rendered with provenance so a rep can see the
 *             difference between "their site says this" and "a model thinks this".
 *   Call      the live agent: disclosure, transcript, and the measured latency budget.
 *
 * Routing is a `useState`. A router is 15kb and a second source of truth for one of three
 * views; the back button is wired to history directly instead.
 */

import { useCallback, useEffect, useState } from "react";
import { CalendarView } from "./components/CalendarView";
import { CallView } from "./components/CallView";
import { DealDrawer } from "./components/DealDrawer";
import { PipelineBoard } from "./components/PipelineBoard";
import { ResearchPanel } from "./components/ResearchPanel";
import { SyncBadge } from "./components/SyncBadge";
import { seedIfEmpty } from "./lib/seed";
import { useLocalStore, useStoreStatus, useTheme } from "./lib/useStore";

type View = "pipeline" | "research" | "calendar" | "call";

const VIEWS: { id: View; label: string }[] = [
  { id: "pipeline", label: "Pipeline" },
  { id: "research", label: "Research" },
  { id: "calendar", label: "Calendar" },
  { id: "call", label: "Live call" },
];

/**
 * Identity is per browser profile and generated once.
 *
 * A real deployment authenticates; this is a portfolio build with no auth server, and being
 * explicit about that is better than a fake login screen. The actor id is what the CRDT uses
 * to break concurrent-write ties, so it must be stable across reloads — a fresh id every
 * session would make the same person look like a new replica each time.
 */
function useActor(): string {
  const [actor] = useState(() => {
    try {
      const saved = localStorage.getItem("rainmaker-actor");
      if (saved) return saved;
      const minted = `rep-${crypto.randomUUID().slice(0, 8)}`;
      localStorage.setItem("rainmaker-actor", minted);
      return minted;
    } catch {
      return `rep-${Math.random().toString(36).slice(2, 10)}`;
    }
  });
  return actor;
}

export default function App() {
  const actor = useActor();
  const store = useLocalStore(actor);
  const [view, setView] = useState<View>("pipeline");
  const [selected, setSelected] = useState<string | null>(null);
  const [theme, toggleTheme] = useTheme();

  useEffect(() => {
    if (store) void seedIfEmpty(store);
  }, [store]);

  const openDeal = useCallback((id: string) => setSelected(id), []);

  if (!store) {
    return (
      <div className="app">
        <div className="page">
          <div className="stats">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="skeleton" style={{ blockSize: 92 }} />
            ))}
          </div>
          <div className="skeleton" style={{ blockSize: 320 }} />
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <Topbar
        view={view}
        onView={setView}
        store={store}
        theme={theme}
        onToggleTheme={toggleTheme}
      />
      <main className="main">
        {view === "pipeline" && <PipelineBoard store={store} onOpen={openDeal} selected={selected} />}
        {view === "research" && <ResearchPanel store={store} />}
        {view === "calendar" && <CalendarView />}
        {view === "call" && <CallView store={store} />}
      </main>
      {selected && (
        <DealDrawer store={store} dealId={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}

function Topbar({
  view,
  onView,
  store,
  theme,
  onToggleTheme,
}: {
  view: View;
  onView: (v: View) => void;
  store: NonNullable<ReturnType<typeof useLocalStore>>;
  theme: string;
  onToggleTheme: () => void;
}) {
  const status = useStoreStatus(store);
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden>
          ◆
        </span>
        Rainmaker
      </div>
      <nav className="nav">
        {VIEWS.map((v) => (
          <button
            key={v.id}
            onClick={() => onView(v.id)}
            aria-current={view === v.id ? "page" : undefined}
          >
            {v.label}
          </button>
        ))}
      </nav>
      <span className="grow" />
      <SyncBadge status={status} />
      <button
        className="icon-btn"
        onClick={onToggleTheme}
        title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
        aria-label="Toggle theme"
      >
        {theme === "dark" ? "☾" : "☀"}
      </button>
    </header>
  );
}
