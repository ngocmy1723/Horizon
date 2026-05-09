# AGENTS.md

Operational notes for AI agents working in this repo.

## Project

**Horizon** — AI-driven news aggregation pipeline. Fetches from configured sources (RSS, Hacker News, Reddit, Telegram, Twitter/X, GitHub, Gitee, LinuxDo, OneHack, Firecrawl, IndieHackers, BlackHatWorld), deduplicates, AI-scores/filters, enriches with web context + community discussion, summarizes, and delivers via Pages / email / webhooks / MCP.

- Python `>=3.11`, package manager: **uv** (preferred), pip works too.
- Build backend: hatchling. Source layout: `src/` (packaged as `src`).

## Layout

```
src/
  main.py              # CLI entry (horizon)
  orchestrator.py      # Pipeline driver: fetch -> dedup -> score -> enrich -> summarize -> deliver
  models.py            # Pydantic data models
  search.py            # Web search (ddgs) for enrichment
  ai/
    client.py          # Multi-provider LLM client (Claude/OpenAI/Azure OpenAI/Gemini/DeepSeek/Doubao/MiniMax/...)
    analyzer.py        # Scoring + dedup
    enricher.py        # Background context generation
    summarizer.py      # Final markdown briefing generation
    prompts.py, tokens.py, utils.py
  scrapers/
    base.py, hackernews.py, rss.py, reddit.py, telegram.py,
    twitter.py, github.py, gitee.py, linuxdo.py, onehack.py,
    firecrawl.py, indiehackers.py, blackhatworld.py
  services/
    email.py           # SMTP/IMAP newsletter (subscribe/unsubscribe)
    webhook.py         # Feishu/DingTalk/Slack/Discord/custom
    webhook_cli.py     # entry: horizon-webhook
  mcp/
    server.py          # entry: horizon-mcp (MCP server)
    service.py, horizon_adapter.py, run_store.py, errors.py
  setup/
    wizard.py          # entry: horizon-wizard (interactive config generator)
    presets.py, ai_recommend.py, prompts.py, tag_aliases.py
  storage/manager.py   # Run/state persistence under data/
scripts/
  daily-run.sh, check_mcp.py
tests/                 # pytest suite
docs/                  # GitHub Pages site + design docs
data/                  # config.json, summaries/, runs (gitignored content)
```

## Console scripts (`pyproject.toml`)

| Command | Entry |
|---|---|
| `horizon` | `src.main:main` |
| `horizon-mcp` | `src.mcp.server:main` |
| `horizon-wizard` | `src.setup.wizard:main` |
| `horizon-webhook` | `src.services.webhook_cli:main` |

## Common commands

```bash
uv sync                       # install runtime deps
uv sync --extra dev           # + pytest, pytest-cov
uv run horizon                # run pipeline, default 24h window
uv run horizon --hours 48     # custom window
uv run horizon-wizard         # interactive config
uv run pytest                 # run tests (testpaths=["tests"], addopts="-q")
docker-compose run --rm horizon
```

Output briefings land in `data/summaries/`.

## Configuration

- `.env` — secrets (API keys referenced by `api_key_env` in config).
- `data/config.json` — sources, AI provider/model, filtering thresholds, outputs. Templates: `.env.example`, `data/config.example.json`.
- Reference: `docs/configuration.md`. Scoring details: `docs/scoring.md`. Scraper details: `docs/scrapers.md`. Hub design: `docs/horizon-hub-design.md`.

## Runtime dependencies (key ones)

`httpx`, `feedparser`, `anthropic`, `openai`, `google-genai`, `pydantic>=2`, `python-dateutil`, `rich`, `tenacity`, `python-dotenv`, `ddgs`, `beautifulsoup4`, `markdown`, `mcp`.

No DeepSeek/Doubao/MiniMax SDK — those go through the OpenAI-compatible path in `src/ai/client.py`.

## Tests

Pytest config in `pyproject.toml` (`minversion=8.0`, `-q`, `testpaths=["tests"]`). Existing tests cover: analyzer, summarizer, MCP (adapter/errors/run_store/service smoke), Azure OpenAI client, MiniMax client, Reddit, Twitter, Firecrawl, IndieHackers, OneHack, Gitee scrapers, webhook service. Add tests next to these when modifying corresponding modules.

## Conventions

- Python 3.11+ idioms; type hints with Pydantic models in `src/models.py`.
- New scrapers: subclass `src/scrapers/base.py`, register in orchestrator/config schema.
- New AI providers: extend `src/ai/client.py` (prefer OpenAI-compatible endpoint when possible).
- New webhook channels: extend `src/services/webhook.py`.
- Don't commit `.env` or anything under `data/` other than the `*.example.*` templates.

## CI / Deployment

GitHub Actions workflow `.github/workflows/daily-summary.yml` runs the pipeline on a cron and publishes `docs/` to GitHub Pages (`deploy-docs.yml` badge in README).
