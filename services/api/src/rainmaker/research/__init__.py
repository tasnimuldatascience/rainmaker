"""Prospect research: fetch public pages, extract typed facts with provenance."""

from .agent import ResearchAgent, ResearchConfig
from .fetch import Fetcher, NullFetcher, Page, PolitePool, build_fetcher
from .schema import CompanySize, Enrichment, PricingModel, Provenance, ResearchRequest, Sourced

__all__ = [
    "ResearchAgent", "ResearchConfig",
    "Fetcher", "NullFetcher", "Page", "PolitePool", "build_fetcher",
    "CompanySize", "Enrichment", "PricingModel", "Provenance", "ResearchRequest", "Sourced",
]
