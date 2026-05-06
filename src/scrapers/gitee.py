"""Gitee scraper implementation.

Gitee (gitee.com) exposes a REST API at ``https://gitee.com/api/v5`` whose
shape closely mirrors GitHub's v3 API. This scraper supports two source
types: ``user_events`` (a user's public activity feed) and
``repo_releases`` (releases for a specific repo).
"""

import logging
import os
from datetime import datetime
from typing import List, Optional
import httpx

from .base import BaseScraper
from ..models import ContentItem, SourceType, GiteeSourceConfig

logger = logging.getLogger(__name__)


class GiteeScraper(BaseScraper):
    """Scraper for Gitee events and releases."""

    def __init__(self, sources: List[GiteeSourceConfig], http_client: httpx.AsyncClient):
        """Initialize Gitee scraper.

        Args:
            sources: List of Gitee source configurations
            http_client: Shared async HTTP client
        """
        super().__init__({"sources": sources}, http_client)
        self.token = os.getenv("GITEE_TOKEN")
        self.base_url = "https://gitee.com/api/v5"

    def _get_headers(self) -> dict:
        """Get request headers."""
        return {
            "Accept": "application/json",
            "User-Agent": "Horizon-Aggregator",
        }

    def _auth_params(self) -> dict:
        """Return query params with optional access token."""
        return {"access_token": self.token} if self.token else {}

    async def fetch(self, since: datetime) -> List[ContentItem]:
        """Fetch Gitee content items.

        Args:
            since: Only fetch items published after this time

        Returns:
            List[ContentItem]: Fetched content items
        """
        items: List[ContentItem] = []
        sources = self.config["sources"]

        for source in sources:
            if not source.enabled:
                continue

            if source.type == "user_events" and source.username:
                items.extend(await self._fetch_user_events(source.username, since))
            elif source.type == "repo_releases" and source.owner and source.repo:
                items.extend(
                    await self._fetch_repo_releases(source.owner, source.repo, since)
                )

        return items

    async def _fetch_user_events(
        self,
        username: str,
        since: datetime,
    ) -> List[ContentItem]:
        """Fetch public events for a Gitee user."""
        url = f"{self.base_url}/users/{username}/events/public"
        items: List[ContentItem] = []

        try:
            response = await self.client.get(
                url,
                params=self._auth_params(),
                headers=self._get_headers(),
                follow_redirects=True,
            )
            response.raise_for_status()
            events = response.json()

            for event in events:
                created_raw = event.get("created_at")
                if not created_raw:
                    continue
                created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))

                if created_at < since:
                    continue

                event_type = event.get("type")
                if event_type not in [
                    "PushEvent", "CreateEvent", "ReleaseEvent",
                    "PublicEvent", "StarEvent", "WatchEvent",
                    "PullRequestEvent", "IssueEvent",
                ]:
                    continue

                item = self._parse_event(event, username)
                if item:
                    items.append(item)

        except httpx.HTTPError as e:
            logger.warning("Error fetching Gitee events for %s: %s", username, e)

        return items

    def _parse_event(self, event: dict, username: str) -> Optional[ContentItem]:
        """Parse Gitee event into ContentItem."""
        event_type = event.get("type")
        event_id = str(event.get("id"))
        created_at = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))

        repo_obj = event.get("repo") or {}
        repo_name = repo_obj.get("full_name") or repo_obj.get("name") or ""
        repo_url = f"https://gitee.com/{repo_name}" if repo_name else "https://gitee.com"

        payload = event.get("payload") or {}

        if event_type == "PushEvent":
            commits = payload.get("commits", []) or []
            title = f"{username} pushed {len(commits)} commit(s) to {repo_name}"
            content = "\n".join([c.get("message", "") for c in commits[:3]])
        elif event_type == "CreateEvent":
            ref_type = payload.get("ref_type", "repository")
            title = f"{username} created {ref_type} in {repo_name}"
            content = payload.get("description", "") or ""
        elif event_type == "ReleaseEvent":
            release = payload.get("release", {}) or {}
            tag = release.get("tag_name", "")
            title = f"{username} released {tag} in {repo_name}"
            content = release.get("body", "") or ""
            repo_url = release.get("html_url") or repo_url
        elif event_type == "PublicEvent":
            title = f"{username} made {repo_name} public"
            content = ""
        elif event_type in ("StarEvent", "WatchEvent"):
            title = f"{username} starred {repo_name}"
            content = ""
        elif event_type == "PullRequestEvent":
            pr = payload.get("pull_request", {}) or {}
            action = payload.get("action", "updated")
            title = f"{username} {action} PR #{pr.get('number', '')} in {repo_name}: {pr.get('title', '')}"
            content = pr.get("body", "") or ""
            repo_url = pr.get("html_url") or repo_url
        elif event_type == "IssueEvent":
            issue = payload.get("issue", {}) or {}
            action = payload.get("action", "updated")
            title = f"{username} {action} issue in {repo_name}: {issue.get('title', '')}"
            content = issue.get("body", "") or ""
            repo_url = issue.get("html_url") or repo_url
        else:
            return None

        return ContentItem(
            id=self._generate_id("gitee", "event", event_id),
            source_type=SourceType.GITEE,
            title=title,
            url=repo_url,
            content=content,
            author=username,
            published_at=created_at,
            metadata={
                "event_type": event_type,
                "repo": repo_name,
            },
        )

    async def _fetch_repo_releases(
        self,
        owner: str,
        repo: str,
        since: datetime,
    ) -> List[ContentItem]:
        """Fetch releases for a Gitee repository."""
        url = f"{self.base_url}/repos/{owner}/{repo}/releases"
        items: List[ContentItem] = []

        try:
            response = await self.client.get(
                url,
                params=self._auth_params(),
                headers=self._get_headers(),
                follow_redirects=True,
            )
            response.raise_for_status()
            releases = response.json()

            for release in releases:
                published_raw = release.get("created_at") or release.get("published_at")
                if not published_raw:
                    continue
                published_at = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))

                if published_at < since:
                    continue

                tag = release.get("tag_name", "")
                html_url = (
                    f"https://gitee.com/{owner}/{repo}/releases/tag/{tag}"
                    if tag
                    else f"https://gitee.com/{owner}/{repo}/releases"
                )

                author_obj = release.get("author") or {}
                author = author_obj.get("login") or author_obj.get("name") or owner

                item = ContentItem(
                    id=self._generate_id("gitee", "release", str(release.get("id"))),
                    source_type=SourceType.GITEE,
                    title=f"{owner}/{repo} released {tag}",
                    url=html_url,
                    content=release.get("body", "") or "",
                    author=author,
                    published_at=published_at,
                    metadata={
                        "repo": f"{owner}/{repo}",
                        "tag": tag,
                        "prerelease": release.get("prerelease", False),
                    },
                )
                items.append(item)

        except httpx.HTTPError as e:
            logger.warning("Error fetching Gitee releases for %s/%s: %s", owner, repo, e)

        return items
