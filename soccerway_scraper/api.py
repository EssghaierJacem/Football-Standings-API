"""FastAPI serving layer for stored standings data."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from . import storage
from .scheduler import scrape_one

logger = logging.getLogger(__name__)

app = FastAPI(title="Soccerway Standings API")


@app.on_event("startup")
def _on_startup() -> None:
    storage.init_db()


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
