import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.models import FirecrawlConfig, FirecrawlSourceConfig, SourceType
from src.scrapers.firecrawl import FirecrawlScraper


def _scrape_response() -> dict:
    return {
        "success": True,
        "data": {
            "markdown": "# Hello\n\nbody text",
            "metadata": {
                "title": "Hello world",
                "description": "A test page",
                "sourceURL": "https://example.com/post",
                "author": "Alice",
                "publishedTime": "2030-01-02T03:04:05Z",
                "language": "en",
            },
        },
    }


def test_firecrawl_scrape_mode(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/scrape"
        assert request.headers["Authorization"] == "Bearer fc-test"
        posted["url"] = str(request.url)
        return httpx.Response(200, json=_scrape_response())

    transport = httpx.MockTransport(handler)

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            cfg = FirecrawlConfig(
                enabled=True,
                sources=[
                    FirecrawlSourceConfig(
                        name="example",
                        url="https://example.com/post",
                        mode="scrape",
                    )
                ],
            )
            scraper = FirecrawlScraper(cfg, client)
            return await scraper.fetch(datetime(2020, 1, 1, tzinfo=timezone.utc))

    items = asyncio.run(go())
    assert len(items) == 1
    item = items[0]
    assert item.source_type == SourceType.FIRECRAWL
    assert item.title == "Hello world"
    assert str(item.url) == "https://example.com/post"
    assert "body text" in item.content
    assert item.author == "Alice"
    assert item.metadata["feed_name"] == "example"
    assert item.metadata["mode"] == "scrape"
    assert "scrape" in posted["url"]


def test_firecrawl_skips_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not hit network without API key")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = FirecrawlConfig(
                enabled=True,
                sources=[
                    FirecrawlSourceConfig(name="x", url="https://example.com")
                ],
            )
            return await FirecrawlScraper(cfg, client).fetch(
                datetime(2020, 1, 1, tzinfo=timezone.utc)
            )

    assert asyncio.run(go()) == []


def test_firecrawl_drops_items_older_than_since(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    def handler(request: httpx.Request) -> httpx.Response:
        body = _scrape_response()
        body["data"]["metadata"]["publishedTime"] = "2000-01-01T00:00:00Z"
        return httpx.Response(200, json=body)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = FirecrawlConfig(
                enabled=True,
                sources=[FirecrawlSourceConfig(name="x", url="https://example.com")],
            )
            return await FirecrawlScraper(cfg, client).fetch(
                datetime.now(timezone.utc) - timedelta(days=1)
            )

    assert asyncio.run(go()) == []


def test_firecrawl_crawl_mode_polls_until_completed(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/crawl":
            return httpx.Response(200, json={"success": True, "id": "job-1"})
        if request.method == "GET" and request.url.path == "/v1/crawl/job-1":
            state["polls"] += 1
            if state["polls"] < 2:
                return httpx.Response(200, json={"status": "scraping", "data": []})
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "data": [
                        {
                            "markdown": "page A",
                            "metadata": {
                                "title": "A",
                                "sourceURL": "https://example.com/a",
                                "publishedTime": "2030-05-01T00:00:00Z",
                            },
                        },
                        {
                            "markdown": "page B",
                            "metadata": {
                                "title": "B",
                                "sourceURL": "https://example.com/b",
                                "publishedTime": "2030-05-02T00:00:00Z",
                            },
                        },
                    ],
                },
            )
        raise AssertionError(f"unexpected {request.method} {request.url}")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = FirecrawlConfig(
                enabled=True,
                sources=[
                    FirecrawlSourceConfig(
                        name="site",
                        url="https://example.com",
                        mode="crawl",
                        limit=2,
                        poll_interval_sec=0.0,
                        poll_timeout_sec=5.0,
                    )
                ],
            )
            return await FirecrawlScraper(cfg, client).fetch(
                datetime(2020, 1, 1, tzinfo=timezone.utc)
            )

    items = asyncio.run(go())
    assert state["polls"] >= 2
    assert {i.title for i in items} == {"A", "B"}
    assert all(i.metadata["mode"] == "crawl" for i in items)
