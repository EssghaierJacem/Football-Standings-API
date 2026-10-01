"""Discovery + scraping logic for Soccerway league standings tables.

Soccerway pages embed a ``window.environment = {...}`` JSON blob in the raw
HTML which carries the current season's ``tournament`` id and
``tournamentStage`` id (under ``stats2_config``). That blob is present in the
plain HTTP response, so :func:`discover_stage_id` uses a lightweight
``httpx`` GET and a regex extraction instead of a full JSON parse of the
(very large) environment object.

The actual standings table is rendered client-side by a JS single-page app,
so :func:`scrape_standings` drives a headless Chromium browser via
Playwright to read it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from playwright.async_api import Page, async_playwright

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

BASE_URL = "https://www.soccerway.com"

# Matches e.g. `"stats2_config":{"tournament":"UewsOUqm","tournamentStage":"SAB2tfQ1"`
STATS2_CONFIG_RE = re.compile(
    r'"stats2_config"\s*:\s*\{\s*"tournament"\s*:\s*"(?P<tournament_id>[^"]+)"'
    r'\s*,\s*"tournamentStage"\s*:\s*"(?P<stage_id>[^"]+)"'
)

# Matches the season out of the page <title>, e.g. "Ligue 2 2026/2027 table, ..."
TITLE_SEASON_RE = re.compile(r"<title>.*?(\d{4}/\d{4}).*?</title>", re.IGNORECASE | re.DOTALL)

TABLE_SELECTOR = ".ui-table"
ROW_SELECTOR = ".ui-table__body .ui-table__row"
CONSENT_BUTTON_SELECTOR = "#onetrust-accept-btn-handler"


class ScrapeError(Exception):
    """Raised when discovery or scraping fails in a way callers must handle explicitly."""


@dataclass
class StageInfo:
    tournament_id: str
    stage_id: str
    season: str | None


async def discover_stage_id(country_slug: str, league_slug: str) -> StageInfo:
    """Fetch the league's main page and extract the current tournament/stage ids.

    This is a plain HTTP GET (no browser needed) because the ``window.environment``
    blob containing these ids is present in the raw server-rendered HTML.
    """
    url = f"{BASE_URL}/{country_slug}/{league_slug}/"
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30.0) as client:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
    html = response.text

    match = STATS2_CONFIG_RE.search(html)
    if not match:
        raise ScrapeError(
            f"Could not find stats2_config (tournament/stage ids) on {url}. "
            "Soccerway may have changed its page structure."
        )

    season_match = TITLE_SEASON_RE.search(html)
    season = season_match.group(1) if season_match else None

    return StageInfo(
        tournament_id=match.group("tournament_id"),
        stage_id=match.group("stage_id"),
        season=season,
    )


async def _dismiss_consent_overlay(page: Page) -> None:
    try:
        button = page.locator(CONSENT_BUTTON_SELECTOR)
        if await button.count() > 0:
            await button.first.click(timeout=3000)
            logger.debug("Dismissed cookie consent overlay")
    except Exception:  # noqa: BLE001 - overlay is best-effort, never block the scrape
        logger.debug("No consent overlay to dismiss (or dismissal failed harmlessly)")


async def _extract_rows(page: Page) -> list[dict[str, Any]]:
    """Extract standings rows from the already-rendered page via a single JS pass."""
    return await page.eval_on_selector_all(
        TABLE_SELECTOR,
        """
        (tables) => tables.flatMap((table) => {
          // Leagues split into groups render one table per group; the group name
          // ("Group A") is the title of the participant header cell. Single-table
          // leagues use a generic header, which we treat as "no group".
          const headerTitle = (table.querySelector('.table__headerCell--participant') || {}).title || '';
          const group = /group|groupe|zone|pool/i.test(headerTitle) ? headerTitle.trim() : null;
          return Array.from(table.querySelectorAll('.ui-table__body .ui-table__row')).map((row) => {
            const rankEl = row.querySelector('.tableCellRank');
            const nameEl = row.querySelector('.tableCellParticipant__name');
            const imgEl = row.querySelector('.tableCellParticipant__image img');
            const valueEls = Array.from(row.querySelectorAll('.table__cell--value'));

            const formIcons = Array.from(row.querySelectorAll('.tableCellFormIcon')).map((icon) => {
                const badge = icon.querySelector('[data-testid^="wcl-badgeForm-"]');
                const testId = badge ? badge.getAttribute('data-testid') : null;
                const result = testId ? testId.replace('wcl-badgeForm-', '') : null;
                const letterEl = icon.querySelector('[data-testid="wcl-simple-text-01"]');
                const colorClass = badge
                    ? Array.from(badge.classList).find((c) => c !== 'wcl-badgeform_AKaAR') || null
                    : null;
                return {
                    result: result,
                    letter: letterEl ? letterEl.textContent.trim() : null,
                    color_class: colorClass,
                    match_url: icon.getAttribute('href'),
                };
            });

            const score = valueEls[4] ? valueEls[4].textContent.trim() : null;
            let goalsFor = null, goalsAgainst = null;
            if (score && score.includes(':')) {
                const parts = score.split(':');
                goalsFor = parseInt(parts[0], 10);
                goalsAgainst = parseInt(parts[1], 10);
            }

            return {
                group: group,
                position_raw: rankEl ? rankEl.textContent.trim() : null,
                zone_title: rankEl ? rankEl.getAttribute('title') : null,
                team_name: nameEl ? nameEl.textContent.trim() : null,
                team_url: nameEl ? nameEl.getAttribute('href') : null,
                team_logo_url: imgEl ? imgEl.getAttribute('src') : null,
                played: valueEls[0] ? valueEls[0].textContent.trim() : null,
                won: valueEls[1] ? valueEls[1].textContent.trim() : null,
                drawn: valueEls[2] ? valueEls[2].textContent.trim() : null,
                lost: valueEls[3] ? valueEls[3].textContent.trim() : null,
                goals_for: goalsFor,
                goals_against: goalsAgainst,
                goal_difference: valueEls[5] ? valueEls[5].textContent.trim() : null,
                points: valueEls[6] ? valueEls[6].textContent.trim() : null,
                form: formIcons,
            };
          });
        })
        """,
    )


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip().rstrip("."))
    except ValueError:
        return None


def _clean_row(raw: dict[str, Any]) -> dict[str, Any]:
    team_id = None
    if raw.get("team_url"):
        # team URLs look like /team/soliman/n5jl3GmP/
        parts = [p for p in raw["team_url"].split("/") if p]
        if parts:
            team_id = parts[-1]

    return {
        "group": raw.get("group"),
        "position": _to_int(raw.get("position_raw")),
        "zone": raw.get("zone_title"),
        "team_name": raw.get("team_name"),
        "team_id": team_id,
        "team_url": raw.get("team_url"),
        "team_logo_url": raw.get("team_logo_url"),
        "played": _to_int(raw.get("played")),
        "won": _to_int(raw.get("won")),
        "drawn": _to_int(raw.get("drawn")),
        "lost": _to_int(raw.get("lost")),
        "goals_for": raw.get("goals_for"),
        "goals_against": raw.get("goals_against"),
        "goal_difference": _to_int(raw.get("goal_difference")),
        "points": _to_int(raw.get("points")),
        "form": [
            {
                "result": icon.get("result"),
                "letter": icon.get("letter"),
                "color_class": icon.get("color_class"),
                "match_url": icon.get("match_url"),
            }
            for icon in raw.get("form", [])
        ],
    }


async def scrape_standings(
    country_slug: str,
    league_slug: str,
    stage_id: str | None = None,
    variant: str = "overall",
) -> dict[str, Any]:
    """Scrape the standings table for a league via a headless browser.

    Returns a dict with ``meta`` (country, league, stage_id, season, scraped_at)
    and ``standings`` (list of per-team dicts).
    """
    season = None
    tournament_id = None
    if stage_id is None:
        info = await discover_stage_id(country_slug, league_slug)
        stage_id = info.stage_id
        tournament_id = info.tournament_id
        season = info.season

    url = f"{BASE_URL}/{country_slug}/{league_slug}/standings/{stage_id}/standings/{variant}/"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent=USER_AGENT)
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await _dismiss_consent_overlay(page)

            try:
                await page.wait_for_selector(ROW_SELECTOR, timeout=20000)
            except Exception as exc:  # noqa: BLE001
                raise ScrapeError(
                    f"Standings table did not render for {url} within timeout: {exc}"
                ) from exc

            raw_rows = await _extract_rows(page)
        finally:
            await browser.close()

    if not raw_rows:
        raise ScrapeError(
            f"Scrape of {url} returned 0 rows. Soccerway's page structure may have "
            "changed, or the table failed to render."
        )

    standings = [_clean_row(row) for row in raw_rows]

    return {
        "meta": {
            "country": country_slug,
            "league": league_slug,
            "stage_id": stage_id,
            "tournament_id": tournament_id,
            "season": season,
            "variant": variant,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "url": url,
        },
        "standings": standings,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def _main() -> None:
        result = await scrape_standings("tunisia", "ligue-2")
        import json

        print(json.dumps(result, indent=2, ensure_ascii=False))

    asyncio.run(_main())
