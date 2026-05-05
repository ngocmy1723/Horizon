"""linux.do (Discourse) scraper implementation."""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, List, Optional

import httpx

from .base import BaseScraper
from ..models import ContentItem, LinuxDoConfig, LinuxDoFeedConfig, SourceType

logger = logging.getLogger(__name__)

MAX_TOPIC_CONCURRENCY = 5
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


class LinuxDoScraper(BaseScraper):
    """Scraper for linux.do (a Discourse-based forum)."""

    def __init__(self, config: LinuxDoConfig, http_client: httpx.AsyncClient):
        super().__init__(config.model_dump(), http_client)
        self.ld_config = config
        self.base_url = config.base_url.rstrip("/")
        self._topic_semaphore = asyncio.Semaphore(MAX_TOPIC_CONCURRENCY)
        self._headers = {
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            "Referer": f"{self.base_url}/",
        }
        # linux.do is fronted by Cloudflare which TLS-fingerprints requests.
        # httpx is blocked with 403, so we use primp (already in deps via DDGS)
        # which impersonates a real Chrome TLS handshake.
        self._primp_client = self._build_primp_client()

    @staticmethod
    def _build_primp_client():
        try:
            import primp
        except ImportError:
            logger.warning("primp not installed; linux.do scraper will be disabled")
            return None
        # Suppress primp's stderr warning about unknown impersonate names
        stderr = sys.stderr
        sys.stderr = open(os.devnull, "w")
        try:
            return primp.Client(impersonate="chrome_131", verify=True, timeout=30)
        finally:
            sys.stderr.close()
            sys.stderr = stderr

    async def fetch(self, since: datetime) -> List[ContentItem]:
        if not self.ld_config.enabled:
            return []

        feeds = [f for f in self.ld_config.feeds if f.enabled]
        if not feeds:
            return []

        results = await asyncio.gather(
            *[self._fetch_feed(f, since) for f in feeds],
            return_exceptions=True,
        )

        items: List[ContentItem] = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Error fetching linux.do feed: %s", r)
            elif isinstance(r, list):
                items.extend(r)
        return items

    async def _fetch_feed(self, cfg: LinuxDoFeedConfig, since: datetime) -> List[ContentItem]:
        url = self._feed_url(cfg)
        if not url:
            logger.warning("linux.do feed %s has no resolvable URL", cfg.name)
            return []

        data = await self._get_json(url)
        if not data:
            return []

        topics = data.get("topic_list", {}).get("topics", []) or []
        topics = topics[: cfg.fetch_limit]

        valid: list[dict] = []
        for t in topics:
            created = self._parse_dt(t.get("created_at"))
            if not created or created < since:
                continue
            if t.get("like_count", 0) < cfg.min_likes:
                continue
            valid.append(t)

        if not valid:
            return []

        details = await asyncio.gather(
            *[self._fetch_topic(t["id"]) for t in valid],
            return_exceptions=True,
        )

        items: List[ContentItem] = []
        for topic, detail in zip(valid, details):
            if isinstance(detail, Exception):
                detail = None
            item = self._parse_topic(topic, detail, cfg)
            if item:
                items.append(item)
        return items

    def _feed_url(self, cfg: LinuxDoFeedConfig) -> Optional[str]:
        if cfg.feed == "latest":
            return f"{self.base_url}/latest.json"
        if cfg.feed == "top":
            return f"{self.base_url}/top.json?period={cfg.period}"
        if cfg.feed == "category":
            if not cfg.category_slug or cfg.category_id is None:
                return None
            return f"{self.base_url}/c/{cfg.category_slug}/{cfg.category_id}.json"
        return None

    async def _fetch_topic(self, topic_id: int) -> Optional[dict]:
        async with self._topic_semaphore:
            return await self._get_json(f"{self.base_url}/t/{topic_id}.json")

    async def _get_json(self, url: str) -> Optional[Any]:
        if self._primp_client is None:
            return None
        try:
            response = await asyncio.to_thread(
                self._primp_client.get, url, headers=self._headers
            )
            status = response.status_code
            if status == 429:
                retry_after = 5
                try:
                    retry_after = int(response.headers.get("Retry-After", 5))
                except (ValueError, TypeError):
                    pass
                logger.warning("linux.do rate limited, retrying after %ds", retry_after)
                await asyncio.sleep(retry_after)
                response = await asyncio.to_thread(
                    self._primp_client.get, url, headers=self._headers
                )
                status = response.status_code
            if status >= 400:
                logger.warning("linux.do HTTP %d for %s", status, url)
                return None
            return response.json()
        except Exception as e:
            logger.warning("linux.do request failed for %s: %s", url, e)
            return None

    def _parse_topic(
        self, topic: dict, detail: Optional[dict], cfg: LinuxDoFeedConfig
    ) -> Optional[ContentItem]:
        topic_id = topic.get("id")
        if topic_id is None:
            return None

        title = topic.get("fancy_title") or topic.get("title") or ""
        slug = topic.get("slug") or ""
        url = f"{self.base_url}/t/{slug}/{topic_id}" if slug else f"{self.base_url}/t/{topic_id}"
        created = self._parse_dt(topic.get("created_at")) or datetime.now(timezone.utc)

        posts = []
        if detail:
            posts = detail.get("post_stream", {}).get("posts", []) or []

        op_post = posts[0] if posts else None
        op_text = self._strip_html(op_post.get("cooked")) if op_post else ""
        if len(op_text) > 1500:
            op_text = op_text[:1497] + "..."

        author = (
            (op_post or {}).get("username")
            or topic.get("last_poster_username")
            or "unknown"
        )

        # Top replies (skip OP), sorted by reactions/like count
        replies: list[dict] = []
        fetch_n = self.ld_config.fetch_comments
        if fetch_n > 0 and len(posts) > 1:
            candidates = posts[1:]
            candidates.sort(
                key=lambda p: (
                    p.get("reaction_users_count")
                    or p.get("reactions_summary_count")
                    or _post_score(p)
                ),
                reverse=True,
            )
            replies = candidates[:fetch_n]

        parts = []
        if op_text:
            parts.append(op_text)
        if replies:
            parts.append("\n--- Top Comments ---")
            for p in replies:
                commenter = p.get("username", "anon")
                body = self._strip_html(p.get("cooked"))
                if len(body) > 500:
                    body = body[:497] + "..."
                score = _post_score(p)
                parts.append(f"[{commenter} ({score} pts)]: {body}")

        content = "\n\n".join(parts)
        tags = topic.get("tags") or []

        return ContentItem(
            id=self._generate_id("linuxdo", "topic", str(topic_id)),
            source_type=SourceType.LINUXDO,
            title=title,
            url=url,
            content=content,
            author=author,
            published_at=created,
            metadata={
                "feed_name": cfg.name,
                "category_id": topic.get("category_id"),
                "tags": tags,
                "likes": topic.get("like_count", 0),
                "views": topic.get("views", 0),
                "reply_count": topic.get("reply_count", 0),
                "posts_count": topic.get("posts_count", 0),
                "pinned": topic.get("pinned", False),
                "discussion_url": url,
            },
        )

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            # Discourse timestamps are ISO 8601 with trailing Z
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _strip_html(html: Optional[str]) -> str:
        if not html:
            return ""
        text = HTML_TAG_RE.sub(" ", html)
        text = WHITESPACE_RE.sub(" ", text).strip()
        return text


def _post_score(post: dict) -> int:
    """Best-effort engagement score for a Discourse post."""
    summary = post.get("reactions_summary") or []
    if isinstance(summary, list):
        total = 0
        for r in summary:
            if isinstance(r, dict):
                total += int(r.get("count", 0) or 0)
        if total:
            return total
    return int(post.get("score", 0) or 0)
