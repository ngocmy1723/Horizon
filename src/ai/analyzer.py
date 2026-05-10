"""Content analysis using AI."""

import asyncio
import json
import re
from typing import List, Optional
from tenacity import retry, stop_after_attempt, wait_exponential
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn

from .client import AIClient
from .prompts import CONTENT_ANALYSIS_SYSTEM, CONTENT_ANALYSIS_USER
from .utils import parse_json_response
from ..models import ContentItem

DEFAULT_THROTTLE_SEC = 0.0

# Char budgets for the analyzer prompt. Main content is the primary signal
# (article body / OP post); comments are supplementary. Keep main >= comments
# so scoring is not biased by community sentiment over the actual content.
# These are fallbacks; runtime values come from AIConfig.analyzer_main_chars /
# analyzer_comments_chars.
_MAIN_CONTENT_CHAR_LIMIT = 2000
_COMMENTS_CHAR_LIMIT = 1000
_COMMENTS_MARKER = "--- Top Comments ---"


class ContentAnalyzer:
    """Analyzes content items using AI to determine importance."""

    def __init__(self, ai_client: AIClient):
        self.client = ai_client

    @staticmethod
    def _parse_json_response(response: str) -> Optional[dict]:
        """Try multiple strategies to extract a JSON object from an AI response.

        Returns the parsed dict, or None if all strategies fail.
        """
        return parse_json_response(response)

    def _get_throttle_sec(self) -> float:
        """Return the configured inter-item throttle, clamped to zero or above."""
        config = getattr(self.client, "config", None)
        throttle_sec = getattr(config, "throttle_sec", DEFAULT_THROTTLE_SEC)
        return max(throttle_sec, 0.0)

    def _get_char_budgets(self) -> tuple[int, int]:
        """Return (main, comments) char budgets from config with module-level fallback.

        Negative or zero values fall back to the module defaults so a misconfig
        cannot silently disable a section.
        """
        config = getattr(self.client, "config", None)
        main = getattr(config, "analyzer_main_chars", _MAIN_CONTENT_CHAR_LIMIT)
        comments = getattr(config, "analyzer_comments_chars", _COMMENTS_CHAR_LIMIT)
        if not isinstance(main, int) or main <= 0:
            main = _MAIN_CONTENT_CHAR_LIMIT
        if not isinstance(comments, int) or comments <= 0:
            comments = _COMMENTS_CHAR_LIMIT
        return main, comments

    async def analyze_batch(self, items: List[ContentItem]) -> List[ContentItem]:
        throttle_sec = self._get_throttle_sec()
        analyzed_items = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("Analyzing", total=len(items))

            for index, item in enumerate(items):
                try:
                    await self._analyze_item(item)
                    analyzed_items.append(item)
                except Exception as e:
                    print(f"Error analyzing item {item.id}: {e}")
                    item.ai_score = 0.0
                    item.ai_reason = "Analysis failed"
                    item.ai_summary = item.title
                    analyzed_items.append(item)
                progress.advance(task)
                if throttle_sec > 0 and index < len(items) - 1:
                    await asyncio.sleep(throttle_sec)

        return analyzed_items

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10)
    )
    async def _analyze_item(self, item: ContentItem) -> None:
        """Analyze a single content item.

        Args:
            item: Content item to analyze (modified in-place)
        """
        main_limit, comments_limit = self._get_char_budgets()

        # Prepare content section + split off comments once if present.
        content_section = ""
        comments_part = ""
        if item.content:
            if _COMMENTS_MARKER in item.content:
                main, comments_part = item.content.split(_COMMENTS_MARKER, 1)
            else:
                main = item.content
            main_text = main.strip()[:main_limit]
            if main_text:
                content_section = f"Content: {main_text}"

        # Prepare discussion section (comments, engagement)
        discussion_parts = []
        comments_text = comments_part.strip()[:comments_limit]
        if comments_text:
            discussion_parts.append(f"Community Comments:\n{comments_text}")

        meta = item.metadata
        engagement_items = []
        if meta.get("score"):
            engagement_items.append(f"score: {meta['score']}")
        if meta.get("descendants"):
            engagement_items.append(f"{meta['descendants']} comments")
        if meta.get("favorite_count"):
            engagement_items.append(f"{meta['favorite_count']} likes")
        if meta.get("retweet_count"):
            engagement_items.append(f"{meta['retweet_count']} retweets")
        if meta.get("reply_count"):
            engagement_items.append(f"{meta['reply_count']} replies")
        if meta.get("views"):
            engagement_items.append(f"{meta['views']} views")
        if meta.get("bookmarks"):
            engagement_items.append(f"{meta['bookmarks']} bookmarks")
        if meta.get("upvote_ratio"):
            engagement_items.append(f"upvote ratio: {meta['upvote_ratio']:.0%}")
        if engagement_items:
            discussion_parts.append(f"Engagement: {', '.join(engagement_items)}")
        if meta.get("discussion_url"):
            discussion_parts.append(f"Discussion: {meta['discussion_url']}")
        if meta.get("community_note"):
            discussion_parts.append(f"Community Note: {meta['community_note']}")

        discussion_section = "\n".join(discussion_parts) if discussion_parts else ""

        # Generate user prompt
        user_prompt = CONTENT_ANALYSIS_USER.format(
            title=item.title,
            source=f"{item.source_type.value}",
            author=item.author or "Unknown",
            url=str(item.url),
            content_section=content_section,
            discussion_section=discussion_section
        )

        # Get AI completion
        response = await self.client.complete(
            system=CONTENT_ANALYSIS_SYSTEM,
            user=user_prompt,
        )

        # Parse JSON response with robust fallback
        result = self._parse_json_response(response)
        if result is None:
            print(f"Warning: could not parse analysis response for {item.id}, using defaults")
            item.ai_score = 0.0
            item.ai_reason = "Analysis response parse failed"
            item.ai_summary = item.title
            item.ai_tags = []
            return

        # Update item with analysis results
        item.ai_score = float(result.get("score", 0))
        item.ai_reason = result.get("reason", "")
        item.ai_summary = result.get("summary", item.title)
        item.ai_tags = result.get("tags", [])
