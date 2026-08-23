/**
 * The research agent's output.
 *
 * THE PROVENANCE BADGE IS THE FEATURE. Enrichment products routinely present a scraped guess
 * and a model's invention with identical styling, and a rep repeats both on a call with equal
 * confidence. Here every fact is rendered with how it was obtained and the passage it came
 * from, so "their careers page says this" and "a model thinks this" never look the same.
 *
 * The excerpt is shown inline rather than behind a tooltip on purpose: a rep skimming before
 * a call will not hover, and evidence nobody reads is evidence that does not exist.
 */

import { useState } from "react";
import type { LocalStore } from "../lib/store";

type Provenance = "observed" | "derived" | "inferred";

interface Sourced<T> {
  value: T;
  provenance: Provenance;
  source_url?: string | null;
  excerpt?: string | null;
  confidence: number;
}

interface Enrichment {
  domain: string;
  name?: Sourced<string> | null;
  description?: Sourced<string> | null;
  industry?: Sourced<string> | null;
  size: Sourced<string>;
  pricing_model: Sourced<string>;
  published_price_usd?: Sourced<number> | null;
  tech: { name: string; category: string; signal: Sourced<string> }[];
  hiring: { title: string; department?: string | null; url?: string | null }[];
  signals: { kind: string; detail: Sourced<string>; weight: number }[];
  pages_fetched: string[];
  pages_skipped: [string, string][];
  duration_ms: number;
  cache_hit: boolean;
  score: number;
  field_count: Record<string, number>;
}

const PROV_TITLE: Record<Provenance, string> = {
  observed: "Read verbatim from a page we fetched",
  derived: "Computed from observed values by a documented rule",
  inferred: "A language model's reading — check the excerpt",
};

export function ResearchPanel({ store }: { store: LocalStore }) {
  const [domain, setDomain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Enrichment | null>(null);

  const run = async () => {
    const target = domain.trim();
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/research", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ domain: target, max_pages: 12 }),
      });
      if (!res.ok) throw new Error((await res.text()).slice(0, 300));
      setResult((await res.json()) as Enrichment);
    } catch (err) {
      // The research agent needs the network; the rest of the console does not. Saying so
      // explicitly beats a spinner that never resolves.
      setError(
        err instanceof Error
          ? `${err.message} — research needs a connection; your pipeline does not.`
          : String(err),
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Prospect research</h1>
          <p>
            Reads a prospect&apos;s public pages and returns typed facts, each with the URL it
            came from. Public pages only, robots.txt respected, hard page budget.
          </p>
        </div>
      </div>

      <div className="card" style={{ marginBlockEnd: "var(--s-5)" }}>
        <div className="row wrap">
          <input
            className="input"
            style={{ flex: 1, minInlineSize: 240 }}
            placeholder="prospect domain — e.g. firecrawl.dev"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void run()}
          />
          <button className="btn" data-variant="primary" onClick={() => void run()} disabled={busy}>
            {busy ? "Researching…" : "Research"}
          </button>
        </div>
        {error && (
          <div className="banner" style={{ marginBlockStart: "var(--s-3)", marginBlockEnd: 0 }}>
            <span aria-hidden>⚠</span>
            <span>{error}</span>
          </div>
        )}
      </div>

      {busy && !result && (
        <div className="col-flex">
          {[0, 1, 2].map((i) => (
            <div key={i} className="skeleton" style={{ blockSize: 108 }} />
          ))}
        </div>
      )}

      {result && <Result data={result} store={store} />}

      {!result && !busy && !error && (
        <div className="empty">
          <h3>Nothing researched yet</h3>
          <p>
            Enter a domain above. The agent reads the landing page first, then follows the
            links it finds to pricing, careers, docs and changelog — never off-domain, never
            past its page budget.
          </p>
        </div>
      )}
    </div>
  );
}

function Result({ data, store }: { data: Enrichment; store: LocalStore }) {
  const [attached, setAttached] = useState(false);
  const deals = store.replica.list("deal");

  /** Write the score onto a matching deal, if one exists for this domain. */
  const attach = () => {
    const match = deals.find(
      (d) => (store.replica.field<string>("deal", d.id, "account") ?? "") === data.domain,
    );
    if (match) {
      store.setField("deal", match.id, "intent", data.score);
      setAttached(true);
    }
  };

  return (
    <div className="col-flex" style={{ gap: "var(--s-5)" }}>
      <div className="stats">
        <Stat value={data.score.toFixed(2)} label="Buying intent" sub={`${data.signals.length} signals`} />
        <Stat value={String(data.pages_fetched.length)} label="Pages read" sub={`${data.pages_skipped.length} skipped`} />
        <Stat value={`${Math.round(data.duration_ms)}ms`} label="Duration" sub={data.cache_hit ? "from cache" : "live fetch"} />
        <Stat
          value={String(data.field_count.observed ?? 0)}
          label="Observed facts"
          sub={`${data.field_count.derived ?? 0} derived · ${data.field_count.inferred ?? 0} inferred`}
        />
      </div>

      <div className="card">
        <h3>Company</h3>
        <p className="sub">Each row shows how the value was obtained and the text it came from.</p>
        <Fact label="Name" fact={data.name} />
        <Fact label="Description" fact={data.description} />
        <Fact label="Industry" fact={data.industry} />
        <Fact label="Size" fact={data.size} />
        <Fact label="Pricing" fact={data.pricing_model} />
        <Fact label="Entry price" fact={data.published_price_usd} format={(v) => `$${v}`} />
      </div>

      {data.signals.length > 0 && (
        <div className="card">
          <h3>Buying signals</h3>
          <p className="sub">Ordered by weight. These are what the closer agent opens with.</p>
          {data.signals
            .slice()
            .sort((a, b) => b.weight - a.weight)
            .map((signal, i) => (
              <div className="fact" key={i}>
                <div className="fact-head">
                  <span className="fact-key">{signal.kind.replace(/_/g, " ")}</span>
                  <span className="prov" data-p={signal.detail.provenance} title={PROV_TITLE[signal.detail.provenance]}>
                    {signal.detail.provenance}
                  </span>
                  <span className="right tiny muted">weight {signal.weight.toFixed(2)}</span>
                </div>
                <div className="fact-val">{signal.detail.value}</div>
                {signal.detail.excerpt && <p className="fact-ex">{signal.detail.excerpt}</p>}
              </div>
            ))}
        </div>
      )}

      {data.tech.length > 0 && (
        <div className="card">
          <h3>Stack</h3>
          <p className="sub">Word-boundary matched across every page read, with the source line.</p>
          <div className="row wrap" style={{ gap: "var(--s-2)" }}>
            {data.tech.map((t) => (
              <span key={t.name} className="tag" title={`${t.category} — ${t.signal.source_url ?? ""}`}>
                {t.name}
              </span>
            ))}
          </div>
        </div>
      )}

      {data.hiring.length > 0 && (
        <div className="card">
          <h3>Open roles ({data.hiring.length})</h3>
          <p className="sub">The most reliable public growth signal.</p>
          <div className="row wrap" style={{ gap: "var(--s-2)" }}>
            {data.hiring.slice(0, 24).map((role, i) => (
              <span key={i} className="tag" data-tone={role.department === "engineering" ? "hot" : undefined}>
                {role.title}
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="card">
        <h3>Audit</h3>
        <p className="sub">Every page touched, and every page that was not.</p>
        {data.pages_fetched.map((url) => (
          <div className="fact" key={url}>
            <div className="fact-head">
              <span className="prov" data-p="observed">read</span>
              <a className="fact-src" href={url} target="_blank" rel="noreferrer">{url}</a>
            </div>
          </div>
        ))}
        {data.pages_skipped.map(([url, reason], i) => (
          <div className="fact" key={`skip-${i}`}>
            <div className="fact-head">
              <span className="prov" data-p="inferred">skipped</span>
              <span className="fact-src">{url}</span>
              <span className="right tiny muted">{reason}</span>
            </div>
          </div>
        ))}
      </div>

      <div className="row">
        <button className="btn" onClick={attach} disabled={attached}>
          {attached ? "Attached to deal ✓" : "Attach intent score to matching deal"}
        </button>
        <span className="tiny muted">
          Writes locally as a CRDT op — works offline like every other edit.
        </span>
      </div>
    </div>
  );
}

function Fact<T>({
  label,
  fact,
  format,
}: {
  label: string;
  fact?: Sourced<T> | null;
  format?: (v: T) => string;
}) {
  if (!fact) {
    return (
      <div className="fact">
        <div className="fact-head">
          <span className="fact-key">{label}</span>
        </div>
        {/* Explicit "not found" rather than a blank row. A missing fact and an empty one are
            different, and only one of them means the crawl worked. */}
        <div className="fact-val muted">not found</div>
      </div>
    );
  }
  return (
    <div className="fact">
      <div className="fact-head">
        <span className="fact-key">{label}</span>
        <span className="prov" data-p={fact.provenance} title={PROV_TITLE[fact.provenance]}>
          {fact.provenance}
        </span>
        <span className="right tiny muted">confidence {fact.confidence.toFixed(2)}</span>
      </div>
      <div className="fact-val">{format ? format(fact.value) : String(fact.value)}</div>
      {fact.excerpt && <p className="fact-ex">{fact.excerpt}</p>}
      {fact.source_url && (
        <a className="fact-src" href={fact.source_url} target="_blank" rel="noreferrer">
          {fact.source_url}
        </a>
      )}
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
