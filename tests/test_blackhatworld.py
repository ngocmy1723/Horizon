"""Tests for the BlackHatWorld (XenForo) scraper."""

from __future__ import annotations

import asyncio
import builtins
from datetime import datetime, timezone

import httpx
import pytest

from src.models import (
    BlackHatWorldConfig,
    BlackHatWorldForumConfig,
    SourceType,
)
from src.scrapers.blackhatworld import BlackHatWorldScraper


_FORUM_HTML = """
<html><body>
<div class="structItemContainer">
  <div class="structItem structItem--thread js-inlineModContainer is-unread"
       data-author="alice" data-content-key="thread-1234567">
    <div class="structItem-cell structItem-cell--main">
      <div class="structItem-title">
        <a href="/forums/black-hat-seo.9/" class="labelLink">Label</a>
        <a href="/threads/awesome-seo-trick.1234567/">Awesome SEO trick</a>
      </div>
      <div class="structItem-minor">
        <ul class="structItem-parts">
          <li><a class="username" href="/members/alice.1/">alice</a></li>
          <li><time class="u-dt"
                    datetime="2026-05-08T12:00:00Z"
                    data-time="1746705600">May 8, 2026</time></li>
        </ul>
      </div>
    </div>
    <div class="structItem-cell structItem-cell--meta">
      <dl class="pairs pairs--justified">
        <dt>Replies</dt><dd>42</dd>
      </dl>
      <dl class="pairs pairs--justified structItem-minor">
        <dt>Views</dt><dd>1.2K</dd>
      </dl>
    </div>
  </div>

  <div class="structItem structItem--thread"
       data-author="bob" data-content-key="thread-2222">
    <div class="structItem-cell structItem-cell--main">
      <div class="structItem-title">
        <a href="/threads/old-thread.2222/">Old thread</a>
      </div>
      <div class="structItem-minor">
        <ul class="structItem-parts">
          <li><a class="username">bob</a></li>
          <li><time class="u-dt"
                    datetime="2020-01-01T00:00:00Z">Jan 1, 2020</time></li>
        </ul>
      </div>
    </div>
    <div class="structItem-cell structItem-cell--meta">
      <dl class="pairs pairs--justified">
        <dt>Replies</dt><dd>10</dd>
      </dl>
    </div>
  </div>

  <div class="structItem structItem--thread"
       data-author="carol" data-content-key="thread-3333">
    <div class="structItem-cell structItem-cell--main">
      <div class="structItem-title">
        <a href="/threads/low-engagement.3333/">Low engagement</a>
      </div>
      <div class="structItem-minor">
        <ul class="structItem-parts">
          <li><a class="username">carol</a></li>
          <li><time class="u-dt"
                    datetime="2026-05-08T13:00:00Z">May 8, 2026</time></li>
        </ul>
      </div>
    </div>
    <div class="structItem-cell structItem-cell--meta">
      <dl class="pairs pairs--justified">
        <dt>Replies</dt><dd>1</dd>
      </dl>
    </div>
  </div>
</div>
</body></html>
"""

_THREAD_HTML = """
<html><body>
<article class="message message--post js-post js-inlineModContainer">
  <div class="message-cell message-cell--main">
    <div class="message-userContent">
      <div class="bbWrapper">
        Hello <b>SEO world</b>.
        Multi-line body.
      </div>
    </div>
  </div>
</article>
<article class="message message--post js-post">
  <div class="bbWrapper">A reply we should ignore.</div>
</article>
</body></html>
"""


def _make_scraper(cfg: BlackHatWorldConfig) -> BlackHatWorldScraper:
    # Force the curl_cffi path off so _get falls back to httpx (which we mock).
    async def _no_op(): ...
    transport = httpx.MockTransport(lambda req: httpx.Response(404))
    client = httpx.AsyncClient(transport=transport)
    scraper = BlackHatWorldScraper(cfg, client)
    scraper._curl_unavailable = True  # short-circuit the lazy import path
    return scraper


def _patch_get(scraper, mapping):
    async def fake_get(url):
        return mapping.get(url)
    scraper._get = fake_get  # type: ignore[assignment]


def test_parses_forum_and_filters_since_and_min_replies():
    cfg = BlackHatWorldConfig(
        enabled=True,
        request_delay_sec=0.0,
        forums=[
            BlackHatWorldForumConfig(
                slug="black-hat-seo",
                id=9,
                name="Black Hat SEO",
                category="seo",
                min_replies=5,
                fetch_first_post=True,
            )
        ],
    )
    scraper = _make_scraper(cfg)
    _patch_get(
        scraper,
        {
            "https://www.blackhatworld.com/forums/black-hat-seo.9/": _FORUM_HTML,
            "https://www.blackhatworld.com/threads/awesome-seo-trick.1234567/": _THREAD_HTML,
        },
    )

    items = asyncio.run(scraper.fetch(datetime(2025, 1, 1, tzinfo=timezone.utc)))

    assert len(items) == 1
    item = items[0]
    assert item.source_type == SourceType.BLACKHATWORLD
    assert item.title == "Awesome SEO trick"
    assert (
        str(item.url)
        == "https://www.blackhatworld.com/threads/awesome-seo-trick.1234567/"
    )
    assert item.author == "alice"
    assert item.published_at == datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    assert item.metadata["replies"] == 42
    assert item.metadata["thread_id"] == "1234567"
    assert item.metadata["feed_name"] == "Black Hat SEO"
    assert item.metadata["category"] == "seo"
    assert item.id == "blackhatworld:black-hat-seo:1234567"
    assert "SEO world" in item.content
    assert "A reply we should ignore" not in item.content


def test_skip_when_disabled():
    cfg = BlackHatWorldConfig(enabled=False)
    scraper = _make_scraper(cfg)
    items = asyncio.run(scraper.fetch(datetime(2020, 1, 1, tzinfo=timezone.utc)))
    assert items == []


def test_skip_when_no_forums():
    cfg = BlackHatWorldConfig(enabled=True, forums=[])
    scraper = _make_scraper(cfg)
    items = asyncio.run(scraper.fetch(datetime(2020, 1, 1, tzinfo=timezone.utc)))
    assert items == []


def test_skips_first_post_fetch_when_disabled():
    cfg = BlackHatWorldConfig(
        enabled=True,
        request_delay_sec=0.0,
        forums=[
            BlackHatWorldForumConfig(
                slug="black-hat-seo",
                id=9,
                min_replies=0,
                fetch_first_post=False,
            )
        ],
    )
    scraper = _make_scraper(cfg)
    # Only the forum index is mapped — thread URL would 404 if hit.
    _patch_get(
        scraper,
        {"https://www.blackhatworld.com/forums/black-hat-seo.9/": _FORUM_HTML},
    )

    items = asyncio.run(scraper.fetch(datetime(2025, 1, 1, tzinfo=timezone.utc)))
    # Two threads pass the since filter: 1234567 and 3333.
    assert {i.metadata["thread_id"] for i in items} == {"1234567", "3333"}
    for item in items:
        assert item.content == ""


def test_curl_cffi_missing_marks_unavailable_and_uses_httpx(monkeypatch):
    cfg = BlackHatWorldConfig(enabled=True, forums=[])

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "curl_cffi.requests" or name.startswith("curl_cffi"):
            raise ImportError("forced missing curl_cffi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="<html></html>"))

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            scraper = BlackHatWorldScraper(cfg, client)
            assert scraper._get_curl_session() is None
            assert scraper._curl_unavailable is True
            # Ensure _get returns the httpx response body when curl is missing.
            return await scraper._get("https://www.blackhatworld.com/")

    body = asyncio.run(go())
    assert body == "<html></html>"


def test_parse_replies_handles_abbreviations():
    from bs4 import BeautifulSoup

    cases = {
        "5": 5,
        "1,234": 1234,
        "1.2K": 1200,
        "2M": 2_000_000,
        "garbage": 0,
    }
    for text, expected in cases.items():
        html = (
            '<div class="structItem-cell structItem-cell--meta">'
            f'<dl><dt>Replies</dt><dd>{text}</dd></dl></div>'
        )
        el = BeautifulSoup(html, "html.parser")
        assert BlackHatWorldScraper._parse_replies(el) == expected
