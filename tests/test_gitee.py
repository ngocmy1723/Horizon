"""Smoke tests for the Gitee scraper."""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from src.models import GiteeSourceConfig, SourceType, SourcesConfig
from src.scrapers.gitee import GiteeScraper


def test_sources_config_gitee_default_empty():
    sc = SourcesConfig()
    assert sc.gitee == []


def test_gitee_source_config_defaults():
    cfg = GiteeSourceConfig(type="repo_releases", owner="o", repo="r")
    assert cfg.enabled is True
    assert cfg.username is None


def test_parse_event_push():
    scraper = GiteeScraper([], http_client=None)  # type: ignore[arg-type]
    event = {
        "id": 1,
        "type": "PushEvent",
        "created_at": "2026-01-01T00:00:00+00:00",
        "repo": {"full_name": "alice/proj"},
        "payload": {"commits": [{"message": "fix x"}, {"message": "feat y"}]},
    }
    item = scraper._parse_event(event, "alice")
    assert item is not None
    assert item.source_type == SourceType.GITEE
    assert item.id == "gitee:event:1"
    assert "alice/proj" in item.title
    assert "fix x" in (item.content or "")
    assert str(item.url) == "https://gitee.com/alice/proj"


def test_parse_event_release_uses_html_url():
    scraper = GiteeScraper([], http_client=None)  # type: ignore[arg-type]
    event = {
        "id": 2,
        "type": "ReleaseEvent",
        "created_at": "2026-01-01T00:00:00Z",
        "repo": {"full_name": "alice/proj"},
        "payload": {
            "release": {
                "tag_name": "v1.0",
                "body": "notes",
                "html_url": "https://gitee.com/alice/proj/releases/tag/v1.0",
            }
        },
    }
    item = scraper._parse_event(event, "alice")
    assert item is not None
    assert item.metadata["event_type"] == "ReleaseEvent"
    assert "v1.0" in item.title
    assert str(item.url) == "https://gitee.com/alice/proj/releases/tag/v1.0"


def test_parse_event_unknown_type_returns_none():
    scraper = GiteeScraper([], http_client=None)  # type: ignore[arg-type]
    item = scraper._parse_event(
        {
            "id": 3,
            "type": "ForkEvent",
            "created_at": "2026-01-01T00:00:00Z",
            "repo": {"full_name": "alice/proj"},
            "payload": {},
        },
        "alice",
    )
    assert item is None


def test_fetch_repo_releases_filters_since():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "id": 10,
                    "tag_name": "v2",
                    "body": "new",
                    "created_at": "2026-02-01T00:00:00+00:00",
                    "author": {"login": "bob"},
                },
                {
                    "id": 11,
                    "tag_name": "v1",
                    "body": "old",
                    "created_at": "2025-01-01T00:00:00+00:00",
                    "author": {"login": "bob"},
                },
            ],
        )

    transport = httpx.MockTransport(handler)
    sources = [GiteeSourceConfig(type="repo_releases", owner="o", repo="r")]

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            scraper = GiteeScraper(sources, client)
            since = datetime(2026, 1, 15, tzinfo=timezone.utc)
            return await scraper.fetch(since)

    items = asyncio.run(run())
    assert "/repos/o/r/releases" in captured["url"]
    assert len(items) == 1
    assert items[0].metadata["tag"] == "v2"
    assert items[0].source_type == SourceType.GITEE
