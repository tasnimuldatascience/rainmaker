"""Page acquisition for the research agent.

THE RULES THIS MODULE ENFORCES, and they are enforced here rather than left to callers,
because "the agent decided to" is not a defence:

  1. PUBLIC PAGES ONLY. No credentials, no cookies, no logged-in state, ever. If a page needs
     an account, it is out of scope. This is not a limitation to work around later -- an AI
     agent authenticating into third-party systems to harvest data is the thing that gets a
     product sued.
  2. robots.txt IS OBEYED. Including crawl-delay. A sales tool that ignores robots is a
     liability its customers inherit.
  3. RATE LIMITED PER HOST, not globally. One prospect's site should never see a burst.
  4. CACHED AND RESUMABLE. Research runs repeatedly on the same domains; re-fetching is rude
     and slow.
  5. BUDGETED. A hard page cap per run. An agent that decides for itself how much of someone's
     site to download is an agent that will eventually download all of it.

TWO BACKENDS behind one interface:

  FirecrawlFetcher   the hosted API. Handles JS rendering, returns clean markdown, and is the
                     right answer when a key is available.
  PlaywrightFetcher  self-hosted fallback. Slower and needs a browser install, but keeps the
                     system runnable with no third-party account.

Both are optional. `NullFetcher` lets the rest of the pipeline be tested with no network at
all, which is what the test suite uses.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import urllib.parse
import urllib.robotparser
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("rainmaker.research.fetch")

DEFAULT_UA = "RainmakerResearchBot/0.1 (+https://github.com/tasnimuldatascience/rainmaker)"
CACHE_TTL = timedelta(days=7)


@dataclass(slots=True)
class Page:
    url: str
    markdown: str
    title: str = ""
    status: int = 200
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.markdown.strip())


class FetchError(RuntimeError):
    pass


class RobotsDisallowed(FetchError):
    """The site asked us not to. Surfaced, not swallowed, so it appears in `pages_skipped`."""


class Fetcher(ABC):
    """One page in, one page out. Everything else is policy applied around it."""

    name: str = "abstract"

    @abstractmethod
    async def fetch(self, url: str) -> Page: ...

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class NullFetcher(Fetcher):
    """Serves from a fixed map. The test suite's backend; no network, fully deterministic."""

    name = "null"

    def __init__(self, pages: dict[str, str] | None = None):
        self.pages = pages or {}
        self.calls: list[str] = []

    async def fetch(self, url: str) -> Page:
        self.calls.append(url)
        body = self.pages.get(url)
        if body is None:
            return Page(url=url, markdown="", status=404)
        return Page(url=url, markdown=body, title=_first_heading(body))


class FirecrawlFetcher(Fetcher):
    """Hosted scrape API. Returns clean markdown, handles JS-rendered pages."""

    name = "firecrawl"
    ENDPOINT = "https://api.firecrawl.dev/v2/scrape"

    def __init__(self, api_key: str, timeout: float = 45.0):
        if not api_key:
            raise ValueError("FirecrawlFetcher requires an API key")
        self.api_key = api_key
        self.timeout = timeout
        self._client = None

    async def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def fetch(self, url: str) -> Page:
        client = await self._get_client()
        resp = await client.post(
            self.ENDPOINT,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
        )
        if resp.status_code >= 400:
            raise FetchError(f"firecrawl {resp.status_code} for {url}: {resp.text[:200]}")
        payload = resp.json()
        data = payload.get("data") or {}
        return Page(
            url=url,
            markdown=data.get("markdown") or "",
            title=(data.get("metadata") or {}).get("title") or "",
            status=200,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


class PlaywrightFetcher(Fetcher):
    """Self-hosted fallback. One browser, one context, a page per fetch.

    A shared browser rather than one per fetch: launching Chromium costs ~300ms and a research
    run touches a dozen pages, so per-fetch launching would dominate the run.
    """

    name = "playwright"

    def __init__(self, timeout: float = 30.0, user_agent: str = DEFAULT_UA):
        self.timeout = timeout
        self.user_agent = user_agent
        self._pw = None
        self._browser = None

    async def _ensure(self):
        if self._browser is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True)
        return self._browser

    async def fetch(self, url: str) -> Page:
        browser = await self._ensure()
        ctx = await browser.new_context(user_agent=self.user_agent)
        try:
            page = await ctx.new_page()
            resp = await page.goto(url, timeout=self.timeout * 1000, wait_until="domcontentloaded")
            title = await page.title()
            html = await page.content()
            return Page(
                url=url,
                markdown=_html_to_markdown(html),
                title=title,
                status=resp.status if resp else 0,
            )
        finally:
            await ctx.close()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()


class PolitePool:
    """Applies the policy: robots, per-host rate limit, cache, budget.

    Wraps any `Fetcher`. Separating policy from transport means the rules are tested once and
    apply identically whichever backend is in use -- rather than being re-implemented, subtly
    differently, in each.
    """

    def __init__(
        self,
        fetcher: Fetcher,
        cache_dir: Path | None = None,
        min_interval: float = 1.0,
        user_agent: str = DEFAULT_UA,
        respect_robots: bool = True,
    ):
        self.fetcher = fetcher
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.skipped: list[tuple[str, str]] = []

    # ------------------------------------------------------------------ robots
    async def _robots_for(self, host: str):
        if host in self._robots:
            return self._robots[host]
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"https://{host}/robots.txt")
        try:
            # robotparser.read() is blocking; keep it off the event loop.
            await asyncio.to_thread(parser.read)
        except Exception as exc:  # noqa: BLE001
            # A missing or unreachable robots.txt means "no restrictions stated". Treating a
            # fetch failure as a blanket deny would make the agent useless against any site
            # with a slow robots endpoint.
            log.debug("robots.txt unavailable for %s (%s); proceeding", host, exc)
            self._robots[host] = None
            return None
        self._robots[host] = parser
        return parser

    async def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        host = urllib.parse.urlparse(url).netloc
        parser = await self._robots_for(host)
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    async def crawl_delay(self, url: str) -> float:
        host = urllib.parse.urlparse(url).netloc
        parser = await self._robots_for(host)
        if parser is None:
            return self.min_interval
        declared = parser.crawl_delay(self.user_agent)
        # The site's stated delay wins when it is SLOWER than ours. Never faster.
        return max(self.min_interval, float(declared or 0))

    # ------------------------------------------------------------------ cache
    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.json"

    def _read_cache(self, url: str) -> Page | None:
        path = self._cache_path(url)
        if not path or not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(raw["fetched_at"])
        except (OSError, ValueError, KeyError):
            return None
        if datetime.now(timezone.utc) - fetched > CACHE_TTL:
            return None
        return Page(
            url=raw["url"],
            markdown=raw["markdown"],
            title=raw.get("title", ""),
            status=raw.get("status", 200),
            fetched_at=fetched,
            from_cache=True,
        )

    def _write_cache(self, page: Page) -> None:
        path = self._cache_path(page.url)
        if not path:
            return
        path.write_text(
            json.dumps(
                {
                    "url": page.url,
                    "markdown": page.markdown,
                    "title": page.title,
                    "status": page.status,
                    "fetched_at": page.fetched_at.isoformat(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------ fetch
    async def get(self, url: str) -> Page | None:
        cached = self._read_cache(url)
        if cached is not None:
            return cached

        if not await self.allowed(url):
            self.skipped.append((url, "robots.txt disallow"))
            log.info("skipping %s (robots.txt)", url)
            return None

        host = urllib.parse.urlparse(url).netloc
        lock = self._locks.setdefault(host, asyncio.Lock())
        # Serialise per host so concurrency across DIFFERENT prospects stays high while any
        # single site sees a well-spaced sequence.
        async with lock:
            delay = await self.crawl_delay(url)
            elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
            try:
                page = await self.fetcher.fetch(url)
            except Exception as exc:  # noqa: BLE001
                self.skipped.append((url, f"{type(exc).__name__}: {exc}"))
                log.warning("fetch failed for %s: %s", url, exc)
                return None
            finally:
                self._last_hit[host] = time.monotonic()

        if not page.ok:
            self.skipped.append((url, f"status {page.status}"))
            return None
        self._write_cache(page)
        return page

    async def get_many(self, urls: list[str], budget: int) -> list[Page]:
        """Fetch up to `budget` pages concurrently, respecting per-host serialisation.

        The budget is applied to the REQUEST list, not to successes: an agent that keeps
        fetching until it has N good pages has no bound on how much of a site it downloads.
        """
        if budget < len(urls):
            for dropped in urls[budget:]:
                self.skipped.append((dropped, "page budget exhausted"))
        results = await asyncio.gather(*(self.get(u) for u in urls[:budget]))
        return [p for p in results if p is not None]

    async def close(self) -> None:
        await self.fetcher.close()


# ---------------------------------------------------------------------- helpers
def _first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def _html_to_markdown(html: str) -> str:
    """Minimal HTML→text reduction for the Playwright path.

    Deliberately crude: the Firecrawl path returns proper markdown, and this fallback only
    needs to be good enough for keyword extraction. Pulling in a full HTML-to-markdown
    dependency for the degraded path is not worth the install cost.
    """
    import re

    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", html)
    html = re.sub(r"(?i)<li[^>]*>", "- ", html)
    html = re.sub(r"<[^>]+>", " ", html)
    html = (
        html.replace("&nbsp;", " ").replace("&amp;", "&")
        .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    )
    html = re.sub(r"[ \t]{2,}", " ", html)
    return re.sub(r"\n{3,}", "\n\n", html).strip()


def build_fetcher(
    firecrawl_key: str | None = None, prefer: str = "auto"
) -> Fetcher:
    """Pick a backend. `auto` prefers Firecrawl when a key exists, else Playwright."""
    if prefer == "null":
        return NullFetcher()
    if prefer in ("firecrawl", "auto") and firecrawl_key:
        log.info("research backend: firecrawl")
        return FirecrawlFetcher(firecrawl_key)
    if prefer == "firecrawl":
        raise ValueError("prefer='firecrawl' but no API key was supplied")
    log.info("research backend: playwright (no Firecrawl key)")
    return PlaywrightFetcher()
