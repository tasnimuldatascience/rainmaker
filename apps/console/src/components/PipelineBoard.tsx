/**
 * The pipeline board.
 *
 * Drag-and-drop is the HTML5 native API rather than a library. dnd-kit is ~30kb and the
 * interaction here is one drag type onto one drop target type; the native API covers that in
 * a few handlers and keeps the offline bundle small, which matters for an app whose whole
 * premise is working on a bad connection.
 *
 * The stage change on drop is a single `setField` — a local CRDT op that lands instantly and
 * syncs whenever it can. There is no optimistic-update bookkeeping and no rollback path,
 * because there is no request that can fail.
 */

import { useMemo, useState } from "react";
import type { LocalStore } from "../lib/store";
import { useDeals, type DealView } from "../lib/useStore";

const STAGES = [
  { id: "discovery", label: "Discovery" },
  { id: "qualified", label: "Qualified" },
  { id: "proposal", label: "Proposal" },
  { id: "negotiation", label: "Negotiation" },
  { id: "closed-won", label: "Closed won" },
] as const;

const TAG_TONE: Record<string, string> = {
  champion: "won",
  enterprise: "hot",
  "mid-market": "hot",
  churn: "risk",
  blocked: "risk",
};

const money = (n: number) =>
  n >= 1000 ? `$${(n / 1000).toFixed(n % 1000 === 0 ? 0 : 1)}k` : `$${n}`;

export function PipelineBoard({
  store,
  onOpen,
  selected,
}: {
  store: LocalStore;
  onOpen: (id: string) => void;
  selected: string | null;
}) {
  const deals = useDeals(store);
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);

  const byStage = useMemo(() => {
    const map = new Map<string, DealView[]>(STAGES.map((s) => [s.id, []]));
    for (const deal of deals) {
      // A deal whose stage is not one of ours still has to appear somewhere. Dropping it
      // would mean the board's total silently disagrees with the pipeline total.
      const bucket = map.get(deal.stage) ?? map.get("discovery")!;
      bucket.push(deal);
    }
    for (const list of map.values()) list.sort((a, b) => b.amount - a.amount);
    return map;
  }, [deals]);

  const total = deals.reduce((sum, d) => sum + d.amount, 0);
  const open = deals.filter((d) => !d.stage.startsWith("closed"));
  const openValue = open.reduce((sum, d) => sum + d.amount, 0);
  const hot = deals.filter((d) => d.intent >= 0.7).length;

  const drop = (stage: string) => {
    if (dragging) store.setField("deal", dragging, "stage", stage);
    setDragging(null);
    setOver(null);
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Pipeline</h1>
          <p>
            Every change here is written to this device first and synced when there is a
            connection. Drag a card to move a stage — it works with the network off.
          </p>
        </div>
      </div>

      <div className="stats">
        <Stat value={money(openValue)} label="Open pipeline" sub={`${open.length} active deals`} />
        <Stat value={money(total)} label="Total value" sub={`${deals.length} deals`} />
        <Stat value={String(hot)} label="High intent" sub="research score ≥ 0.70" />
        <Stat
          value={money(open.length ? Math.round(openValue / open.length) : 0)}
          label="Average deal"
          sub="open deals only"
        />
      </div>

      <div className="board">
        {STAGES.map((stage) => {
          const list = byStage.get(stage.id) ?? [];
          const value = list.reduce((sum, d) => sum + d.amount, 0);
          return (
            <section
              key={stage.id}
              className="col"
              data-over={over === stage.id}
              onDragOver={(e) => {
                e.preventDefault();
                setOver(stage.id);
              }}
              onDragLeave={() => setOver((o) => (o === stage.id ? null : o))}
              onDrop={() => drop(stage.id)}
            >
              <header className="col-head">
                <span className="col-name">{stage.label}</span>
                <span className="col-count">{list.length}</span>
                <span className="col-value">{money(value)}</span>
              </header>
              <div className="col-body">
                {list.map((deal) => (
                  <button
                    key={deal.id}
                    className="deal"
                    draggable
                    data-dragging={dragging === deal.id}
                    data-selected={selected === deal.id}
                    onDragStart={() => setDragging(deal.id)}
                    onDragEnd={() => {
                      setDragging(null);
                      setOver(null);
                    }}
                    onClick={() => onOpen(deal.id)}
                  >
                    <div className="deal-top">
                      <span className="deal-name">{deal.name}</span>
                      <span className="deal-amt">{money(deal.amount)}</span>
                    </div>
                    <div className="deal-meta">
                      <span>{deal.account || "—"}</span>
                      {deal.noteLength > 0 && <span>· {deal.noteLength} chars of notes</span>}
                    </div>
                    {deal.tags.length > 0 && (
                      <div className="deal-tags">
                        {deal.tags.map((tag) => (
                          <span key={tag} className="tag" data-tone={TAG_TONE[tag]}>
                            {tag}
                          </span>
                        ))}
                      </div>
                    )}
                    {deal.intent > 0 && (
                      <div
                        className="meter"
                        title={`Buying-intent score ${deal.intent.toFixed(2)}`}
                      >
                        <i style={{ inlineSize: `${Math.round(deal.intent * 100)}%` }} />
                      </div>
                    )}
                  </button>
                ))}
                {list.length === 0 && (
                  <p className="tiny muted" style={{ padding: "var(--s-2)" }}>
                    Drop a deal here
                  </p>
                )}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}

function Stat({ value, label, sub }: { value: string; label: string; sub?: string }) {
  return (
    <div className="stat">
      <div className="stat-v">{value}</div>
      <div className="stat-l">{label}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}
