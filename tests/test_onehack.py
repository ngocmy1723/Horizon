"""Smoke tests for the onehack.st scraper."""

from datetime import datetime, timezone

from src.models import (
    LinuxDoFeedConfig,
    OneHackConfig,
    SourceType,
    SourcesConfig,
)
from src.scrapers.linuxdo import LinuxDoScraper
from src.scrapers.onehack import OneHackScraper


def test_onehack_config_defaults():
    cfg = OneHackConfig()
    assert cfg.base_url == "https://onehack.st"
    assert cfg.enabled is True  # explicit instances default to enabled
    assert cfg.fetch_comments == 5


def test_sources_config_onehack_default_disabled():
    sc = SourcesConfig()
    assert sc.onehack.enabled is False
    assert sc.onehack.base_url == "https://onehack.st"


def test_onehack_scraper_class_attrs():
    assert OneHackScraper.SOURCE_TYPE == SourceType.ONEHACK
    assert OneHackScraper.SOURCE_ID_PREFIX == "onehack"
    assert OneHackScraper.LOG_NAME == "onehack.st"
    # Inherits behavior from LinuxDoScraper
    assert issubclass(OneHackScraper, LinuxDoScraper)


def test_onehack_parse_topic_uses_subclass_identity():
    cfg = OneHackConfig(
        base_url="https://onehack.st",
        feeds=[LinuxDoFeedConfig(name="latest")],
    )
    scraper = OneHackScraper(cfg, http_client=None)  # type: ignore[arg-type]

    topic = {
        "id": 12345,
        "title": "Hello onehack",
        "fancy_title": "Hello onehack",
        "slug": "hello-onehack",
        "created_at": "2026-01-01T00:00:00.000Z",
        "category_id": 1,
        "tags": ["tag1"],
        "like_count": 3,
        "views": 10,
        "reply_count": 0,
        "posts_count": 1,
    }
    detail = {
        "post_stream": {
            "posts": [
                {"username": "alice", "cooked": "<p>hi</p>"},
            ]
        }
    }
    feed_cfg = LinuxDoFeedConfig(name="latest")
    item = scraper._parse_topic(topic, detail, feed_cfg)
    assert item is not None
    assert item.id == "onehack:topic:12345"
    assert item.source_type == SourceType.ONEHACK
    assert str(item.url).startswith("https://onehack.st/t/hello-onehack/12345")
    assert item.author == "alice"
    assert item.published_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
