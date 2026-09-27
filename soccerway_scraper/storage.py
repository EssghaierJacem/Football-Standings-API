"""SQLite storage: tracked leagues + standings, with upsert-on-team-per-season."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config

UNKNOWN_SEASON = "unknown"


def _connect(db_path: Path | str = config.DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_connection(db_path: Path | str = config.DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | str = config.DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_leagues (
                country TEXT NOT NULL,
                league TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (country, league)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS standings (
                country TEXT NOT NULL,
                league TEXT NOT NULL,
                season TEXT NOT NULL,
                team_key TEXT NOT NULL,
                team_id TEXT,
                team_name TEXT NOT NULL,
                position INTEGER,
                zone TEXT,
                team_logo_url TEXT,
                played INTEGER,
                won INTEGER,
                drawn INTEGER,
                lost INTEGER,
                goals_for INTEGER,
                goals_against INTEGER,
                goal_difference INTEGER,
                points INTEGER,
                form_json TEXT,
                raw_json TEXT,
                stage_id TEXT,
                scraped_at TEXT NOT NULL,
                PRIMARY KEY (country, league, season, team_key)
            )
            """
        )
        # Seed tracked_leagues from config, without clobbering existing rows
        # (e.g. leagues added at runtime via POST /leagues) or their added_at.
        for country, league in config.TRACKED_LEAGUES:
            conn.execute(
                """
                INSERT OR IGNORE INTO tracked_leagues (country, league, added_at)
                VALUES (?, ?, ?)
                """,
                (country, league, datetime.now(timezone.utc).isoformat()),
            )


def add_tracked_league(country: str, league: str, db_path: Path | str = config.DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO tracked_leagues (country, league, added_at)
            VALUES (?, ?, ?)
            """,
            (country, league, datetime.now(timezone.utc).isoformat()),
        )


def list_tracked_leagues(db_path: Path | str = config.DB_PATH) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT country, league, added_at FROM tracked_leagues ORDER BY country, league"
        ).fetchall()
        return [dict(row) for row in rows]


def upsert_standings(result: dict[str, Any], db_path: Path | str = config.DB_PATH) -> int:
    """Store a scrape_standings() result. Returns the number of rows upserted.

    Existing rows for the same (country, league, season, team) are updated in
    place rather than duplicated. Never call this with an empty standings
    list — callers should treat that as a failed scrape and skip the write
    entirely so good data is never overwritten with empty data.
    """
    meta = result["meta"]
    standings = result["standings"]
    if not standings:
        raise ValueError("Refusing to store an empty standings list")

    season = meta.get("season") or UNKNOWN_SEASON

    with get_connection(db_path) as conn:
        for row in standings:
            team_key = row.get("team_id") or row["team_name"]
            conn.execute(
                """
                INSERT INTO standings (
                    country, league, season, team_key, team_id, team_name,
                    position, zone, team_logo_url, played, won, drawn, lost,
                    goals_for, goals_against, goal_difference, points,
                    form_json, raw_json, stage_id, scraped_at
                ) VALUES (
                    :country, :league, :season, :team_key, :team_id, :team_name,
                    :position, :zone, :team_logo_url, :played, :won, :drawn, :lost,
                    :goals_for, :goals_against, :goal_difference, :points,
                    :form_json, :raw_json, :stage_id, :scraped_at
                )
                ON CONFLICT (country, league, season, team_key) DO UPDATE SET
                    team_id = excluded.team_id,
                    team_name = excluded.team_name,
                    position = excluded.position,
                    zone = excluded.zone,
                    team_logo_url = excluded.team_logo_url,
                    played = excluded.played,
                    won = excluded.won,
                    drawn = excluded.drawn,
                    lost = excluded.lost,
                    goals_for = excluded.goals_for,
                    goals_against = excluded.goals_against,
                    goal_difference = excluded.goal_difference,
                    points = excluded.points,
                    form_json = excluded.form_json,
                    raw_json = excluded.raw_json,
                    stage_id = excluded.stage_id,
                    scraped_at = excluded.scraped_at
                """,
                {
                    "country": meta["country"],
                    "league": meta["league"],
                    "season": season,
                    "team_key": team_key,
                    "team_id": row.get("team_id"),
                    "team_name": row["team_name"],
                    "position": row.get("position"),
                    "zone": row.get("zone"),
                    "team_logo_url": row.get("team_logo_url"),
                    "played": row.get("played"),
                    "won": row.get("won"),
                    "drawn": row.get("drawn"),
                    "lost": row.get("lost"),
                    "goals_for": row.get("goals_for"),
                    "goals_against": row.get("goals_against"),
                    "goal_difference": row.get("goal_difference"),
                    "points": row.get("points"),
                    "form_json": json.dumps(row.get("form", [])),
                    "raw_json": json.dumps(row),
                    "stage_id": meta.get("stage_id"),
                    "scraped_at": meta["scraped_at"],
                },
            )
    return len(standings)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["form"] = json.loads(d.pop("form_json") or "[]")
    d.pop("raw_json", None)
    return d


def get_standings(
    country: str,
    league: str,
    season: str | None = None,
    db_path: Path | str = config.DB_PATH,
) -> dict[str, Any] | None:
    """Return the stored standings table for a league, ordered by position.

    If ``season`` is omitted, the most recently scraped season on record is used.
    Returns None if nothing is stored for this league yet.
    """
    with get_connection(db_path) as conn:
        if season is None:
            latest = conn.execute(
                """
                SELECT season FROM standings
                WHERE country = ? AND league = ?
                ORDER BY scraped_at DESC LIMIT 1
                """,
                (country, league),
            ).fetchone()
            if latest is None:
                return None
            season = latest["season"]

        rows = conn.execute(
            """
            SELECT * FROM standings
            WHERE country = ? AND league = ? AND season = ?
            ORDER BY position ASC
            """,
            (country, league, season),
        ).fetchall()

        if not rows:
            return None

        return {
            "country": country,
            "league": league,
            "season": season,
            "standings": [_row_to_dict(row) for row in rows],
        }


def get_team(
    country: str,
    league: str,
    team_name: str,
    season: str | None = None,
    db_path: Path | str = config.DB_PATH,
) -> dict[str, Any] | None:
    with get_connection(db_path) as conn:
        if season is None:
            latest = conn.execute(
                """
                SELECT season FROM standings
                WHERE country = ? AND league = ?
                ORDER BY scraped_at DESC LIMIT 1
                """,
                (country, league),
            ).fetchone()
            if latest is None:
                return None
            season = latest["season"]

        row = conn.execute(
            """
            SELECT * FROM standings
            WHERE country = ? AND league = ? AND season = ?
              AND team_name = ? COLLATE NOCASE
            """,
            (country, league, season, team_name),
        ).fetchone()

        return _row_to_dict(row) if row else None
