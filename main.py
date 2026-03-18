import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()
API_KEY = os.getenv("ODDS_API_KEY", "")

try:
    from kbl_scraper import get_kbl_schedule, get_kbl_standings, get_kbl_leaders
    KBL_AVAILABLE = True
except ImportError:
    KBL_AVAILABLE = False

ODDS_BASE = "https://api.the-odds-api.com/v4"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
BALLDONTLIE = "https://api.balldontlie.io/v1"

app = FastAPI(title="Sports Analytics Dashboard", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SPORT_CONFIGS = {
    "basketball_nba": {"label": "NBA", "cat": "basketball", "region": "us", "espn_path": "basketball/nba"},
    "basketball_kbl": {"label": "KBL", "cat": "basketball", "region": "us", "espn_path": None},
    "baseball_mlb":   {"label": "MLB", "cat": "baseball",   "region": "us", "espn_path": "baseball/mlb"},
    "baseball_kbo":   {"label": "KBO", "cat": "baseball",   "region": "eu", "espn_path": None},
    "baseball_npb":   {"label": "NPB", "cat": "baseball",   "region": "eu", "espn_path": None},
    "soccer_korea_kleague1":     {"label": "K리그1",    "cat": "soccer", "region": "eu", "espn_path": None},
    "soccer_epl":                {"label": "EPL",       "cat": "soccer", "region": "uk", "espn_path": "soccer/eng.1"},
    "soccer_spain_la_liga":      {"label": "La Liga",   "cat": "soccer", "region": "eu", "espn_path": "soccer/esp.1"},
    "soccer_germany_bundesliga": {"label": "Bundesliga","cat": "soccer", "region": "eu", "espn_path": "soccer/ger.1"},
    "soccer_italy_serie_a":      {"label": "Serie A",   "cat": "soccer", "region": "eu", "espn_path": "soccer/ita.1"},
    "soccer_uefa_champs_league": {"label": "UCL",       "cat": "soccer", "region": "eu", "espn_path": "soccer/uefa.champions"},
}

@app.get("/api/plan")
async def check_plan():
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{ODDS_BASE}/sports/", params={"apiKey": API_KEY})
        headers_info = {
            "remaining": r.headers.get("x-requests-remaining"),
            "used": r.headers.get("x-requests-used"),
        }
        sports_count = len(r.json()) if r.is_success else 0
        r2 = await client.get(f"{ODDS_BASE}/sports/basketball_nba/odds/",
            params={"apiKey": API_KEY, "regions": "us", "markets": "player_points", "oddsFormat": "decimal"})
        r3 = await client.get(f"{ODDS_BASE}/sports/basketball_nba/odds/",
            params={"apiKey": API_KEY, "regions": "us", "markets": "h2h_h1", "oddsFormat": "decimal"})
        return {
            "api_key_prefix": API_KEY[:8] + "••••••••",
            "headers": headers_info,
            "sports_count": sports_count,
            "markets": {
                "player_props": {"available": r2.is_success, "message": "사용 가능" if r2.is_success else "불가"},
                "quarter_markets": {"available": r3.is_success, "message": "사용 가능" if r3.is_success else "불가"},
            }
        }

@app.get("/api/odds/{sport_key}")
async def get_odds(
    sport_key: str,
    markets: str = Query("h2h,totals,spreads", description="comma-separated markets"),
):
    if sport_key not in SPORT_CONFIGS:
        raise HTTPException(404, f"Unknown sport: {sport_key}")
    cfg = SPORT_CONFIGS[sport_key]
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{ODDS_BASE}/sports/{sport_key}/odds/",
            params={"apiKey": API_KEY, "regions": cfg["region"],
                    "markets": markets, "oddsFormat": "decimal", "dateFormat": "iso"}
        )
        remaining = r.headers.get("x-requests-remaining")
        if not r.is_success:
            err = r.json() if "application/json" in r.headers.get("content-type", "") else {}
            raise HTTPException(r.status_code, err.get("message", "Odds API error"))
        return {"games": r.json(), "remaining": remaining, "sport": cfg}

@app.get("/api/nba/game/{game_id}")
async def get_nba_game_detail(game_id: str):
    async with httpx.AsyncClient(timeout=20) as client:
        half_task = client.get(
            f"{ODDS_BASE}/sports/basketball_nba/events/{game_id}/odds",
            params={"apiKey": API_KEY, "regions": "us",
                    "markets": "h2h_h1,h2h_h2,totals_h1,totals_h2", "oddsFormat": "decimal"})
        props_task = client.get(
            f"{ODDS_BASE}/sports/basketball_nba/events/{game_id}/odds",
            params={"apiKey": API_KEY, "regions": "us",
                    "markets": "player_points,player_rebounds,player_assists,player_threes",
                    "oddsFormat": "decimal"})
        hr, pr = await asyncio.gather(half_task, props_task, return_exceptions=True)

        quarters = {}
        props = []

        if not isinstance(hr, Exception) and hr.is_success:
            data = hr.json()
            bm = next((b for b in (data.get("bookmakers") or []) if b.get("markets")), None)
            if bm:
                for mkt in bm.get("markets", []):
                    quarters[mkt["key"]] = mkt.get("outcomes", [])

        if not isinstance(pr, Exception) and pr.is_success:
            data = pr.json()
            bm = next((b for b in (data.get("bookmakers") or []) if b.get("markets")), None)
            if bm:
                for mkt in bm.get("markets", []):
                    mkt_type = mkt["key"]
                    players = {}
                    for o in mkt.get("outcomes", []):
                        pname = o.get("description", o.get("name", ""))
                        side = o.get("name", "")
                        point = o.get("point", 0)
                        price = o.get("price", 2.0)
                        if pname not in players:
                            players[pname] = {"name": pname, "market": mkt_type, "line": point}
                        if "over" in side.lower():
                            players[pname]["over"] = price
                        elif "under" in side.lower():
                            players[pname]["under"] = price
                    for p in players.values():
                        over = p.get("over", 99)
                        under = p.get("under", 99)
                        p["recommendation"] = "over" if over < under else "under"
                        p["confidence"] = round((1 / min(over, under)) * 100, 1)
                        props.append(p)

        props.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        remaining = hr.headers.get("x-requests-remaining") if not isinstance(hr, Exception) else None
        return {"game_id": game_id, "quarters": quarters, "props": props[:50], "remaining": remaining}

@app.get("/api/espn/injuries/{sport_key}")
async def get_injuries(sport_key: str):
    cfg = SPORT_CONFIGS.get(sport_key)
    if not cfg or not cfg.get("espn_path"):
        return {"injuries": [], "note": "ESPN 부상 데이터 없음"}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{ESPN_BASE}/{cfg['espn_path']}/injuries")
        if not r.is_success:
            return {"injuries": [], "note": f"ESPN 오류: {r.status_code}"}
        return r.json()

@app.get("/api/espn/scoreboard/{sport_key}")
async def get_scoreboard(sport_key: str):
    cfg = SPORT_CONFIGS.get(sport_key)
    if not cfg or not cfg.get("espn_path"):
        return {"events": []}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{ESPN_BASE}/{cfg['espn_path']}/scoreboard")
        return r.json() if r.is_success else {"events": []}

@app.get("/api/players/search")
async def search_players(name: str = Query(..., min_length=2)):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{BALLDONTLIE}/players", params={"search": name, "per_page": 10})
        if not r.is_success:
            raise HTTPException(r.status_code, "Player search failed")
        return r.json()

@app.get("/api/players/{player_id}/stats")
async def get_player_stats(player_id: int, seasons: str = Query("2024")):
    season_list = [int(s.strip()) for s in seasons.split(",")]
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{BALLDONTLIE}/season_averages",
            params={"player_ids[]": player_id, "season": season_list[-1]})
        if not r.is_success:
            raise HTTPException(r.status_code, "Stats fetch failed")
        return r.json()

@app.get("/api/players/{player_id}/recent")
async def get_recent_games(player_id: int, last_n: int = Query(10, ge=1, le=20)):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{BALLDONTLIE}/stats",
            params={"player_ids[]": player_id, "per_page": last_n, "seasons[]": 2024})
        if not r.is_success:
            raise HTTPException(r.status_code, "Recent games fetch failed")
        return r.json()

@app.get("/api/teams/nba")
async def get_nba_teams():
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{BALLDONTLIE}/teams", params={"per_page": 30})
        if not r.is_success:
            raise HTTPException(r.status_code, "Teams fetch failed")
        return r.json()

@app.get("/api/kbl/schedule")
async def kbl_schedule(date: str = Query(None)):
    if not KBL_AVAILABLE:
        raise HTTPException(503, "KBL 스크래퍼 미설치")
    from datetime import datetime
    d = date or datetime.now().strftime("%Y%m%d")
    return await get_kbl_schedule(d)

@app.get("/api/kbl/standings")
async def kbl_standings():
    if not KBL_AVAILABLE:
        raise HTTPException(503, "KBL 스크래퍼 미설치")
    return await get_kbl_standings()

@app.get("/api/kbl/leaders")
async def kbl_leaders():
    if not KBL_AVAILABLE:
        raise HTTPException(503, "KBL 스크래퍼 미설치")
    return await get_kbl_leaders()

frontend_dir = Path(__file__).parent / "frontend"

@app.get("/")
async def root():
    return FileResponse(frontend_dir / "index.html")

app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8765)), reload=False)
