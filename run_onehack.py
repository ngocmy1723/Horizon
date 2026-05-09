import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

from src.scrapers.onehack import OneHackScraper
from src.storage.manager import StorageManager


async def main(hours: int):
    storage = StorageManager(data_dir=str(Path("data")))
    config = storage.load_config()

    cfg = config.sources.onehack
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    print(f"Fetching topics since: {since} ({hours}h window)")

    async with httpx.AsyncClient() as client:
        scraper = OneHackScraper(cfg, client)
        items = await scraper.fetch(since)
        print(f"Found {len(items)} items")
        for item in items:
            print(item.id, item.title)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hours", type=int, default=24, help="Look back window in hours"
    )
    args = parser.parse_args()
    asyncio.run(main(args.hours))
