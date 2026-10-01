"""FastAPI serving layer for stored standings data."""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from . import config, fixtures_storage, storage
from .scheduler import build_scheduler, run_all_tracked, scrape_one, scrape_team_one

logger = logging.getLogger(__name__)

app = FastAPI(title="Soccerway Standings API")
_scheduler = None


@app.on_event("startup")
async def _on_startup() -> None:
    storage.init_db()
    fixtures_storage.init_fixtures_db()
    if config.AUTO_SCRAPE:
        global _scheduler
        _scheduler = build_scheduler()
        _scheduler.start()
        asyncio.create_task(run_all_tracked())


@app.on_event("shutdown")
def _on_shutdown() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)


class AddLeagueRequest(BaseModel):
    country: str
    league: str


@app.get("/leagues")
def list_leagues() -> list[dict]:
    return storage.list_tracked_leagues()


@app.get("/standings")
def get_standings(
    country: str = Query(...),
    league: str = Query(...),
    season: str | None = Query(None),
) -> dict:
    data = storage.get_standings(country, league, season)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"No stored standings for {country}/{league}"
            + (f" season {season}" if season else ""),
        )
    return data


@app.get("/standings/{country}/{league}/teams/{team_name}")
def get_team(country: str, league: str, team_name: str, season: str | None = None) -> dict:
    team = storage.get_team(country, league, team_name, season)
    if team is None:
        raise HTTPException(
            status_code=404,
            detail=f"No stored data for team '{team_name}' in {country}/{league}",
        )
    return team


@app.post("/refresh")
async def refresh(country: str = Query(...), league: str = Query(...)) -> dict:
    """Scrape one league right now and return the fresh standings."""
    if not await scrape_one(country, league):
        raise HTTPException(status_code=502, detail=f"Scrape failed for {country}/{league}")
    return storage.get_standings(country, league) or {}


@app.get("/teams/{team_id}/fixtures")
def get_team_fixtures(team_id: str) -> list[dict]:
    """All stored fixtures of a team, soonest first."""
    return fixtures_storage.list_fixtures(team_id)


@app.get("/teams/{team_id}/next-match")
def get_team_next_match(team_id: str) -> dict:
    """The team's next match (teams, logos, kickoff, round), or 404 when none is scheduled."""
    match = fixtures_storage.get_next_match(team_id)
    if match is None:
        raise HTTPException(status_code=404, detail=f"No upcoming match stored for team {team_id}")
    return match


@app.post("/teams/{team_id}/refresh")
async def refresh_team(team_id: str) -> dict:
    """Scrape the team's fixtures right now. The team must be listed in config.TRACKED_TEAMS."""
    slug = next((s for s, tid in config.TRACKED_TEAMS if tid == team_id), None)
    if slug is None:
        raise HTTPException(status_code=404, detail=f"Team {team_id} is not tracked")
    if not await scrape_team_one(slug, team_id):
        raise HTTPException(status_code=502, detail=f"Fixtures scrape failed for team {team_id}")
    return {"team_id": team_id, "fixtures": fixtures_storage.list_fixtures(team_id)}


@app.post("/leagues")
async def add_league(request: AddLeagueRequest) -> dict:
    storage.add_tracked_league(request.country, request.league)
    success = await scrape_one(request.country, request.league)
    if not success:
        raise HTTPException(
            status_code=502,
            detail=(
                f"League {request.country}/{request.league} was added to tracking, "
                "but the initial scrape failed. It will be retried on the next "
                "scheduled run."
            ),
        )
    return storage.get_standings(request.country, request.league) or {}
