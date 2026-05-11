"""RSS feed scraper implementation."""

import asyncio
import calendar
import logging
import os
import re
from datetime import datetime, timezone
from typing import List
from email.utils import parsedate_to_datetime
import httpx
import feedparser

from .base import BaseScraper
from ..models import ContentItem, SourceType, RSSSourceConfig

logger = logging.getLogger(__name__)

# Many RSS feeds (Cloudflare, WAFs, Substack, etc.) reject the default
# "python-httpx/x.y.z" User-Agent with 403 Forbidden, especially from
# data-center IPs (AWS / DigitalOcean). We rotate a realistic desktop
# browser UA per request via `fake-useragent` to avoid that and to look
# less like a single repeating client.
try:
    from fake_useragent import UserAgent, FakeUserAgentError  # type: ignore

    _UA_GENERATOR: "UserAgent | None" = UserAgent(
        browsers=["Chrome", "Edge", "Firefox", "Safari"],
        os=["Windows", "Mac OS X", "Linux"],
        platforms=["desktop"],
        min_version=115.0,
    )
except Exception as _ua_init_err:  # pragma: no cover - defensive import
    logger.warning(
        "fake-useragent unavailable (%s); falling back to a static User-Agent",
        _ua_init_err,
    )
    _UA_GENERATOR = None

# Fallback UA used only when fake-useragent fails at import time or its
# data source cannot be reached at runtime.
_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Static, non-UA headers that every RSS request should carry.
_BASE_RSS_HEADERS = {
    "Accept": (
        "application/rss+xml, application/atom+xml, "
        "application/xml;q=0.9, text/xml;q=0.9, "
        "application/json;q=0.8, text/html;q=0.7, */*;q=0.5"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def _random_user_agent() -> str:
    """Return a fresh random desktop-browser User-Agent string."""
    if _UA_GENERATOR is not None:
        try:
            return _UA_GENERATOR.random
        except Exception as e:  # FakeUserAgentError or any runtime failure
            logger.debug("fake-useragent random() failed (%s); using fallback", e)
    return _FALLBACK_USER_AGENT


def build_rss_headers() -> dict:
    """Build the header set for a single RSS request with a random UA."""
    return {"User-Agent": _random_user_agent(), **_BASE_RSS_HEADERS}


# HTTP statuses where a retry is worth attempting.
_RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504, 522, 524}
_MAX_ATTEMPTS = 3
_BASE_BACKOFF = 1.5  # seconds


class RSSScraper(BaseScraper):
    """Scraper for RSS/Atom feeds."""

    def __init__(self, sources: List[RSSSourceConfig], http_client: httpx.AsyncClient):
        """Initialize RSS scraper.

        Args:
            sources: List of RSS feed configurations
            http_client: Shared async HTTP client
        """
        super().__init__({"sources": sources}, http_client)

    async def fetch(self, since: datetime) -> List[ContentItem]:
        """Fetch RSS feed items.

        Args:
            since: Only fetch items published after this time

        Returns:
            List[ContentItem]: Fetched content items
        """
        items = []
        sources = self.config["sources"]

        for source in sources:
            if not source.enabled:
                continue

            feed_items = await self._fetch_feed(source, since)
            items.extend(feed_items)

        return items

    async def _fetch_feed(
        self,
        source: RSSSourceConfig,
        since: datetime
    ) -> List[ContentItem]:
        """Fetch items from a single RSS feed.

        Args:
            source: RSS feed configuration
            since: Only fetch items after this time

        Returns:
            List[ContentItem]: Feed content items
        """
        items = []

        try:
            # Expand environment variables in URL (e.g. ${LWN_KEY})
            feed_url = re.sub(
                r'\$\{(\w+)\}',
                lambda m: os.environ.get(m.group(1), m.group(0)).strip(),
                str(source.url),
            )

            # Warn if the URL still contains an unresolved ${VAR} placeholder.
            unresolved = re.search(r'\$\{(\w+)\}', feed_url)
            if unresolved:
                logger.warning(
                    "Skipping RSS feed %s: environment variable %s is not set (url=%s)",
                    source.name,
                    unresolved.group(1),
                    feed_url,
                )
                return items

            response = await self._fetch_with_retry(source.name, feed_url)
            if response is None:
                return items

            # Parse feed
            feed = feedparser.parse(response.text)

            for entry in feed.entries:
                # Parse published date
                published_at = self._parse_date(entry)
                if not published_at or published_at < since:
                    continue

                # Generate unique ID from feed URL and entry ID
                feed_id = str(source.url).split("//")[1].replace("/", "_")
                entry_id = entry.get("id", entry.get("link", ""))
                unique_id = f"{feed_id}:{hash(entry_id)}"

                # Extract content
                content = self._extract_content(entry)

                item = ContentItem(
                    id=self._generate_id("rss", feed_id, str(hash(entry_id))),
                    source_type=SourceType.RSS,
                    title=entry.get("title", "Untitled"),
                    url=entry.get("link", str(source.url)),
                    content=content,
                    author=entry.get("author", source.name),
                    published_at=published_at,
                    metadata={
                        "feed_name": source.name,
                        "category": source.category,
                        "tags": [tag.term for tag in entry.get("tags", [])],
                    }
                )
                items.append(item)

        except Exception as e:
            logger.warning("Error parsing RSS feed %s: %s", source.name, e)

        return items

    async def _fetch_with_retry(
        self,
        source_name: str,
        feed_url: str,
    ) -> httpx.Response | None:
        """GET a feed URL with browser-like headers and retry on transient errors.

        Returns None if all attempts fail (the error is already logged).
        """
        last_error: str | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self.client.get(
                    feed_url,
                    headers=build_rss_headers(),
                    follow_redirects=True,
                    timeout=30.0,
                )
            except httpx.HTTPError as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(_BASE_BACKOFF * attempt)
                    continue
                logger.warning(
                    "Error fetching RSS feed %s (%s) after %d attempts: %s",
                    source_name, feed_url, attempt, last_error,
                )
                return None

            status = response.status_code
            if status < 400:
                return response

            # Retry on transient statuses.
            if status in _RETRYABLE_STATUSES and attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_BASE_BACKOFF * attempt)
                continue

            # Give up: log a concise, actionable message.
            final_url = str(response.url)
            logger.warning(
                "Error fetching RSS feed %s: HTTP %d for %s%s",
                source_name,
                status,
                final_url,
                "" if final_url == feed_url else f" (redirected from {feed_url})",
            )
            return None

        return None

    def _parse_date(self, entry: dict) -> datetime:
        """Parse publication date from feed entry.

        Args:
            entry: Feed entry data

        Returns:
            datetime: Parsed publication date or None
        """
        # Try different date fields
        for field in ["published", "updated", "created"]:
            if field in entry:
                try:
                    # Try parsing structured time first
                    if f"{field}_parsed" in entry and entry[f"{field}_parsed"]:
                        return datetime.fromtimestamp(
                            calendar.timegm(entry[f"{field}_parsed"]),
                            tz=timezone.utc
                        )
                    # Fallback to string parsing
                    date_str = entry[field]
                    return parsedate_to_datetime(date_str)
                except Exception:
                    continue

        return None

    def _extract_content(self, entry: dict) -> str:
        """Extract text content from feed entry.

        Args:
            entry: Feed entry data

        Returns:
            str: Extracted text content
        """
        # Try different content fields
        if "summary" in entry:
            return entry.summary
        elif "description" in entry:
            return entry.description
        elif "content" in entry and entry.content:
            # content is usually a list
            return entry.content[0].get("value", "")

        return ""
