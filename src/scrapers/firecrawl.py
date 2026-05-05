"""Firecrawl scraper.

Wraps the Firecrawl API (https://www.firecrawl.dev/) to fetch page content
as Markdown. Two modes:

  * ``scrape`` — single page via ``POST /v1/scrape`` (synchronous).
  * ``crawl``  — multi-page via ``POST /v1/crawl`` (async; we poll
                  ``GET /v1/crawl/{id}`` until completion or timeout).

Firecrawl's own LLM/extract/summary endpoints are intentionally NOT used;
the rest of the Horizon pipeline does the AI scoring/summarization. This
scraper only collects raw markdown + metadata.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import httpx

from .base import BaseScraper
from ..models import ContentItem, FirecrawlConfig, FirecrawlSourceConfig, SourceType

logger = logging.getLogger(__name__)


class FirecrawlScraper(BaseScraper):
    """Scraper for the Firecrawl API."""

    def __init__(self, config: FirecrawlConfig, http_client: httpx.AsyncClient):
        super().__init__({"config": config}, http_client)
        self.fc_config = config
        self.api_key = os.environ.get(config.api_key_env, "").strip()
        self.base_url = config.base_url.rstrip("/")

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #
    async def fetch(self, since: datetime) -> List[ContentItem]:
        if not self.fc_config.enabled or not self.fc_config.sources:
            return []
        if not self.api_key:
            logger.warning(
                "Firecrawl enabled but %s is empty; skipping",
                self.fc_config.api_key_env,
            )
            return []

        items: List[ContentItem] = []
        for source in self.fc_config.sources:
            if not source.enabled:
                continue
            try:
                if source.mode == "crawl":
                    items.extend(await self._fetch_crawl(source, since))
                else:
                    items.extend(await self._fetch_scrape(source, since))
            except Exception as e:
                logger.warning("Firecrawl source %s failed: %s", source.name, e)
        return items

    # ------------------------------------------------------------------ #
    # HTTP helpers
    # ------------------------------------------------------------------ #
    @property
    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self.client.post(
            f"{self.base_url}{path}",
            headers=self._auth_headers,
            json=payload,
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def _get(self, path: str) -> Dict[str, Any]:
        resp = await self.client.get(
            f"{self.base_url}{path}",
            headers=self._auth_headers,
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # Modes
    # ------------------------------------------------------------------ #
    async def _fetch_scrape(
        self, source: FirecrawlSourceConfig, since: datetime
    ) -> List[ContentItem]:
        payload = {"url": str(source.url), "formats": ["markdown"]}
        data = await self._post("/v1/scrape", payload)
        page = data.get("data") or {}
        item = self._page_to_item(source, page)
        if item is None:
            return []
        if item.published_at < since:
            return []
        return [item]

    async def _fetch_crawl(
        self, source: FirecrawlSourceConfig, since: datetime
    ) -> List[ContentItem]:
        payload: Dict[str, Any] = {
            "url": str(source.url),
            "limit": source.limit,
            "scrapeOptions": {"formats": ["markdown"]},
        }
        if source.max_depth is not None:
            payload["maxDepth"] = source.max_depth
        if source.include_paths:
            payload["includePaths"] = source.include_paths
        if source.exclude_paths:
            payload["excludePaths"] = source.exclude_paths

        start = await self._post("/v1/crawl", payload)
        job_id = start.get("id")
        if not job_id:
            logger.warning("Firecrawl crawl returned no job id for %s", source.name)
            return []

        deadline = asyncio.get_event_loop().time() + source.poll_timeout_sec
        pages: List[Dict[str, Any]] = []
        status = "scraping"
        while asyncio.get_event_loop().time() < deadline:
            result = await self._get(f"/v1/crawl/{job_id}")
            status = result.get("status", "scraping")
            pages = result.get("data") or []
            if status in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(source.poll_interval_sec)

        if status != "completed":
            logger.warning(
                "Firecrawl crawl %s ended with status=%s after timeout (got %d pages)",
                source.name, status, len(pages),
            )

        items: List[ContentItem] = []
        for page in pages:
            item = self._page_to_item(source, page)
            if item is None:
                continue
            if item.published_at < since:
                continue
            items.append(item)
        return items

    # ------------------------------------------------------------------ #
    # Conversion
    # ------------------------------------------------------------------ #
    def _page_to_item(
        self, source: FirecrawlSourceConfig, page: Dict[str, Any]
    ) -> Optional[ContentItem]:
        if not page:
            return None
        markdown: str = page.get("markdown") or ""
        meta: Dict[str, Any] = page.get("metadata") or {}
        page_url = (
            meta.get("sourceURL")
            or meta.get("url")
            or meta.get("ogUrl")
            or str(source.url)
        )
        title = meta.get("title") or meta.get("ogTitle") or page_url
        author = meta.get("author") or meta.get("ogSiteName")

        published_at = self._parse_published(meta) or datetime.now(timezone.utc)

        native_id = hashlib.sha1(page_url.encode("utf-8")).hexdigest()[:16]

        return ContentItem(
            id=self._generate_id("firecrawl", source.name, native_id),
            source_type=SourceType.FIRECRAWL,
            title=title,
            url=page_url,
            content=markdown,
            author=author,
            published_at=published_at,
            metadata={
                "feed_name": source.name,
                "category": source.category,
                "mode": source.mode,
                "description": meta.get("description") or meta.get("ogDescription"),
                "language": meta.get("language"),
            },
        )

    @staticmethod
    def _parse_published(meta: Dict[str, Any]) -> Optional[datetime]:
        for key in ("publishedTime", "published_time", "article:published_time", "datePublished", "modifiedTime"):
            raw = meta.get(key)
            if not raw:
                continue
            try:
                # Try ISO 8601 first
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                pass
            try:
                return parsedate_to_datetime(str(raw))
            except (TypeError, ValueError):
                continue
        return None
