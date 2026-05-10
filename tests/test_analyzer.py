import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import src.ai.analyzer as analyzer_module
from src.ai.analyzer import (
    ContentAnalyzer,
    _COMMENTS_CHAR_LIMIT,
    _COMMENTS_MARKER,
    _MAIN_CONTENT_CHAR_LIMIT,
)
from src.models import ContentItem, SourceType


def _make_item(item_id: str) -> ContentItem:
    return ContentItem(
        id=item_id,
        source_type=SourceType.RSS,
        title=f"Item {item_id}",
        url="https://example.com/item",
        published_at=datetime(2026, 4, 26, tzinfo=timezone.utc),
    )


def test_analyze_batch_does_not_sleep_by_default(monkeypatch):
    analyzer = ContentAnalyzer(SimpleNamespace())
    items = [_make_item("rss:test:1"), _make_item("rss:test:2")]
    sleep_calls = []

    async def fake_analyze_item(item):
        item.ai_score = 8.0

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(analyzer, "_analyze_item", fake_analyze_item)
    monkeypatch.setattr(analyzer_module.asyncio, "sleep", fake_sleep)

    result = asyncio.run(analyzer.analyze_batch(items))

    assert len(result) == 2
    assert sleep_calls == []


def test_analyze_batch_sleeps_between_items_when_throttle_configured(monkeypatch):
    client = SimpleNamespace(config=SimpleNamespace(throttle_sec=1.5))
    analyzer = ContentAnalyzer(client)
    items = [_make_item("rss:test:1"), _make_item("rss:test:2"), _make_item("rss:test:3")]
    sleep_calls = []

    async def fake_analyze_item(item):
        item.ai_score = 8.0

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(analyzer, "_analyze_item", fake_analyze_item)
    monkeypatch.setattr(analyzer_module.asyncio, "sleep", fake_sleep)

    asyncio.run(analyzer.analyze_batch(items))

    assert sleep_calls == [1.5, 1.5]


def test_main_budget_dominates_comments_budget():
    """Article body should never get less prompt space than community comments."""
    assert _MAIN_CONTENT_CHAR_LIMIT >= _COMMENTS_CHAR_LIMIT


def test_analyze_item_truncates_main_and_comments_with_correct_budgets():
    captured = {}

    class FakeClient:
        config = SimpleNamespace(throttle_sec=0)

        async def complete(self, system, user):
            captured["user"] = user
            return '{"score": 5, "reason": "ok", "summary": "s", "tags": []}'

    analyzer = ContentAnalyzer(FakeClient())
    main_sentinel = "\u00b6"  # ¶ — unlikely to appear in prompt template
    comments_sentinel = "\u00a7"  # § — unlikely to appear in prompt template
    main_body = main_sentinel * (_MAIN_CONTENT_CHAR_LIMIT * 2)
    comments_body = comments_sentinel * (_COMMENTS_CHAR_LIMIT * 2)
    item = _make_item("rss:test:1")
    item.content = f"{main_body}\n{_COMMENTS_MARKER}\n{comments_body}"

    asyncio.run(analyzer._analyze_item(item))

    user = captured["user"]
    # Main truncation respects its budget; comments truncation respects its own.
    assert user.count(main_sentinel) == _MAIN_CONTENT_CHAR_LIMIT
    assert user.count(comments_sentinel) == _COMMENTS_CHAR_LIMIT
    # Marker itself never leaks into the prompt.
    assert _COMMENTS_MARKER not in user


def test_analyze_item_respects_config_overrides():
    """Char budgets from AIConfig override module-level defaults."""
    captured = {}

    class FakeClient:
        config = SimpleNamespace(
            throttle_sec=0,
            analyzer_main_chars=300,
            analyzer_comments_chars=100,
        )

        async def complete(self, system, user):
            captured["user"] = user
            return '{"score": 5, "reason": "ok", "summary": "s", "tags": []}'

    analyzer = ContentAnalyzer(FakeClient())
    main_sentinel = "\u00b6"
    comments_sentinel = "\u00a7"
    item = _make_item("rss:test:cfg")
    item.content = (
        f"{main_sentinel * 1000}\n{_COMMENTS_MARKER}\n{comments_sentinel * 1000}"
    )

    asyncio.run(analyzer._analyze_item(item))

    user = captured["user"]
    assert user.count(main_sentinel) == 300
    assert user.count(comments_sentinel) == 100


def test_analyze_item_falls_back_to_defaults_on_invalid_config():
    """Non-positive / non-int budgets fall back to module defaults."""
    captured = {}

    class FakeClient:
        config = SimpleNamespace(
            throttle_sec=0,
            analyzer_main_chars=0,
            analyzer_comments_chars=-50,
        )

        async def complete(self, system, user):
            captured["user"] = user
            return '{"score": 5, "reason": "ok", "summary": "s", "tags": []}'

    analyzer = ContentAnalyzer(FakeClient())
    main_sentinel = "\u00b6"
    comments_sentinel = "\u00a7"
    item = _make_item("rss:test:bad-cfg")
    item.content = (
        f"{main_sentinel * (_MAIN_CONTENT_CHAR_LIMIT * 2)}\n"
        f"{_COMMENTS_MARKER}\n"
        f"{comments_sentinel * (_COMMENTS_CHAR_LIMIT * 2)}"
    )

    asyncio.run(analyzer._analyze_item(item))

    user = captured["user"]
    assert user.count(main_sentinel) == _MAIN_CONTENT_CHAR_LIMIT
    assert user.count(comments_sentinel) == _COMMENTS_CHAR_LIMIT


def test_analyze_item_no_comments_uses_full_main_budget():
    captured = {}

    class FakeClient:
        config = SimpleNamespace(throttle_sec=0)

        async def complete(self, system, user):
            captured["user"] = user
            return '{"score": 5, "reason": "ok", "summary": "s", "tags": []}'

    analyzer = ContentAnalyzer(FakeClient())
    main_sentinel = "\u00b6"
    item = _make_item("rss:test:2")
    item.content = main_sentinel * (_MAIN_CONTENT_CHAR_LIMIT * 2)

    asyncio.run(analyzer._analyze_item(item))

    user = captured["user"]
    assert user.count(main_sentinel) == _MAIN_CONTENT_CHAR_LIMIT
    assert "Community Comments" not in user
