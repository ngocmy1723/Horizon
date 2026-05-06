"""Unit tests for daily summary rendering."""

from datetime import datetime, timezone

from src.ai.summarizer import DailySummarizer
from src.models import ContentItem, SourceType


def _make_item(idx: int) -> ContentItem:
    item = ContentItem(
        id=f"rss:item-{idx}",
        source_type=SourceType.RSS,
        title=f"Important Item {idx}",
        url=f"https://example.com/items/{idx}",
        content="content",
        author="tester",
        published_at=datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc),
    )
    item.ai_score = 8.0
    item.ai_summary = f"Summary for item {idx}."
    item.ai_tags = ["AI", "News"]
    return item


def test_generate_webhook_overview_lists_items_without_full_details():
    summarizer = DailySummarizer()
    items = [_make_item(1), _make_item(2)]

    result = summarizer.generate_webhook_overview(
        items,
        date="2026-04-25",
        total_fetched=10,
        language="en",
    )

    assert "Selected 2 important items from 10 fetched items" in result
    assert "1. [Important Item 1](https://example.com/items/1)" in result
    assert "2. [Important Item 2](https://example.com/items/2)" in result
    assert "Summary for item 1." not in result


def test_generate_webhook_item_renders_single_item_detail():
    summarizer = DailySummarizer()

    result = summarizer.generate_webhook_item(
        _make_item(1),
        language="en",
        index=1,
        total=2,
    )

    assert result.startswith("Item 1/2")
    assert "## [Important Item 1](https://example.com/items/1)" in result
    assert "Summary for item 1." in result
    assert "**Tags**: `#AI`, `#News`" in result


def test_generate_webhook_item_includes_discussion_link_when_distinct():
    summarizer = DailySummarizer()
    item = _make_item(1)
    item.metadata["discussion_url"] = "https://news.ycombinator.com/item?id=1"

    result = summarizer.generate_webhook_item(
        item,
        language="en",
        index=1,
        total=1,
    )

    assert "tester · Apr 25, 08:00 · [Discussion](https://news.ycombinator.com/item?id=1)" in result


def test_generate_webhook_item_omits_discussion_link_when_same_as_item_url():
    summarizer = DailySummarizer()
    item = _make_item(1)
    item.metadata["discussion_url"] = item.url

    result = summarizer.generate_webhook_item(
        item,
        language="en",
        index=1,
        total=1,
    )

    assert "[Discussion](https://example.com/items/1)" not in result


def test_generate_webhook_item_uses_localized_discussion_label():
    summarizer = DailySummarizer()
    item = _make_item(1)
    item.metadata["discussion_url"] = "https://www.reddit.com/r/python/comments/abc123/test/"

    result = summarizer.generate_webhook_item(
        item,
        language="zh",
        index=1,
        total=1,
    )

    assert "[社区讨论](https://www.reddit.com/r/python/comments/abc123/test/)" in result


def test_generate_summary_supports_vietnamese_labels():
    summarizer = DailySummarizer()
    item = _make_item(1)
    item.metadata["detailed_summary_vi"] = "Đây là bản tóm tắt bằng tiếng Việt."
    item.metadata["title_vi"] = "Tiêu đề tiếng Việt"

    import asyncio

    result = asyncio.run(
        summarizer.generate_summary([item], "2026-04-25", total_fetched=1, language="vi")
    )

    assert "Horizon Hằng Ngày - 2026-04-25" in result
    assert "Đây là bản tóm tắt bằng tiếng Việt." in result
    assert "Tiêu đề tiếng Việt" in result
    assert "**Nhãn**: `#AI`, `#News`" in result


def test_generate_webhook_item_prefix_vietnamese():
    summarizer = DailySummarizer()

    result = summarizer.generate_webhook_item(
        _make_item(1),
        language="vi",
        index=2,
        total=5,
    )

    assert result.startswith("Mục 2/5")


def test_build_enrichment_prompts_include_vietnamese():
    from src.ai.prompts import (
        build_enrichment_system_prompt,
        build_enrichment_user_template,
    )

    system = build_enrichment_system_prompt(["en", "zh", "vi"])
    user = build_enrichment_user_template(["en", "zh", "vi"])

    # System prompt advertises all three languages and correct key naming.
    assert "Vietnamese" in system
    assert "title_vi" in system and "title_en" in system and "title_zh" in system
    assert "community_discussion_vi" in system

    # User template has the JSON schema key for every field in every language.
    for field in ("title", "whats_new", "why_it_matters", "key_details", "background", "community_discussion"):
        for lang in ("en", "zh", "vi"):
            assert f'"{field}_{lang}"' in user

    # Template still formattable with the standard placeholders the enricher passes in.
    formatted = user.format(
        title="T",
        url="U",
        summary="S",
        score=9,
        reason="R",
        tags="t",
        content="C",
        comments_section="",
        web_context="W",
    )
    assert '"title_vi"' in formatted
