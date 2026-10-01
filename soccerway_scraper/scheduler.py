"""Interval runner that keeps tracked leagues' standings up to date.

Failures (network errors, 0 rows, missing selectors, etc.) are logged loudly
and that league's run is skipped — the last good data in storage.py is never
overwritten with an empty or partial result.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import config, storage
from .scraper import ScrapeError, scrape_standings

logger = logging.getLogger(__name__)


def _run_in_own_loop(coro_factory):
    """Run a coroutine on a fresh event loop in this (worker) thread.

    Playwright needs subprocess support, which Windows' SelectorEventLoop (what
    ``uvicorn --reload`` uses) lacks -> NotImplementedError. A dedicated Proactor
    loop on a worker thread works regardless of how the host server was started.
    """
    loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_factory())
    finally:
        loop.close()


async def scrape_one(country: str, league: str) -> bool:
    """Scrape and store a single league. Returns True on success."""
    try:
        result = await asyncio.to_thread(_run_in_own_loop, lambda: scrape_standings(country, league))
        storage.upsert_standings(result)
        logger.info(
            "Scraped %s/%s: %d teams (season %s)",
            country,
            league,
            len(result["standings"]),
            result["meta"].get("season"),
        )
        return True
    except ScrapeError as exc:
        logger.error("Scrape FAILED for %s/%s, skipping this run: %s", country, league, exc)
        return False
    except Exception:  # noqa: BLE001 - never let one league's crash kill the loop
        logger.exception("Unexpected error scraping %s/%s, skipping this run", country, league)
        return False


async def run_all_tracked() -> None:
    leagues = storage.list_tracked_leagues()
    if not leagues:
        logger.warning("No tracked leagues configured; nothing to scrape")
        return

    for entry in leagues:
        await scrape_one(entry["country"], entry["league"])
        # Respectful pacing: never fire requests back-to-back across leagues.
        await asyncio.sleep(config.MIN_REQUEST_DELAY_SECONDS)


def build_scheduler(interval_minutes: int = config.SCRAPE_INTERVAL_MINUTES) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_all_tracked,
        "interval",
        minutes=interval_minutes,
        next_run_time=None,  # first run is triggered explicitly by the caller
        id="scrape_all_tracked_leagues",
        max_instances=1,
        coalesce=True,
    )
    return scheduler


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    storage.init_db()

    scheduler = build_scheduler()
    scheduler.start()

    logger.info("Running initial scrape of all tracked leagues...")
    await run_all_tracked()

    logger.info(
        "Scheduler started, will re-scrape every %d minutes", config.SCRAPE_INTERVAL_MINUTES
    )
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
