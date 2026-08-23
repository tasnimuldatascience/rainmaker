/**
 * Deal detail.
 *
 * The notes field is a live CRDT text sequence, not a form input that overwrites on save.
 * Typing emits a minimal insert/delete diff (see `store.editText`), so two people editing the
 * same note concurrently both keep their words. Replacing the whole field on each keystroke —
 * the obvious implementation — would delete and re-insert every character and destroy the
 * other person's edits on every merge.
 *
 * There is no save button, and that is the point: there is nothing to save. The write already
 * happened, locally, before this component re-rendered.
 */

import { useEffect } from "react";
import type { LocalStore } from "../lib/store";
import { useDeal, useText } from "../lib/useStore";

const STAGES = ["discovery", "qualified", "proposal", "negotiation", "closed-won", "closed-lost"];

export function DealDrawer({
  store,
  dealId,
  onClose,
}: {
  store: LocalStore;
  dealId: string;
  onClose: () => void;
}) {
  const deal = useDeal(store, dealId);
  const notes = useText(store, "deal", dealId, "notes");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!deal) return null;

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label={`Deal: ${deal.name}`}>
        <header className="drawer-head">
          <div>
            <h2 style={{ fontSize: "var(--t-lg)", fontWeight: 620, letterSpacing: "-0.02em" }}>
              {deal.name}
            </h2>
            <p className="tiny muted">{deal.account}</p>
          </div>
          <button className="icon-btn right" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <div className="drawer-body">
          <div className="row wrap" style={{ gap: "var(--s-4)" }}>
            <div className="field" style={{ flex: 1, minInlineSize: 150 }}>
              <label htmlFor="stage">Stage</label>
              <select
                id="stage"
                className="select"
                value={deal.stage}
                onChange={(e) => store.setField("deal", dealId, "stage", e.target.value)}
              >
                {STAGES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ flex: 1, minInlineSize: 150 }}>
              <label htmlFor="amount">Amount (USD)</label>
              <input
                id="amount"
                className="input"
                type="number"
                value={deal.amount}
                onChange={(e) =>
                  store.setField("deal", dealId, "amount", Number(e.target.value) || 0)
                }
              />
            </div>
          </div>

          <div className="field">
            <label>Tags</label>
            <div className="row wrap" style={{ gap: "var(--s-2)" }}>
              {deal.tags.map((tag) => (
                <button
                  key={tag}
                  className="tag"
                  title="Remove tag"
                  onClick={() => store.removeTag("deal", dealId, tag)}
                >
                  {tag} ✕
                </button>
              ))}
              <input
                className="input"
                style={{ inlineSize: 150 }}
                placeholder="add tag…"
                onKeyDown={(e) => {
                  const value = e.currentTarget.value.trim();
                  if (e.key === "Enter" && value) {
                    store.addTag("deal", dealId, value);
                    e.currentTarget.value = "";
                  }
                }}
              />
            </div>
          </div>

          <div className="field">
            <label htmlFor="notes">
              Notes <span className="muted" style={{ textTransform: "none" }}>· collaborative, no save button</span>
            </label>
            <textarea
              id="notes"
              className="textarea"
              value={notes}
              placeholder="Everything typed here merges character by character with anyone else editing."
              onChange={(e) => store.editText("deal", dealId, "notes", e.target.value)}
            />
            <p className="tiny muted">
              {notes.length} characters · edits from other reps merge without overwriting yours
            </p>
          </div>
        </div>
      </aside>
    </>
  );
}
