---
layout: default
title: Source Scrapers
---

# Source Scrapers

Horizon fetches content from four source types. All scrapers inherit from `BaseScraper`, share an async HTTP client, and implement a `fetch(since)` method that returns a list of `ContentItem` objects. Sources are fetched concurrently via `asyncio.gather`.

## Hacker News

**File**: `src/scrapers/hackernews.py`

Uses the [Firebase HN API](https://hacker-news.firebaseio.com/v0):

- `GET /topstories.json` — fetches top story IDs
- `GET /item/{id}.json` — fetches story/comment details

Stories and their comments are fetched concurrently. For each story, the top 5 comments are included (deleted/dead comments excluded, HTML stripped, truncated at 500 chars).

**Config** (`sources.hackernews`):

```json
{
  "enabled": true,
  "fetch_top_stories": 30,
  "min_score": 100
}
```

- `fetch_top_stories` — number of top story IDs to fetch
- `min_score` — minimum HN points to include a story

**Extracted data**: title, URL (falls back to HN discussion URL), author, score, comment count, and top comment text.

## GitHub

**File**: `src/scrapers/github.py`

Uses the [GitHub REST API](https://api.github.com):

- `GET /users/{username}/events/public` — user activity events
- `GET /repos/{owner}/{repo}/releases` — repository releases

Two source types are supported:

- **`user_events`** — tracks push, create, release, public, and watch events for a user
- **`repo_releases`** — tracks new releases for a specific repository

**Config** (`sources.github`, list of entries):

```json
{
  "type": "user_events",
  "username": "torvalds",
  "enabled": true
}
```

```json
{
  "type": "repo_releases",
  "owner": "golang",
  "repo": "go",
  "enabled": true
}
```

**Authentication**: Set `GITHUB_TOKEN` in your environment for higher rate limits (5000 req/hr vs 60 without).

## RSS

**File**: `src/scrapers/rss.py`

Fetches any Atom/RSS feed using the `feedparser` library. Tries multiple date fields (`published`, `updated`, `created`) with fallback parsing.

**Config** (`sources.rss`, list of entries):

```json
{
  "name": "Simon Willison",
  "url": "https://simonwillison.net/atom/everything/",
  "enabled": true,
  "category": "ai-tools"
}
```

- `category` — optional tag for grouping (e.g., `"programming"`, `"microblog"`)

**Extracted data**: title, URL, author, content (from `summary`/`description`/`content` fields), feed name, category, and entry tags.

## Reddit

**File**: `src/scrapers/reddit.py`

Uses Reddit's public JSON API (`www.reddit.com`):

- `GET /r/{subreddit}/{sort}.json` — subreddit posts
- `GET /user/{username}/submitted.json` — user submissions
- `GET /r/{subreddit}/comments/{post_id}.json` — post comments

Subreddits and users are fetched concurrently. Comments are sorted by score, limited to the configured count, and exclude moderator-distinguished comments. Self-text is truncated at 1500 chars, comments at 500 chars.

**Config** (`sources.reddit`):

```json
{
  "enabled": true,
  "fetch_comments": 5,
  "subreddits": [
    {
      "subreddit": "MachineLearning",
      "sort": "hot",
      "fetch_limit": 25,
      "min_score": 10
    }
  ],
  "users": [
    {
      "username": "spez",
      "sort": "new",
      "fetch_limit": 10
    }
  ]
}
```

- `sort` — `hot`, `new`, `top`, or `rising` (subreddits); `hot` or `new` (users)
- `time_filter` — for `top`/`rising` sorts: `hour`, `day`, `week`, `month`, `year`, `all`
- `min_score` — minimum post score (subreddits only)

**Rate limiting**: Detects HTTP 429 responses, reads the `Retry-After` header, waits, and retries once. Uses a descriptive `User-Agent` as required by Reddit's API guidelines.

**Extracted data**: title, URL, author, score, upvote ratio, comment count, subreddit, flair, self-text, and top comments.

## Twitter

**File**: `src/scrapers/twitter.py`

Uses the [Apify](https://apify.com) platform to bypass Twitter's anti-scraping measures. The actor `altimis~scweet` is called via the Apify REST API.

Flow:
1. POST to `/v2/acts/{actor_id}/runs` to trigger a run
2. Poll `/v2/actor-runs/{run_id}` until status is `SUCCEEDED` or a terminal failure
3. GET `/v2/datasets/{dataset_id}/items` to retrieve results

**Config** (`sources.twitter`):

```json
{
  "enabled": true,
  "users": ["karpathy", "ylecun"],
  "fetch_limit": 10,
  "fetch_reply_text": false,
  "max_replies_per_tweet": 3,
  "max_tweets_to_expand": 10,
  "reply_min_likes": 5,
  "actor_id": "altimis~scweet",
  "apify_token_env": "APIFY_TOKEN"
}
```

- `users` — Twitter screen names to monitor, without the `@` prefix
- `fetch_limit` — maximum tweets to fetch per run
- `fetch_reply_text` — when `true`, a second Apify run fetches reply bodies for each important tweet and appends them under `--- Top Comments ---` for AI analysis
- `max_replies_per_tweet` — maximum reply lines per tweet (sorted by engagement score)
- `max_tweets_to_expand` — cap on reply expansion runs per pipeline cycle, to control Apify credit usage
- `reply_min_likes` — minimum likes required for a reply to be included
- `actor_id` — Apify actor ID (default: `altimis~scweet`)
- `apify_token_env` — environment variable name containing the Apify API token

**Authentication**: Set `APIFY_TOKEN` in your `.env`. Get a token at [console.apify.com](https://console.apify.com/account/integrations).

**Extracted data**: tweet text, URL, author, publish time, likes, retweets, replies, views, and (optionally) reply-thread text appended under `--- Top Comments ---`.

## linux.do

**File**: `src/scrapers/linuxdo.py`

Uses the public Discourse JSON API exposed by [linux.do](https://linux.do):

- `GET /latest.json` — most recently active topics
- `GET /top.json?period={daily|weekly|monthly|yearly|all}` — top topics in a period
- `GET /c/{slug}/{id}.json` — topics within a specific category
- `GET /t/{topic_id}.json` — topic detail with `post_stream.posts` (OP + replies)

Each configured feed is fetched concurrently. Topics are filtered by `created_at` against the orchestrator time window and by `like_count >= min_likes`. For each surviving topic the detail endpoint is fetched (with bounded concurrency) so the OP body and top replies can be included for AI analysis. Replies are sorted by reaction/score, the top N are kept, HTML is stripped, OP text is truncated at 1500 chars and replies at 500 chars.

**Config** (`sources.linuxdo`):

```json
{
  "enabled": true,
  "base_url": "https://linux.do",
  "fetch_comments": 5,
  "feeds": [
    {
      "name": "latest",
      "feed": "latest",
      "fetch_limit": 25,
      "min_likes": 0
    },
    {
      "name": "top-daily",
      "feed": "top",
      "period": "daily",
      "fetch_limit": 25,
      "min_likes": 5
    },
    {
      "name": "develop",
      "feed": "category",
      "category_slug": "develop",
      "category_id": 4,
      "fetch_limit": 25
    }
  ]
}
```

- `feed` — `latest`, `top`, or `category`
- `period` — only used when `feed` is `top`
- `category_slug` + `category_id` — both required when `feed` is `category` (find the ID at `https://linux.do/categories.json`)
- `fetch_comments` — number of top replies per topic; set to `0` to skip detail fetches and reply expansion
- `min_likes` — minimum topic `like_count` to include
- `base_url` — override if you are aggregating a different self-hosted Discourse instance

**Rate limiting**: Detects HTTP 429 responses, reads the `Retry-After` header, waits, and retries once. A browser-like `User-Agent` and `Referer` are sent because Discourse blocks requests with the default `httpx` UA.

**Extracted data**: title, topic URL, OP author, created time, likes, views, reply count, posts count, category id, tags, OP body, and top replies.

## onehack.st

**File**: `src/scrapers/onehack.py`

Thin subclass of the linux.do scraper since [onehack.st](https://onehack.st/) also runs Discourse. Same fetch/parse logic, same feed config shape (reuses `LinuxDoFeedConfig`), same Cloudflare TLS-fingerprint workaround via `primp`.

**Config** (`sources.onehack`): identical to `sources.linuxdo`, with `base_url` defaulted to `https://onehack.st`. See the linux.do section above for `feeds`, `fetch_comments`, `min_likes`, etc.

```json
"onehack": {
  "enabled": true,
  "base_url": "https://onehack.st",
  "fetch_comments": 5,
  "feeds": [
    { "name": "latest", "feed": "latest", "fetch_limit": 25 },
    { "name": "top-daily", "feed": "top", "period": "daily", "min_likes": 5 }
  ]
}
```
