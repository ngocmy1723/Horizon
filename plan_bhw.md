# Plan: BlackHatWorld scraper (`src/scrapers/blackhatworld.py`)

## Summary

New scraper subclassing `BaseScraper`. Uses `curl_cffi` (impersonate Chrome) to bypass Cloudflare; falls back to `httpx` with realistic headers if `curl_cffi` missing. Parses XenForo forum index via BeautifulSoup -> `ContentItem`. Optional fetch of first-post body per thread.

## Implementation steps

### 1. `src/models.py`

- Add `SourceType.BLACKHATWORLD = "blackhatworld"`.
- New `BlackHatWorldForumConfig`:
  - `slug: str` (e.g. `"black-hat-seo"`)
  - `id: int` (e.g. `9`)
  - `name: Optional[str]`
  - `category: Optional[str]`
  - `enabled: bool = True`
  - `fetch_limit: int = 30`
  - `fetch_first_post: bool = True`
  - `min_replies: int = 0`
- New `BlackHatWorldConfig`:
  - `enabled: bool = False`
  - `base_url: str = "https://www.blackhatworld.com"`
  - `impersonate: str = "chrome120"`
  - `request_delay_sec: float = 1.5`
  - `forums: List[BlackHatWorldForumConfig] = []`
- Add `blackhatworld: BlackHatWorldConfig = Field(default_factory=BlackHatWorldConfig)` to `SourcesConfig`.

### 2. `src/scrapers/blackhatworld.py` (new, ~150-180 LOC)

```
BlackHatWorldScraper(BaseScraper)
  __init__(config, http_client)
    super().__init__({"config": config}, http_client)
    self.bhw = config
    self._curl_session = None  # lazy AsyncSession from curl_cffi

  async fetch(since)
    if not enabled or no forums -> []
    for forum in forums (enabled):
      items += await _fetch_forum(forum, since)
    cleanup curl session
    return items

  async _get(url) -> str
    # tier 1: curl_cffi.AsyncSession(impersonate=chrome120)
    # tier 2 fallback: self.client (httpx) with realistic UA + Accept headers
    raise on 403/blocked

  async _fetch_forum(forum, since)
    url = f"{base_url}/forums/{slug}.{id}/"
    html = await _get(url)
    soup = BeautifulSoup(html, "html.parser")
    threads = soup.select(".structItem--thread")[:fetch_limit]
    for el in threads:
      title_a    = el.select_one(".structItem-title a:not(.labelLink)")
      time_el    = el.select_one("time.u-dt")
      replies_el = el.select_one(".structItem-cell--meta dl:nth-of-type(1) dd")
      published  = datetime.fromisoformat(time_el["datetime"].replace("Z","+00:00"))
      if published < since: skip
      if int(replies) < forum.min_replies: skip
      thread_url = urljoin(base_url, title_a["href"])
      tid = el["data-content-key"].removeprefix("thread-")

      content = ""
      if forum.fetch_first_post:
        await asyncio.sleep(request_delay_sec)
        detail = BeautifulSoup(await _get(thread_url), "html.parser")
        body = detail.select_one("article.message--post:first-of-type .bbWrapper")
        content = body.get_text("\n", strip=True)[:5000] if body else ""

      yield ContentItem(
        id=_generate_id("blackhatworld", forum.slug, tid),
        source_type=SourceType.BLACKHATWORLD,
        title=title_a.get_text(strip=True),
        url=thread_url,
        content=content,
        author=el.select_one(".username").get_text(strip=True),
        published_at=published,
        metadata={"feed_name": forum.name or forum.slug,
                  "category": forum.category,
                  "replies": replies_int,
                  "thread_id": tid},
      )
```

### 3. `src/orchestrator.py`

Add import `BlackHatWorldScraper`. In `fetch_all_sources`:

```python
if self.config.sources.blackhatworld.enabled and self.config.sources.blackhatworld.forums:
    bhw_scraper = BlackHatWorldScraper(self.config.sources.blackhatworld, client)
    tasks.append(self._fetch_with_progress("BlackHatWorld", bhw_scraper, since))
```

### 4. `data/config.example.json`

Add disabled-by-default block:

```json
"blackhatworld": {
  "enabled": false,
  "base_url": "https://www.blackhatworld.com",
  "impersonate": "chrome120",
  "request_delay_sec": 1.5,
  "forums": [
    {
      "slug": "black-hat-seo",
      "id": 9,
      "name": "Black Hat SEO",
      "category": "seo",
      "fetch_limit": 30,
      "min_replies": 5,
      "fetch_first_post": true,
      "enabled": true
    }
  ]
}
```

### 5. `pyproject.toml`

Add to `dependencies`:

```
"curl-cffi>=0.7.0",
```

(BS4 + httpx already present.)

### 6. `tests/test_blackhatworld.py` (new)

- Fixture: trimmed real XenForo forum-index HTML + 1 thread page HTML.
- Mock `_get` -> return fixture HTML.
- Asserts: thread count, title, url, published_at, author, replies, thread_id parsed correctly; `since` filter respected; `fetch_first_post=True` populates body; fallback when `curl_cffi` absent (monkeypatch ImportError).

### 7. `AGENTS.md`

Add `blackhatworld` to scraper list.

## Files touched

| File | Change |
|---|---|
| `src/models.py` | enum value + 2 config classes + `SourcesConfig` field |
| `src/scrapers/blackhatworld.py` | **new** ~150-180 LOC |
| `src/orchestrator.py` | import + fetch branch |
| `data/config.example.json` | sample block |
| `pyproject.toml` | dep `curl-cffi` |
| `tests/test_blackhatworld.py` | **new** unit tests |
| `AGENTS.md` | scraper list update |

## Verification

- [ ] `uv sync` (install curl-cffi)
- [ ] `uv run pytest tests/test_blackhatworld.py -q`
- [ ] `uv run pytest -q` (full suite, no regressions)
- [ ] Pydantic schema validate: `uv run python -c "import json; from src.models import Config; Config(**json.load(open('data/config.example.json')))"`
- [ ] Manual smoke: enable 1 forum in `data/config.json`, run `uv run horizon --hours 168`, check `data/summaries/` + log `Found N items from BlackHatWorld`

## Risks

- **CF tier-up to "Under Attack"** -> `curl_cffi` fails. Fallback path: integrate camoufox/Firecrawl later. Plan logs warn + skips forum.
- **XenForo upgrade -> selector drift**. Wrap per-thread parse in try/except, warn-log, don't crash forum.
- **Rate limit / IP ban**. `request_delay_sec` (default 1.5s) between thread fetches.
- **BHW ToS** may forbid scraping. Caller responsibility.
- **`curl_cffi` native build**. Wheels exist for linux/mac/win x86_64 + arm64. Debian-slim Docker base OK.
