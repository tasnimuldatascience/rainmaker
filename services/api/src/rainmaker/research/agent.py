"""The research agent: decide what to read, read it, turn it into typed facts.

WHY THIS IS NOT "AN LLM WITH A BROWSER TOOL". Handing a model a fetch tool and a goal produces
a system with no bound on what it reads, no reproducibility, and no way to explain a wrong
answer. This agent instead has a FIXED PLAN over a small set of high-yield paths, a hard page
budget, and deterministic extraction. The model, when present, is used only to add labelled
INFERRED fields on top of an already-complete OBSERVED base.

The consequence that matters: the same domain researched twice produces the same enrichment,
and every field can be traced to a URL. That is what makes it usable in a sales conversation,
where being confidently wrong costs the deal.

THE PLAN. Sales-relevant information clusters in a handful of predictable places, and they are
tried in descending order of yield:

    /                landing page  -> name, positioning, category
    /pricing         the routing decision: self-serve vs. sales-assisted
    /careers, /jobs  the strongest public growth and intent signal
    /about, /company headcount, founding, industry
    /docs, /blog     stack, launch cadence
    /changelog       shipping velocity

Discovered links are used only to REFINE those guesses (a site whose careers page lives at
/company/careers), never to wander. The budget is enforced regardless.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .extract import (
    dominant_industry,
    extract_description,
    extract_hiring,
    extract_name,
    extract_pricing,
    extract_signals,
    extract_size,
    extract_tech,
)
from .fetch import Fetcher, Page, PolitePool
from .schema import Enrichment, Provenance, ResearchRequest, Sourced

log = logging.getLogger("rainmaker.research.agent")

# Ordered by expected value per fetch. The first entry is always the landing page.
PLAN: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("home", ("/",)),
    ("pricing", ("/pricing", "/plans", "/pricing/")),
    ("careers", ("/careers", "/jobs", "/company/careers", "/about/careers", "/join")),
    ("about", ("/about", "/company", "/about-us")),
    ("docs", ("/docs", "/documentation", "/developers")),
    ("changelog", ("/changelog", "/releases", "/whats-new")),
    ("blog", ("/blog", "/news")),
)


@dataclass(slots=True)
class ResearchConfig:
    """Everything tunable, in one place, so a run is describable by its config."""

    cache_dir: Path | None = None
    min_interval: float = 1.0
    respect_robots: bool = True
    # Keywords describing what WE sell. A prospect's job post mentioning these is the highest
    # weight signal available, and it is necessarily deployment-specific.
    our_category: tuple[str, ...] = field(default_factory=tuple)
    llm: "InferenceProvider | None" = None


class InferenceProvider:
    """Optional model layer. Adds INFERRED fields; never replaces OBSERVED ones.

    Deliberately a narrow interface rather than a general agent loop. The model is asked one
    bounded question about text we already have; it cannot fetch, cannot loop, and cannot
    overwrite a fact that was read off a page.
    """

    async def summarise(self, domain: str, pages: list[Page]) -> Sourced[str] | None:
        raise NotImplementedError

    async def classify_industry(self, domain: str, pages: list[Page]) -> Sourced[str] | None:
        raise NotImplementedError


class ResearchAgent:
    def __init__(self, fetcher: Fetcher, config: ResearchConfig | None = None):
        self.config = config or ResearchConfig()
        self.pool = PolitePool(
            fetcher,
            cache_dir=self.config.cache_dir,
            min_interval=self.config.min_interval,
            respect_robots=self.config.respect_robots,
        )

    async def research(self, request: ResearchRequest) -> Enrichment:
        started = time.perf_counter()
        domain = request.domain
        base = f"https://{domain}"

        # The pool is long-lived on purpose (it owns the browser and the per-host rate-limit
        # state), so per-RUN state has to be scoped explicitly. Without this the skip list
        # accumulates across calls and a second run of the same domain reports every skip
        # twice -- which also made the agent non-deterministic across repeated invocations.
        self.pool.skipped.clear()

        # Stage 1: the landing page, alone. It is the only page guaranteed to exist, and its
        # links tell us where everything else actually lives.
        home = await self.pool.get(f"{base}/")
        pages: list[Page] = [home] if home else []
        remaining = request.max_pages - (1 if home else 0)

        # Stage 2: the plan, refined by what the landing page linked to.
        candidates = self._candidate_urls(base, home)
        if remaining > 0 and candidates:
            pages.extend(await self.pool.get_many(candidates, budget=remaining))

        enrichment = self._assemble(domain, pages)
        if self.config.llm is not None and pages:
            await self._augment(domain, pages, enrichment)

        enrichment.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        enrichment.pages_skipped = list(self.pool.skipped)
        enrichment.cache_hit = bool(pages) and all(p.from_cache for p in pages)
        log.info(
            "researched %s: %d pages, %d skipped, score=%.2f, %.0fms",
            domain, len(pages), len(enrichment.pages_skipped),
            enrichment.score, enrichment.duration_ms,
        )
        return enrichment

    # ------------------------------------------------------------------ planning
    def _candidate_urls(self, base: str, home: Page | None) -> list[str]:
        """The plan, with each slot resolved to a real link when the home page offers one.

        Using the discovered link rather than the guess matters: /careers 404s on a large
        fraction of sites that do have a careers page somewhere else, and a 404 costs the same
        budget as a hit.
        """
        links = _links(home.markdown, base) if home else []
        chosen: list[str] = []
        seen: set[str] = {f"{base}/"}

        for _slot, paths in PLAN[1:]:
            match = next(
                (
                    link
                    for link in links
                    if any(_path_of(link).rstrip("/").endswith(p.rstrip("/")) for p in paths)
                ),
                None,
            )
            url = match or f"{base}{paths[0]}"
            if url not in seen:
                seen.add(url)
                chosen.append(url)
        return chosen

    # ------------------------------------------------------------------ assembly
    def _assemble(self, domain: str, pages: list[Page]) -> Enrichment:
        enrichment = Enrichment(domain=domain)
        enrichment.pages_fetched = [p.url for p in pages]
        if not pages:
            return enrichment

        home = pages[0]
        enrichment.name = extract_name(home)
        enrichment.description = extract_description(home)

        pricing_page = _best(pages, ("pricing", "plans")) or home
        model, entry = extract_pricing(pricing_page)
        enrichment.pricing_model = model
        enrichment.published_price_usd = entry

        enrichment.tech = extract_tech(pages)
        enrichment.hiring = extract_hiring(pages)
        enrichment.size = extract_size(pages, enrichment.hiring)
        enrichment.signals = extract_signals(
            pages, enrichment.hiring, self.config.our_category
        )

        if industry := dominant_industry(enrichment.tech):
            enrichment.industry = Sourced[str](
                value=industry,
                provenance=Provenance.DERIVED,
                source_url=home.url,
                excerpt=f"derived from stack: {', '.join(t.name for t in enrichment.tech[:6])}",
                confidence=0.4,
            )
        return enrichment

    async def _augment(self, domain: str, pages: list[Page], enrichment: Enrichment) -> None:
        """Model-derived fields, applied only where the deterministic pass found nothing.

        Never overwrites. If the crawl already read the company's own description off their
        landing page, a model paraphrase of it is strictly worse — less accurate, less
        citable, and more expensive.
        """
        assert self.config.llm is not None
        try:
            if enrichment.description is None:
                enrichment.description = await self.config.llm.summarise(domain, pages)
            if enrichment.industry is None:
                enrichment.industry = await self.config.llm.classify_industry(domain, pages)
        except Exception as exc:  # noqa: BLE001
            # The model is an optional enhancement. A failure here degrades the enrichment;
            # it must never fail the run, because the OBSERVED fields are already useful.
            log.warning("inference layer failed for %s: %s", domain, exc)

    async def research_many(
        self, domains: list[str], concurrency: int = 4, max_pages: int = 12
    ) -> list[Enrichment]:
        """Batch enrichment. Concurrency is across DOMAINS; `PolitePool` still serialises
        per host, so no single site sees parallel requests."""
        sem = asyncio.Semaphore(concurrency)

        async def one(domain: str) -> Enrichment:
            async with sem:
                return await self.research(
                    ResearchRequest(domain=domain, max_pages=max_pages)
                )

        return list(await asyncio.gather(*(one(d) for d in domains)))

    async def close(self) -> None:
        await self.pool.close()


# ---------------------------------------------------------------------- helpers
def _best(pages: list[Page], needles: tuple[str, ...]) -> Page | None:
    for page in pages:
        low = page.url.lower()
        if any(n in low for n in needles):
            return page
    return None


def _path_of(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).path or "/"


def _links(markdown: str, base: str) -> list[str]:
    """Same-origin links from a markdown document, in document order.

    Same-origin only. Following off-site links is how a research agent turns into a crawler:
    one link to a partner site and the budget is spent somewhere entirely unrelated to the
    prospect.
    """
    import re
    from urllib.parse import urljoin, urlparse

    host = urlparse(base).netloc
    out: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\[[^\]]*\]\(([^)\s]+)", markdown):
        raw = match.group(1)
        if raw.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        url = urljoin(base + "/", raw)
        parsed = urlparse(url)
        if parsed.netloc != host or parsed.scheme not in ("http", "https"):
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out
