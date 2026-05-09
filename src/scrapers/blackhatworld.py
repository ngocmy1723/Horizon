"""BlackHatWorld (XenForo) scraper.

Scrapes forum index pages (e.g. https://www.blackhatworld.com/forums/black-hat-seo.9/)
and optionally each thread's first post body. The site sits behind Cloudflare,
which TLS-fingerprints httpx and returns 403, so we use ``curl_cffi`` to
impersonate a real Chrome handshake. If ``curl_cffi`` is not installed we
fall back to the shared ``httpx`` client with realistic browser headers —
this works only when CF is in low-challenge mode.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .base import BaseScraper
from ..models import (
    BlackHatWorldConfig,
    BlackHatWorldForumConfig,
    ContentItem,
    SourceType,
)

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


class BlackHatWorldScraper(BaseScraper):
    """Scraper for BlackHatWorld (XenForo) forum indexes."""

    SOURCE_TYPE: SourceType = SourceType.BLACKHATWORLD
    SOURCE_ID_PREFIX: str = "blackhatworld"
    LOG_NAME: str = "BlackHatWorld"

    def __init__(self, config: BlackHatWorldConfig, http_client: httpx.AsyncClient):
        super().__init__(config.model_dump(), http_client)
        self.bhw = config
        self.base_url = config.base_url.rstrip("/")
        self._curl_session = None
        self._curl_unavailable = False

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def fetch(self, since: datetime) -> List[ContentItem]:
        if not self.bhw.enabled:
            return []
        forums = [f for f in self.bhw.forums if f.enabled]
        if not forums:
            return []

        items: List[ContentItem] = []
        try:
            for forum in forums:
                try:
                    items.extend(await self._fetch_forum(forum, since))
                except Exception as e:
                    logger.warning(
                        "%s: error fetching forum %s: %s",
                        self.LOG_NAME,
                        forum.slug,
                        e,
                    )
        finally:
            await self._close_curl_session()
        return items

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    def _get_curl_session(self):
        """Lazily build a ``curl_cffi.requests.AsyncSession``.

        Returns None if ``curl_cffi`` is not importable.
        """
        if self._curl_session is not None or self._curl_unavailable:
            return self._curl_session
        try:
            from curl_cffi.requests import AsyncSession  # type: ignore
        except ImportError:
            logger.warning(
                "%s: curl_cffi not installed, falling back to httpx (may be CF-blocked)",
                self.LOG_NAME,
            )
            self._curl_unavailable = True
            return None
        self._curl_session = AsyncSession(impersonate=self.bhw.impersonate, timeout=30)
        return self._curl_session

    async def _close_curl_session(self) -> None:
        if self._curl_session is None:
            return
        close = getattr(self._curl_session, "close", None)
        try:
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        except Exception:
            pass
        self._curl_session = None

    async def _get(self, url: str) -> Optional[str]:
        """Fetch ``url`` and return HTML text, or None on failure."""
        session = self._get_curl_session()
        if session is not None:
            try:
                resp = await session.get(url, headers=_BROWSER_HEADERS)
                status = getattr(resp, "status_code", 0)
                if status == 403:
                    logger.warning("%s: 403 from %s (CF challenge?)", self.LOG_NAME, url)
                    return None
                if status >= 400:
                    logger.warning("%s: HTTP %d for %s", self.LOG_NAME, status, url)
                    return None
                return resp.text
            except Exception as e:
                logger.warning("%s: curl_cffi failed for %s: %s", self.LOG_NAME, url, e)
                # fall through to httpx fallback

        try:
            resp = await self.client.get(url, headers=_BROWSER_HEADERS, timeout=30.0)
        except httpx.HTTPError as e:
            logger.warning("%s: httpx failed for %s: %s", self.LOG_NAME, url, e)
            return None
        if resp.status_code == 403:
            logger.warning("%s: 403 from %s (CF challenge?)", self.LOG_NAME, url)
            return None
        if resp.status_code >= 400:
            logger.warning("%s: HTTP %d for %s", self.LOG_NAME, resp.status_code, url)
            return None
        return resp.text

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #
    async def _fetch_forum(
        self, forum: BlackHatWorldForumConfig, since: datetime
    ) -> List[ContentItem]:
        url = f"{self.base_url}/forums/{forum.slug}.{forum.id}/"
        html = await self._get(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        threads = soup.select(".structItem--thread")[: forum.fetch_limit]

        items: List[ContentItem] = []
        for el in threads:
            try:
                item = await self._parse_thread(el, forum, since)
            except Exception as e:
                logger.debug("%s: thread parse error: %s", self.LOG_NAME, e)
                continue
            if item is not None:
                items.append(item)
        return items

    async def _parse_thread(
        self,
        el,
        forum: BlackHatWorldForumConfig,
        since: datetime,
    ) -> Optional[ContentItem]:
        title_a = el.select_one(".structItem-title a:not(.labelLink)")
        if title_a is None:
            return None
        title = title_a.get_text(strip=True)
        href = title_a.get("href") or ""
        thread_url = urljoin(self.base_url + "/", href)

        time_el = el.select_one("time.u-dt") or el.select_one("time")
        published = self._parse_dt(time_el)
        if published is None:
            return None
        if published < since:
            return None

        replies = self._parse_replies(el)
        if replies < forum.min_replies:
            return None

        tid = self._parse_thread_id(el, href)
        if not tid:
            return None

        author_el = el.select_one(".structItem-cell--main .username") or el.select_one(
            ".username"
        )
        author = author_el.get_text(strip=True) if author_el else "unknown"

        content = ""
        if forum.fetch_first_post:
            if self.bhw.request_delay_sec > 0:
                await asyncio.sleep(self.bhw.request_delay_sec)
            detail_html = await self._get(thread_url)
            if detail_html:
                content = self._extract_first_post(detail_html)

        return ContentItem(
            id=self._generate_id(self.SOURCE_ID_PREFIX, forum.slug, tid),
            source_type=self.SOURCE_TYPE,
            title=title,
            url=thread_url,
            content=content,
            author=author,
            published_at=published,
            metadata={
                "feed_name": forum.name or forum.slug,
                "category": forum.category,
                "replies": replies,
                "thread_id": tid,
                "discussion_url": thread_url,
            },
        )

    @staticmethod
    def _parse_dt(time_el) -> Optional[datetime]:
        if time_el is None:
            return None
        raw = time_el.get("datetime") or time_el.get("data-time")
        if not raw:
            return None
        # XenForo emits ISO 8601 (UTC, trailing Z) or epoch seconds.
        if raw.isdigit():
            try:
                return datetime.fromtimestamp(int(raw), tz=timezone.utc)
            except (ValueError, OSError):
                return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _parse_replies(el) -> int:
        # Replies usually live in the first <dl> under the meta cell.
        dl = el.select_one(".structItem-cell--meta dl")
        dd = dl.select_one("dd") if dl else None
        if dd is None:
            return 0
        text = dd.get_text(strip=True).replace(",", "")
        # Handle "1.2K" style abbreviations.
        try:
            if text.endswith("K") or text.endswith("k"):
                return int(float(text[:-1]) * 1000)
            if text.endswith("M") or text.endswith("m"):
                return int(float(text[:-1]) * 1_000_000)
            return int(text)
        except ValueError:
            return 0

    @staticmethod
    def _parse_thread_id(el, href: str) -> str:
        key = el.get("data-content-key") or ""
        if key.startswith("thread-"):
            return key[len("thread-"):]
        # Fallback: parse trailing dotted id from the URL, e.g. /threads/foo.12345/
        for part in reversed(href.rstrip("/").split("/")):
            if not part:
                continue
            if "." in part:
                tail = part.rsplit(".", 1)[-1]
                if tail.isdigit():
                    return tail
            if part.isdigit():
                return part
        return ""

    @staticmethod
    def _extract_first_post(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        post = soup.select_one("article.message--post:first-of-type .bbWrapper")
        if post is None:
            post = soup.select_one("article.message .bbWrapper")
        if post is None:
            return ""
        text = post.get_text("\n", strip=True)
        if len(text) > 5000:
            text = text[:4997] + "..."
        return text
