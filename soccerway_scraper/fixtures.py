"""Scrape a team's upcoming fixtures from its Soccerway fixtures page.

Example page: https://www.soccerway.com/team/gafsa/pz9NlZbR/fixtures/

The fixtures list shows the teams, their logos and a date without a year
("04.10. 15:00"). The match page gives the full date and the round, so it is
opened once, for the next match only, to keep the number of requests low.
Soccerway does not publish the venue, so it is left out instead of guessed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from playwright.async_api import Page, async_playwright

from .scraper import (
    BASE_URL,
    USER_AGENT,
    ScrapeError,
    _dismiss_consent_overlay,
)

logger = logging.getLogger(__name__)

# Tunisia has no daylight saving time, so one fixed offset is always right.
TUNIS_TZ = timezone(timedelta(hours=1), "Africa/Tunis")
TUNIS_TZ_NAME = "Africa/Tunis"

MATCH_ROW_SELECTOR = ".event__match"

FULL_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})")
SHORT_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.\s*(\d{2}):(\d{2})")
ROUND_RE = re.compile(r"ROUND\s*(\d+)", re.IGNORECASE)
# /match/gafsa-pz9NlZbR/korba-MPZ3rysm/?mid=0OXsMGMs
MATCH_TEAMS_RE = re.compile(r"/match/([^/]+)-([A-Za-z0-9]{8})/([^/]+)-([A-Za-z0-9]{8})/")


async def _extract_rows(page: Page) -> list[dict[str, Any]]:
    """Read every match row, remembering which competition header it sits under."""
    return await page.evaluate(
        """
        () => {
          const rows = [];
          document.querySelectorAll('.leagues--static').forEach((container) => {
            let competition = null;
            let country = null;
            Array.from(container.children).forEach((child) => {
              if (child.classList.contains('headerLeague__wrapper')) {
                const title = child.querySelector('.headerLeague__title-text, .headerLeague__title');
                const category = child.querySelector('.headerLeague__category-text');
                competition = title ? title.textContent.trim() : null;
                country = category ? category.textContent.trim() : null;
                return;
              }
              if (!child.classList.contains('event__match')) return;
              const link = child.querySelector('a.eventRowLink');
              const side = (el) => {
                if (!el) return { name: null, logo: null };
                const img = el.querySelector('img');
                const label = el.querySelector('[data-testid="wcl-simple-text-01"]');
                return {
                  name: label ? label.textContent.trim() : (img ? img.getAttribute('alt') : null),
                  logo: img ? img.getAttribute('src') : null,
                };
              };
              const home = side(child.querySelector('.event__homeParticipant'));
              const away = side(child.querySelector('.event__awayParticipant'));
              const time = child.querySelector('.event__stageTime');
              const homeScore = child.querySelector('.event__score--home');
              const awayScore = child.querySelector('.event__score--away');
              rows.push({
                match_id: (child.id || '').replace('g_1_', '') || null,
                match_url: link ? link.getAttribute('href') : null,
                classes: child.className,
                date_raw: time ? time.textContent.trim() : null,
                competition: competition,
                country: country,
                home: home,
                away: away,
                home_score: homeScore ? homeScore.textContent.trim() : null,
                away_score: awayScore ? awayScore.textContent.trim() : null,
              });
            });
          });
          return rows;
        }
        """
    )


def _status(classes: str) -> str:
    if "scheduled" in classes:
        return "scheduled"
    if "live" in classes:
        return "live"
    return "finished"


def _score(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None  # "-" for matches that have not been played


def _normalize(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _team_side(team_slug: str, home_name: str | None, away_name: str | None) -> str | None:
    """Which side the tracked team plays on. The match URL lists teams alphabetically, so it
    cannot be used for this; the slug ("gafsa") is contained in the display name ("EGS Gafsa")."""
    slug = _normalize(team_slug)
    in_home = bool(slug) and slug in _normalize(home_name)
    in_away = bool(slug) and slug in _normalize(away_name)
    if in_home == in_away:
        return None
    return "home" if in_home else "away"


def _opponent_id(match_url: str | None, team_id: str) -> str | None:
    match = MATCH_TEAMS_RE.search(match_url or "")
    ids = [match.group(2), match.group(4)] if match else []
    others = [i for i in ids if i != team_id]
    return others[0] if len(others) == 1 else None


def _kickoff_from_short(date_raw: str | None, now: datetime) -> datetime | None:
    """Turn '04.10. 15:00' into a datetime by picking the year that makes it upcoming-ish."""
    match = SHORT_DATE_RE.search(date_raw or "")
    if not match:
        return None
    day, month, hour, minute = (int(part) for part in match.groups())
    try:
        candidate = datetime(now.year, month, day, hour, minute, tzinfo=TUNIS_TZ)
    except ValueError:
        return None
    # A fixture is never more than ~half a year in the past, so roll over at new year.
    return candidate.replace(year=now.year + 1) if candidate < now - timedelta(days=183) else candidate


async def _read_match_page(page: Page, match_url: str) -> dict[str, Any]:
    """Open a match page and read the exact kickoff and the round number."""
    await page.goto(match_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)
    text = await page.inner_text("body")
    kickoff = None
    full = FULL_DATE_RE.search(text)
    if full:
        day, month, year, hour, minute = (int(part) for part in full.groups())
        kickoff = datetime(year, month, day, hour, minute, tzinfo=TUNIS_TZ)
    round_match = ROUND_RE.search(text)
    return {"kickoff": kickoff, "round": int(round_match.group(1)) if round_match else None}


async def scrape_team_fixtures(team_slug: str, team_id: str) -> dict[str, Any]:
    """Return the team's fixtures. The first upcoming one is enriched from its match page."""
    url = f"{BASE_URL}/team/{team_slug}/{team_id}/fixtures/"
    now = datetime.now(TUNIS_TZ)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            # Times are shown in the browser's timezone, so pin it to the club's.
            context = await browser.new_context(user_agent=USER_AGENT, timezone_id=TUNIS_TZ_NAME, locale="en-GB")
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await _dismiss_consent_overlay(page)
            try:
                await page.wait_for_selector(MATCH_ROW_SELECTOR, timeout=20000)
            except Exception as exc:  # noqa: BLE001
                raise ScrapeError(f"No fixtures rendered for {url} within timeout: {exc}") from exc

            raw_rows = await _extract_rows(page)
            fixtures: list[dict[str, Any]] = []
            for raw in raw_rows:
                side = _team_side(team_slug, raw["home"]["name"], raw["away"]["name"])
                fixtures.append(
                    {
                        "match_id": raw["match_id"],
                        "match_url": raw["match_url"],
                        "status": _status(raw["classes"]),
                        "kickoff": _kickoff_from_short(raw["date_raw"], now),
                        "date_raw": raw["date_raw"],
                        "round": None,
                        "competition": raw["competition"],
                        "country": raw["country"],
                        "team_side": side,
                        "opponent_id": _opponent_id(raw["match_url"], team_id),
                        "home_team": raw["home"]["name"],
                        "home_logo_url": raw["home"]["logo"],
                        "away_team": raw["away"]["name"],
                        "away_logo_url": raw["away"]["logo"],
                        "home_score": _score(raw["home_score"]),
                        "away_score": _score(raw["away_score"]),
                    }
                )

            upcoming = next((f for f in fixtures if f["status"] != "finished" and f["match_url"]), None)
            if upcoming:
                try:
                    detail = await _read_match_page(page, upcoming["match_url"])
                    upcoming["kickoff"] = detail["kickoff"] or upcoming["kickoff"]
                    upcoming["round"] = detail["round"]
                except Exception:  # noqa: BLE001 - the list data is still usable without the detail
                    logger.warning("Could not read match page %s, using list data only", upcoming["match_url"])
        finally:
            await browser.close()

    if not fixtures:
        raise ScrapeError(f"Scrape of {url} returned 0 fixtures.")

    for fixture in fixtures:
        if fixture["kickoff"]:
            fixture["kickoff"] = fixture["kickoff"].isoformat()

    return {
        "meta": {"team_slug": team_slug, "team_id": team_id, "url": url, "scraped_at": datetime.now(timezone.utc).isoformat()},
        "fixtures": fixtures,
    }


if __name__ == "__main__":
    import asyncio
    import json

    logging.basicConfig(level=logging.INFO)
    print(json.dumps(asyncio.run(scrape_team_fixtures("gafsa", "pz9NlZbR")), indent=2, ensure_ascii=False))
