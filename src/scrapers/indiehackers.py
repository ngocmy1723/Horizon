"""IndieHackers scraper.

Scrapes the public Indie Hackers homepage (https://www.indiehackers.com/),
which is server-rendered and includes ~48 latest/featured posts with title,
author, upvote count and comment count. Optionally fetches each post's
detail page to extract the full body and published date.

No API key required. There is no public Indie Hackers JSON API; the SPA
loads from Firebase RTDB which requires auth, so we parse the SSR HTML.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .base import BaseScraper
from ..models import ContentItem, IndieHackersConfig, SourceType

logger = logging.getLogger(__name__)


_STORY_RE = re.compile(
    r'class="(?P<cls>story homepage-post[^"]*)"(?P<body>.*?)(?=class="story homepage-post|<footer|</main>)',
    re.S,
)
_POST_LINK_RE = re.compile(r'href="(/post/[a-zA-Z0-9_-]+)"')
_TITLE_RE = re.compile(r'class="story__title"[^>]*>\s*(.*?)\s*</h3>', re.S)
_AUTHOR_RE = re.compile(
    r'class="user-link__name[^"]*">\s*(.*?)\s*</span>', re.S
)
_LIKES_RE = re.compile(
    r'story__count--likes.*?story__count-number">\s*(\d+)', re.S
)
_COMMENTS_RE = re.compile(
    r'story__count--comments.*?story__count-number">\s*(\d+)', re.S
)


_DATE_RE = re.compile(
    r'post-page__date[^>]*>.*?<span>([^<]+)</span>', re.S
)
_BODY_RE = re.compile(
    r'class="post-page__body[^"]*"[^>]*>(.*?)</div>\s*(?:<!--|<div class="post-page__|<footer)',
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class IndieHackersScraper(BaseScraper):
    """Scraper for indiehackers.com homepage posts."""

    def __init__(self, config: IndieHackersConfig, http_client: httpx.AsyncClient):
        super().__init__(config.model_dump(), http_client)
        self.ih_config = config
        self.base_url = config.base_url.rstrip("/")

    async def fetch(self, since: datetime) -> List[ContentItem]:
        if not self.ih_config.enabled:
            return []

        try:
            resp = await self.client.get(self.base_url + "/", timeout=30.0)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("IndieHackers homepage fetch failed: %s", e)
            return []

        stories = self._parse_homepage(resp.text)
        if not stories:
            logger.info("IndieHackers: no stories parsed from homepage")
            return []

        # Apply min_upvotes + limit before any per-post network calls.
        filtered = [
            s for s in stories
            if s.get("upvotes", 0) >= self.ih_config.min_upvotes
        ]
        filtered = filtered[: self.ih_config.fetch_limit]

        # Optionally enrich each post with body + published date.
        if self.ih_config.fetch_post_body:
            details = await asyncio.gather(
                *[self._fetch_post(s["path"]) for s in filtered],
                return_exceptions=True,
            )
        else:
            details = [None] * len(filtered)

        items: List[ContentItem] = []
        for story, detail in zip(filtered, details):
            if isinstance(detail, Exception):
                detail = None
            item = self._build_item(story, detail or {})
            if item is None:
                continue
            if item.published_at < since:
                continue
            items.append(item)

        return items

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #
    def _parse_homepage(self, html: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for m in _STORY_RE.finditer(html):
            block = m.group("body")
            cls = m.group("cls")
            link = _POST_LINK_RE.search(block)
            if not link:
                continue
            path = link.group(1)
            if path in seen:
                continue
            seen.add(path)
            title_m = _TITLE_RE.search(block)
            author_m = _AUTHOR_RE.search(block)
            likes_m = _LIKES_RE.search(block)
            comments_m = _COMMENTS_RE.search(block)
            out.append({
                "path": path,
                "url": self.base_url + path,
                "title": _clean_text(title_m.group(1)) if title_m else path,
                "author": _clean_text(author_m.group(1)) if author_m else None,
                "upvotes": int(likes_m.group(1)) if likes_m else 0,
                "comments": int(comments_m.group(1)) if comments_m else 0,
                "featured": "story--featured" in cls,
            })
        return out

    async def _fetch_post(self, path: str) -> Dict[str, Any]:
        try:
            resp = await self.client.get(self.base_url + path, timeout=30.0)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.debug("IndieHackers post fetch %s failed: %s", path, e)
            return {}

        html = resp.text
        body = ""
        body_m = _BODY_RE.search(html)
        if body_m:
            body = _clean_text(body_m.group(1))
            if len(body) > 8000:
                body = body[:7997] + "..."

        published: Optional[datetime] = None
        date_m = _DATE_RE.search(html)
        if date_m:
            published = _parse_long_date(date_m.group(1))

        return {"body": body, "published_at": published}

    # ------------------------------------------------------------------ #
    # Conversion
    # ------------------------------------------------------------------ #
    def _build_item(
        self, story: Dict[str, Any], detail: Dict[str, Any]
    ) -> Optional[ContentItem]:
        path = story["path"]
        native_id = path.rsplit("-", 1)[-1] or path.split("/")[-1]
        published = detail.get("published_at") or datetime.now(timezone.utc)
        content = detail.get("body") or story["title"]

        return ContentItem(
            id=self._generate_id("indiehackers", "post", native_id),
            source_type=SourceType.INDIEHACKERS,
            title=story["title"],
            url=story["url"],
            content=content,
            author=story.get("author"),
            published_at=published,
            metadata={
                "upvotes": story.get("upvotes", 0),
                "comment_count": story.get("comments", 0),
                "featured": story.get("featured", False),
                "discussion_url": story["url"],
            },
        )


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _clean_text(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = unescape(text)
    return _WS_RE.sub(" ", text).strip()


_MONTHS = {
    m: i for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"],
        start=1,
    )
}


def _parse_long_date(raw: str) -> Optional[datetime]:
    """Parse strings like 'May 6, 2026' to a UTC datetime at midnight."""
    s = raw.strip().rstrip(".")
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", s)
    if not m:
        return None
    month = _MONTHS.get(m.group(1))
    if not month:
        return None
    try:
        return datetime(int(m.group(3)), month, int(m.group(2)), tzinfo=timezone.utc)
    except ValueError:
        return None
