# Football-Standings-API

A generic, multi-league Soccerway.com standings scraper with SQLite storage,
a polling scheduler, and a FastAPI serving layer. Works for any
country/league Soccerway supports — not just one hardcoded competition.

## How it works

- **Discovery** (`soccerway_scraper/scraper.py::discover_stage_id`): each
  league's main page (`/{country}/{league}/`) embeds a `window.environment`
  JSON blob in the raw HTML containing the current season's
  `stats2_config.tournament` / `tournamentStage` ids. These are discovered
  with a plain `httpx` GET + regex — no browser needed — since the stage id
  changes every season and can't be hardcoded.
- **Scraping** (`scraper.py::scrape_standings`): the standings table itself
  is rendered client-side by Soccerway's JS app, so a headless Chromium
  browser (Playwright) loads
  `/{country}/{league}/standings/{stage_id}/standings/overall/`, waits for
  the table rows to render, dismisses the cookie-consent overlay if present,
  and extracts position, zone (promotion/relegation), team name/id/logo,
  played/won/drawn/lost, goals for/against, goal difference, points, and the
  last-5-matches form badges (result, letter, color class, match URL).
- **Storage** (`storage.py`): SQLite, upserted per
  `(country, league, season, team)` so re-scraping updates existing rows
  instead of duplicating them. A failed or empty scrape is never written —
  the last good data stays in place.
- **Scheduler** (`scheduler.py`): re-scrapes every tracked league on an
  interval (default 45 min). Logs loudly and skips the run on failure
  instead of overwriting good data.
- **API** (`api.py`): FastAPI endpoints to list tracked leagues, read stored
  standings, look up a single team, and add a new league to track.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Running

**One-off scrape (no DB/API), for testing:**

```bash
python -m soccerway_scraper.scraper
```

**Scheduler (scrapes all tracked leagues on a loop):**

```bash
python -m soccerway_scraper.scheduler
```

**API server:**

```bash
uvicorn soccerway_scraper.api:app --reload
```

Note: the API serves whatever is already in the database. Run the
scheduler (or `POST /leagues`, which triggers an immediate scrape) to
populate it.

## API

- `GET /leagues` — list currently tracked `(country, league)` pairs.
- `GET /standings?country=tunisia&league=ligue-2` — stored standings table
  for a league (optionally `&season=2026/2027`; defaults to the most
  recently scraped season).
- `GET /standings/{country}/{league}/teams/{team_name}` — a single team's
  row, including its form.
- `POST /leagues` with `{"country": "...", "league": "..."}` — start
  tracking a new league; triggers an immediate discovery + scrape.

## Adding a new league

No code changes needed — either add the pair to `TRACKED_LEAGUES` in
`config.py`, or `POST /leagues` at runtime. Soccerway's URL scheme
(`/{country-slug}/{league-slug}/...`) is consistent across countries; if you
hit a country where it differs, that's an exception to flag, not the norm.

## Being respectful to Soccerway

- Default scrape interval is 45 minutes per league (configurable in
  `config.py`, `SCRAPE_INTERVAL_MINUTES`).
- A small delay (`MIN_REQUEST_DELAY_SECONDS`) is inserted between leagues
  within a single scheduler run so requests aren't fired in a burst.
- Requests use a realistic browser user-agent, not a bot-identifying one.
- This project is for personal, non-commercial use. Don't lower the
  interval to poll aggressively.

## Project structure

```
soccerway_scraper/
  scraper.py      # discover_stage_id, scrape_standings
  storage.py      # SQLite schema + upsert/read logic
  scheduler.py    # interval runner over tracked leagues
  api.py          # FastAPI app
  config.py       # tracked leagues list, interval, db path
requirements.txt
README.md
```
