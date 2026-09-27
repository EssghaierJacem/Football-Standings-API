"""Static configuration: tracked leagues, scrape interval, DB path.

Values that are safe to tune per-deployment (interval, request pacing, DB
path) can be overridden via environment variables — see ``.env.example``.
``TRACKED_LEAGUES`` stays in code rather than the environment because it's
a structured list, not a scalar; leagues can also be added at runtime via
``POST /leagues``, which persists them to the database independently of
this file.

Add a new tracked league by appending a ``(country_slug, league_slug)``
tuple to ``TRACKED_LEAGUES`` below — no other code changes are required, as
long as Soccerway's URL scheme
(``/{country}/{league}/standings/{stage_id}/standings/{variant}/``) holds
for that country. See the README's "Adding a new league" section for the
runtime (``POST /leagues``) alternative.
"""

import os
from pathlib import Path

TRACKED_LEAGUES: list[tuple[str, str]] = [
    ("tunisia", "ligue-2"),       # baseline: all-zero, round 1, single promotion/relegation zone
    ("england", "premier-league"),  # mid-season: real W/D/L/points, no playoff zone
    ("england", "championship"),    # promotion + playoff + relegation zones (3 distinct colors)
]

# Respectful polling: Soccerway is a free public site with no scraping API,
# so we deliberately poll slowly rather than as fast as possible. Standings
# only change when a match finishes, so anything faster than ~30 min buys
# no fresher data most of the day, it just adds load. 30-60 min is the
# recommended range; default 45. Override with SCRAPE_INTERVAL_MINUTES.
SCRAPE_INTERVAL_MINUTES = int(os.environ.get("SCRAPE_INTERVAL_MINUTES", "45"))

# Minimum delay between consecutive requests to soccerway.com, regardless of
# league, so a run over many tracked leagues doesn't fire back-to-back
# requests in a burst. Applied by scheduler.run_all_tracked between leagues.
#
# Concurrency assumption: the scheduler scrapes leagues SEQUENTIALLY, one
# Playwright browser instance at a time — never in parallel. There is no
# known documented rate limit for Soccerway; this project assumes there
# isn't a generous one and stays conservative rather than testing the
# limit. If you add concurrent scraping later, this delay must become a
# shared rate limiter across workers, not a per-worker sleep.
MIN_REQUEST_DELAY_SECONDS = int(os.environ.get("MIN_REQUEST_DELAY_SECONDS", "5"))

DB_PATH = Path(os.environ.get("SOCCERWAY_DB_PATH", str(Path(__file__).resolve().parent.parent / "soccerway.db")))
