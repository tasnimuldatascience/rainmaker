/**
 * Demo data, written through the normal op path.
 *
 * Seeded via `store.setField` rather than injected into the replica directly, so the demo
 * rows are indistinguishable from real ones: they persist, they sync, they merge. A seed that
 * bypassed the op log would produce a board that looks right and vanishes on reload, which is
 * a worse first impression than an empty board.
 */

import type { LocalStore } from "./store";

const DEALS = [
  { id: "d-northwind", name: "Northwind Analytics", account: "northwind.io", stage: "proposal",
    amount: 48000, owner: "you", tags: ["inbound", "mid-market"], intent: 0.82 },
  { id: "d-helios",    name: "Helios Robotics",     account: "helios.dev",   stage: "discovery",
    amount: 12500, owner: "you", tags: ["inbound"], intent: 0.41 },
  { id: "d-corvus",    name: "Corvus Data",         account: "corvusdata.com", stage: "negotiation",
    amount: 96000, owner: "you", tags: ["enterprise", "champion"], intent: 0.91 },
  { id: "d-lumen",     name: "Lumen Health",        account: "lumenhealth.co", stage: "qualified",
    amount: 22000, owner: "you", tags: ["outbound"], intent: 0.55 },
  { id: "d-arbor",     name: "Arbor Systems",       account: "arborsys.net", stage: "closed-won",
    amount: 31000, owner: "you", tags: ["self-serve"], intent: 1 },
  { id: "d-pike",      name: "Pike Logistics",      account: "pikelogistics.com", stage: "discovery",
    amount: 8000, owner: "you", tags: [], intent: 0.22 },
];

const NOTES: Record<string, string> = {
  "d-corvus":
    "Champion is the VP Eng. Blocker is a security review — they asked for SOC 2 evidence " +
    "and a data-residency answer before signing.",
  "d-northwind":
    "Evaluating us against two others. Price-sensitive at the 50-seat tier.",
};

export async function seedIfEmpty(store: LocalStore): Promise<void> {
  if (store.replica.list("deal").length > 0) return;
  for (const deal of DEALS) {
    store.setField("deal", deal.id, "name", deal.name);
    store.setField("deal", deal.id, "account", deal.account);
    store.setField("deal", deal.id, "stage", deal.stage);
    store.setField("deal", deal.id, "amount", deal.amount);
    store.setField("deal", deal.id, "owner", deal.owner);
    store.setField("deal", deal.id, "intent", deal.intent);
    for (const tag of deal.tags) store.addTag("deal", deal.id, tag);
  }
  for (const [id, text] of Object.entries(NOTES)) {
    store.editText("deal", id, "notes", text);
  }
}
