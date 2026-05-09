"""linux.do (Discourse) scraper implementation."""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, List, Optional

import httpx

from ..models import ContentItem, LinuxDoConfig, LinuxDoFeedConfig, SourceType
from .base import BaseScraper

logger = logging.getLogger(__name__)

MAX_TOPIC_CONCURRENCY = 2
MIN_REQUEST_INTERVAL = 1.0  # seconds between requests (global cooldown)
MAX_RETRIES_429 = 4
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
RATE_LIMIT_RE = re.compile(r"(?:HTTP\s*)?429|Too Many Requests", re.IGNORECASE)


class LinuxDoScraper(BaseScraper):
    """Scraper for linux.do (a Discourse-based forum).

    Subclasses can target other Discourse instances by overriding the
    ``SOURCE_TYPE``, ``SOURCE_ID_PREFIX``, and ``LOG_NAME`` class attributes.
    """

    SOURCE_TYPE: SourceType = SourceType.LINUXDO
    SOURCE_ID_PREFIX: str = "linuxdo"
    LOG_NAME: str = "linux.do"

    def __init__(self, config: LinuxDoConfig, http_client: httpx.AsyncClient):
        super().__init__(config.model_dump(), http_client)
        self.ld_config = config
        self.base_url = config.base_url.rstrip("/")
        self._topic_semaphore = asyncio.Semaphore(MAX_TOPIC_CONCURRENCY)
        self._request_lock = asyncio.Lock()
        self._last_request_ts = 0.0
        self._cooldown_until = 0.0
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
                logger.warning("Error fetching %s feed: %s", self.LOG_NAME, r)
            elif isinstance(r, list):
                items.extend(r)
        return items

    async def _fetch_feed(
        self, cfg: LinuxDoFeedConfig, since: datetime
    ) -> List[ContentItem]:
        url = self._feed_url(cfg)
        if not url:
            logger.warning("%s feed %s has no resolvable URL", self.LOG_NAME, cfg.name)
            return []

        data = await self._get_json(url)
        if not data:
            return []

        topics = data.get("topic_list", {}).get("topics", []) or []
        topics = topics[: cfg.fetch_limit]

        valid: list[dict] = []
        for t in topics:
            updated = (
                self._parse_dt(t.get("bumped_at"))
                or self._parse_dt(t.get("last_posted_at"))
                or self._parse_dt(t.get("created_at"))
            )
            if not updated or updated < since:
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

    async def _throttle(self) -> None:
        """Serialize requests with a global min interval + honor active cooldowns."""
        async with self._request_lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            wait = max(
                self._cooldown_until - now,
                (self._last_request_ts + MIN_REQUEST_INTERVAL) - now,
            )
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_ts = loop.time()

    def _set_cooldown(self, seconds: float) -> None:
        loop = asyncio.get_event_loop()
        target = loop.time() + seconds
        if target > self._cooldown_until:
            self._cooldown_until = target

    @staticmethod
    def _retry_after(response: Any, default: int) -> int:
        try:
            headers = getattr(response, "headers", None) or {}
            value = headers.get("Retry-After") if hasattr(headers, "get") else None
            if value is not None:
                return max(1, int(value))
        except (ValueError, TypeError, AttributeError):
            pass
        return default

    async def _get_json(self, url: str) -> Optional[Any]:
        if self._primp_client is None:
            return None

        for attempt in range(MAX_RETRIES_429 + 1):
            await self._throttle()
            try:
                response = await asyncio.to_thread(
                    self._primp_client.get, url, headers=self._headers
                )
            except Exception as e:
                # primp raises on 429 instead of returning a response.
                msg = str(e)
                if RATE_LIMIT_RE.search(msg) and attempt < MAX_RETRIES_429:
                    backoff = min(60, 5 * (2**attempt))
                    self._set_cooldown(backoff)
                    logger.warning(
                        "%s 429 on %s (attempt %d/%d), cooling down %ds",
                        self.LOG_NAME,
                        url,
                        attempt + 1,
                        MAX_RETRIES_429,
                        backoff,
                    )
                    continue
                logger.warning("%s request failed for %s: %s", self.LOG_NAME, url, e)
                return None

            status = getattr(response, "status_code", 0)
            if status == 429:
                if attempt >= MAX_RETRIES_429:
                    logger.warning(
                        "%s giving up on %s after %d 429s", self.LOG_NAME, url, attempt
                    )
                    return None
                retry_after = self._retry_after(
                    response, default=min(60, 5 * (2**attempt))
                )
                self._set_cooldown(retry_after)
                logger.warning(
                    "%s rate limited (attempt %d/%d), cooling down %ds",
                    self.LOG_NAME,
                    attempt + 1,
                    MAX_RETRIES_429,
                    retry_after,
                )
                continue
            if status >= 400:
                logger.warning("%s HTTP %d for %s", self.LOG_NAME, status, url)
                return None
            try:
                return response.json()
            except Exception as e:
                logger.warning("%s bad JSON for %s: %s", self.LOG_NAME, url, e)
                return None

        return None

    def _parse_topic(
        self, topic: dict, detail: Optional[dict], cfg: LinuxDoFeedConfig
    ) -> Optional[ContentItem]:
        topic_id = topic.get("id")
        if topic_id is None:
            return None

        title = topic.get("fancy_title") or topic.get("title") or ""
        slug = topic.get("slug") or ""
        url = (
            f"{self.base_url}/t/{slug}/{topic_id}"
            if slug
            else f"{self.base_url}/t/{topic_id}"
        )
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
            id=self._generate_id(self.SOURCE_ID_PREFIX, "topic", str(topic_id)),
            source_type=self.SOURCE_TYPE,
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
