import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from src.models import IndieHackersConfig, SourceType
from src.scrapers.indiehackers import IndieHackersScraper, _parse_long_date


_HOMEPAGE = """
<html><body><main>
<div class="story homepage-post story--featured ember-view normal">
  <a href="/post/first-post-abc123" class="story__text-link">
    <h3 class="story__title"> First Post Title </h3>
  </a>
  <a href="/alice?id=u1" class="user-link__link">
    <span class="user-link__name user-link__name--username">alice</span>
  </a>
  <a class="story__count story__count--likes">
    <span class="story__count-number">42</span>
    <span class="story__count-text">upvotes</span>
  </a>
  <a class="story__count story__count--comments">
    <span class="story__count-number">7</span>
    <span class="story__count-text">comments</span>
  </a>
</div>
<div class="story homepage-post story--no-image ember-view normal">
  <a href="/post/second-post-def456" class="story__text-link">
    <h3 class="story__title">Second &amp; Cheap Post</h3>
  </a>
  <a class="user-link__link"><span class="user-link__name user-link__name--username">bob</span></a>
  <a class="story__count story__count--likes"><span class="story__count-number">2</span></a>
  <a class="story__count story__count--comments"><span class="story__count-number">0</span></a>
</div>
</main></body></html>
"""

_POST_PAGE = """
<html><body>
<a class="active ember-view post-page__date">on <span>May 6, 2026</span></a>
<div class="post-page__body content ember-view"><p>Hello <b>world</b>.</p></div>
<!-- end -->
</body></html>
"""


def test_parses_homepage_and_filters_low_upvotes():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text=_HOMEPAGE)
        if request.url.path.startswith("/post/"):
            return httpx.Response(200, text=_POST_PAGE)
        raise AssertionError(f"unexpected {request.url}")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = IndieHackersConfig(enabled=True, min_upvotes=10, fetch_limit=10)
            return await IndieHackersScraper(cfg, client).fetch(
                datetime(2020, 1, 1, tzinfo=timezone.utc)
            )

    items = asyncio.run(go())
    assert len(items) == 1
    item = items[0]
    assert item.source_type == SourceType.INDIEHACKERS
    assert item.title == "First Post Title"
    assert item.author == "alice"
    assert str(item.url).endswith("/post/first-post-abc123")
    assert item.metadata["upvotes"] == 42
    assert item.metadata["comment_count"] == 7
    assert item.metadata["featured"] is True
    assert "Hello world" in item.content
    assert item.published_at == datetime(2026, 5, 6, tzinfo=timezone.utc)
    assert item.id.startswith("indiehackers:post:")


def test_skip_when_disabled():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not hit network when disabled")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = IndieHackersConfig(enabled=False)
            return await IndieHackersScraper(cfg, client).fetch(
                datetime(2020, 1, 1, tzinfo=timezone.utc)
            )

    assert asyncio.run(go()) == []


def test_drops_items_older_than_since():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text=_HOMEPAGE)
        return httpx.Response(
            200,
            text='<a class="post-page__date">on <span>January 1, 2000</span></a>'
                 '<div class="post-page__body content ember-view">old</div><!-- -->',
        )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = IndieHackersConfig(enabled=True, min_upvotes=0)
            return await IndieHackersScraper(cfg, client).fetch(
                datetime(2025, 1, 1, tzinfo=timezone.utc)
            )

    assert asyncio.run(go()) == []


def test_parse_long_date():
    assert _parse_long_date("May 6, 2026") == datetime(2026, 5, 6, tzinfo=timezone.utc)
    assert _parse_long_date("not a date") is None
