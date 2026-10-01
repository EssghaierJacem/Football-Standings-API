"""SQLite storage for team fixtures (kept apart from the standings storage)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config
from .storage import get_connection

# A match that kicked off less than this long ago still counts as "the next match" while it is on.
LIVE_WINDOW = timedelta(hours=2)

COLUMNS = (
    "match_id", "match_url", "status", "kickoff", "date_raw", "round", "competition", "country",
    "team_side", "opponent_id", "home_team", "home_logo_url", "away_team", "away_logo_url",
    "home_score", "away_score",
)


def init_fixtures_db(db_path: Path | str = config.DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS team_fixtures (
                team_id TEXT NOT NULL,
                match_id TEXT NOT NULL,
                match_url TEXT,
                status TEXT NOT NULL,
                kickoff TEXT,
                date_raw TEXT,
                round INTEGER,
                competition TEXT,
                country TEXT,
                team_side TEXT,
                opponent_id TEXT,
                home_team TEXT,
                home_logo_url TEXT,
                away_team TEXT,
                away_logo_url TEXT,
                home_score INTEGER,
                away_score INTEGER,
                scraped_at TEXT NOT NULL,
                PRIMARY KEY (team_id, match_id)
            )
            """
        )


def upsert_fixtures(result: dict[str, Any], db_path: Path | str = config.DB_PATH) -> int:
    """Store a scrape_team_fixtures() result and drop fixtures that are no longer listed."""
    meta, fixtures = result["meta"], result["fixtures"]
    if not fixtures:
        raise ValueError("Refusing to store an empty fixtures list")

    placeholders = ", ".join(f":{c}" for c in COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in COLUMNS if c != "match_id")
    with get_connection(db_path) as conn:
        for fixture in fixtures:
            conn.execute(
                f"""
                INSERT INTO team_fixtures (team_id, {", ".join(COLUMNS)}, scraped_at)
                VALUES (:team_id, {placeholders}, :scraped_at)
                ON CONFLICT (team_id, match_id) DO UPDATE SET {updates}, scraped_at = excluded.scraped_at
                """,
                {**{c: fixture.get(c) for c in COLUMNS}, "team_id": meta["team_id"], "scraped_at": meta["scraped_at"]},
            )
        conn.execute(
            "DELETE FROM team_fixtures WHERE team_id = ? AND scraped_at <> ?",
            (meta["team_id"], meta["scraped_at"]),
        )
    return len(fixtures)


def list_fixtures(team_id: str, db_path: Path | str = config.DB_PATH) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM team_fixtures WHERE team_id = ? ORDER BY COALESCE(kickoff, '9999') ASC, match_id ASC",
            (team_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_next_match(team_id: str, db_path: Path | str = config.DB_PATH) -> dict[str, Any] | None:
    """The first match that has not finished, or that kicked off within the live window."""
    cutoff = datetime.now(timezone.utc) - LIVE_WINDOW
    for fixture in list_fixtures(team_id, db_path):
        if fixture["status"] == "finished":
            continue
        kickoff = fixture["kickoff"]
        if kickoff is None or datetime.fromisoformat(kickoff) >= cutoff:
            return fixture
    return None
