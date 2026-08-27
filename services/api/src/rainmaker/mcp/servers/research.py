"""An MCP server that reads a prospect's website — and shows its work.

    python -m rainmaker.mcp.servers.research

TWO TOOLS, TWO DIFFERENT JOBS. `research_company` runs the full enrichment pipeline and returns
typed facts with provenance. `browse` opens ONE page and returns what it says **plus a picture
of it**, because on this product the browsing is not a background job — it is the demo. The
prospect watches Nadia open their pricing page and read it, which is the most direct way to show
what an agent does before a call that a human would otherwise spend twenty minutes on.

WHY THE SCREENSHOT COMES FROM HERE AND NOT THE CONSOLE. Every interesting site sets
`X-Frame-Options` or a frame-ancestors CSP, so a browser cannot render someone else's page in an
iframe. The picture has to be taken server-side by the same browser that read the text, or the
two drift: the agent narrates a paragraph that is not on the screen the prospect is looking at.

FRAMES ARE CHEAP ON PURPOSE. JPEG at quality 55 and no device-scale factor puts a full page at
roughly 60-90KB, which is a few hundred milliseconds of a voice reply. A PNG at 2x, which is what
the screenshot script uses for the README, is 1.5MB and would stall the call it is meant to
illustrate.
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from rainmaker.research import ResearchAgent, ResearchConfig, ResearchRequest, build_fetcher

DATA_DIR = os.environ.get("RAINMAKER_DATA", "data")

#: Wide enough that a marketing site lays out the way its designer intended; short enough that a
#: frame is one screenful rather than a strip.
VIEWPORT = {"width": 1280, "height": 800}

#: What a frame costs. See the module docstring — this number is the difference between the
#: stage keeping up with the voice and lagging behind it.
JPEG_QUALITY = 55

server = MCPServer(
    "rainmaker-research",
    instructions=(
        "Read a prospect's public website. `research_company` for typed facts with sources; "
        "`browse` for one page, its text, and a picture of it to show on the call."
    ),
)

_agent: ResearchAgent | None = None
_browser: Any = None
_playwright: Any = None


def _research_agent() -> ResearchAgent:
    global _agent
    if _agent is None:
        key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
        _agent = ResearchAgent(
            build_fetcher(key or None, prefer=os.environ.get("RESEARCH_BACKEND", "auto")),
            ResearchConfig(cache_dir=None),
        )
    return _agent


async def _page():
    """One browser for the process, one page per call.

    Launching Chromium costs ~300ms and a call opens half a dozen pages, so per-call launching
    would put three seconds of browser startup inside a conversation.
    """
    global _browser, _playwright
    if _browser is None:
        from playwright.async_api import async_playwright

        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
    context = await _browser.new_context(viewport=VIEWPORT)
    return context, await context.new_page()


@server.tool(
    title="Research a company",
    description=(
        "Read a company's public website and return typed facts with the page each came from: "
        "what they do, how they price, what they run, who they are hiring."
    ),
)
async def research_company(domain: str, max_pages: int = 8) -> dict[str, Any]:
    """Args:
    domain: The company's domain, e.g. "acme.dev".
    max_pages: How many pages the agent may read. It stops there whatever it has found.
    """
    enrichment = await _research_agent().research(
        ResearchRequest(domain=domain, max_pages=max_pages)
    )
    payload = enrichment.model_dump(mode="json")
    payload["score"] = enrichment.score
    # The pages it actually opened, so the caller can show them rather than assert them.
    payload["pages_fetched"] = [str(url) for url in enrichment.pages_fetched]
    return payload


@server.tool(
    title="Open a page and look at it",
    description=(
        "Open one URL, return its visible text and a JPEG of the viewport. Use `scroll_to` to "
        "bring a phrase into view before the picture is taken."
    ),
)
async def browse(
    url: str,
    scroll_to: str = "",
    screenshot: bool = True,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Args:
    url: The page to open. Must be http or https.
    scroll_to: A phrase to scroll into view first — the thing being talked about.
    screenshot: Whether to return a picture. Off when only the text is wanted.
    timeout_seconds: How long to wait for the page.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"browse needs an http(s) URL, got {url!r}")

    context, page = await _page()
    try:
        response = await page.goto(
            url, timeout=timeout_seconds * 1000, wait_until="domcontentloaded"
        )
        title = await page.title()

        scrolled = False
        if scroll_to:
            # `scroll_into_view_if_needed` on a text locator, not a CSS query: the caller knows
            # the phrase Nadia is about to say, not the site's markup.
            locator = page.get_by_text(scroll_to, exact=False).first
            try:
                await locator.scroll_into_view_if_needed(timeout=3000)
                scrolled = True
                # Let the scroll settle before the shutter, or the frame catches the page
                # mid-animation and looks broken.
                await asyncio.sleep(0.35)
            except Exception:  # noqa: BLE001 — a phrase that is not there is not an error
                pass

        text = await page.evaluate("() => document.body?.innerText ?? ''")

        # WHERE THE PAGE WOULD BE SCROLLED TO, AS A FRACTION OF ITS HEIGHT. The caller gets the
        # WHOLE page as one image and the position within it that matters, so the console can
        # scroll to that spot in front of the prospect instead of cutting to a still of the
        # destination. Watching a page get scrolled is what makes the demo read as a browser
        # being driven; a screenshot of the right part of the page reads as a screenshot.
        metrics = await page.evaluate(
            """() => ({
                scrollY: window.scrollY,
                viewport: window.innerHeight,
                total: Math.max(document.body?.scrollHeight ?? 0, window.innerHeight),
            })"""
        )
        frame = ""
        if screenshot:
            # Full page when there is somewhere to scroll to, so the client has the material to
            # animate with; the visible viewport otherwise, because a tall image of a page
            # nobody is going to move through is bytes for nothing.
            raw = await page.screenshot(
                type="jpeg", quality=JPEG_QUALITY, full_page=bool(scrolled)
            )
            frame = base64.b64encode(raw).decode()

        total = max(float(metrics.get("total") or 1), 1.0)
        viewport = max(float(metrics.get("viewport") or 1), 1.0)
        return {
            "full_page": bool(scrolled and screenshot),
            # 0..1: how far down the full-page image the interesting part starts.
            "scroll_ratio": round(min(float(metrics.get("scrollY") or 0) / total, 1.0), 4),
            # How much of that image is one screenful, so the client can size its window.
            "viewport_ratio": round(min(viewport / total, 1.0), 4),
            "url": page.url,
            "title": title,
            "status": response.status if response else 0,
            # Enough for the agent to quote from, not so much that it fills the context window.
            "text": " ".join(text.split())[:4000],
            "scrolled_to": scroll_to if scrolled else "",
            "frame_jpeg_base64": frame,
        }
    finally:
        await context.close()


@server.tool(
    title="Find the pages worth showing",
    description=(
        "Given a domain, the handful of URLs most worth putting on screen during a call — "
        "pricing, careers, docs, about — checked to exist rather than guessed at."
    ),
)
async def pages_worth_showing(domain: str) -> dict[str, Any]:
    """Args:
    domain: The company's domain.
    """
    domain = domain.strip().lower().removeprefix("www.")
    candidates = [
        ("pricing", f"https://{domain}/pricing"),
        ("careers", f"https://{domain}/careers"),
        ("customers", f"https://{domain}/customers"),
        ("docs", f"https://{domain}/docs"),
        ("about", f"https://{domain}/about"),
        ("home", f"https://{domain}/"),
    ]

    context, page = await _page()
    found: list[dict[str, str]] = []
    try:
        for label, url in candidates:
            try:
                response = await page.goto(url, timeout=8000, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001 — a 404 or a dead host is a normal answer here
                continue
            # A soft 404 renders 200 with a "not found" page, which is worse than a hard one:
            # the agent would put it on screen and narrate it.
            if response and response.status == 200:
                title = await page.title()
                if "not found" not in title.lower() and "404" not in title:
                    found.append({"label": label, "url": page.url, "title": title})
    finally:
        await context.close()
    return {"domain": domain, "pages": found, "count": len(found)}


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
