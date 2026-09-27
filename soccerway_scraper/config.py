"""Static configuration: tracked leagues, scrape interval, DB path.

Add a new league by appending a ``(country_slug, league_slug)`` tuple to
``TRACKED_LEAGUES`` — no code changes required, as long as Soccerway's URL
scheme (``/{country}/{league}/standings/{stage_id}/standings/{variant}/``)
holds for that country.
"""

from pathlib import Path

TRACKED_LEAGUES: list[tuple[str, str]] = [
    ("tunisia", "ligue-2"),
]

# Be respectful: don't hammer the site. 30-60 min is the recommended range.
SCRAPE_INTERVAL_MINUTES = 45

DB_PATH = Path(__file__).resolve().parent.parent / "soccerway.db"

# Minimum delay between consecutive requests to soccerway.com, regardless of
# league, to avoid bursts when several leagues are tracked.
MIN_REQUEST_DELAY_SECONDS = 5
