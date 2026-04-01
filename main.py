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

@app.get("/api/espn/boxscore/{sport_key}/{event_id}")
async def get_boxscore(sport_key: str, event_id: str):
    cfg = SPORT_CONFIGS.get(sport_key)
    if not cfg or not cfg.get("espn_path"):
        raise HTTPException(404, "지원하지 않는 리그")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{ESPN_BASE}/{cfg['espn_path']}/summary",
            params={"event": event_id}
        )
        if not r.is_success:
            raise HTTPException(r.status_code, "ESPN 박스스코어 없음")
        data = r.json()

        # boxscore.players 파싱
        teams = []
        for team_data in data.get("boxscore", {}).get("players", []):
            team_info = team_data.get("team", {})
            stats_list = team_data.get("statistics", [])
            if not stats_list:
                continue
            stat_block = stats_list[0]
            keys = stat_block.get("keys", [])
            label_map = {
                "PTS": "득점", "REB": "리바운드", "AST": "어시스트",
                "STL": "스틸", "BLK": "블록", "TO": "턴오버",
                "FGM-FGA": "야투", "3PM-3PA": "3점", "FTM-FTA": "자유투",
                "MIN": "시간", "+/-": "+/-"
            }
            display_keys = ["MIN", "PTS", "REB", "AST", "STL", "BLK", "TO", "FGM-FGA", "3PM-3PA"]
            key_indices = {k: i for i, k in enumerate(keys)}

            players = []
            for ath in stat_block.get("athletes", []):
                athlete = ath.get("athlete", {})
                stats = ath.get("stats", [])
                starter = ath.get("starter", False)
                did_not_play = ath.get("didNotPlay", False)
                reason = ath.get("reason", "")
                player = {
                    "id": athlete.get("id"),
                    "name": athlete.get("shortName") or athlete.get("displayName", ""),
                    "pos": athlete.get("position", {}).get("abbreviation", ""),
                    "starter": starter,
                    "dnp": did_not_play,
                    "reason": reason,
                    "stats": {}
                }
                for k in display_keys:
                    idx = key_indices.get(k)
                    if idx is not None and idx < len(stats):
                        player["stats"][k] = stats[idx]
                    else:
                        player["stats"][k] = "-"
                players.append(player)

            teams.append({
                "team": team_info.get("displayName", ""),
                "abbr": team_info.get("abbreviation", ""),
                "color": team_info.get("color", ""),
                "display_keys": display_keys,
                "label_map": label_map,
                "players": players,
            })

        return {"teams": teams, "event_id": event_id}

@app.get("/api/espn/team_map/{sport_key}")
async def get_team_map(sport_key: str):
    """ESPN 팀명 → ID 매핑 반환"""
    cfg = SPORT_CONFIGS.get(sport_key)
    if not cfg or not cfg.get("espn_path"):
        return {"teams": {}}
    espn_path = cfg["espn_path"]
    url = f"{ESPN_BASE}/{espn_path}/teams"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params={"limit": 100})
        if not r.is_success:
            return {"teams": {}}
        data = r.json()
    teams_map = {}
    for sport in data.get("sports", []):
        for league in sport.get("leagues", []):
            for team in league.get("teams", []):
                t = team.get("team", {})
                tid = t.get("id", "")
                name = t.get("displayName", "")
                short = t.get("shortDisplayName", "")
                if tid and name:
                    teams_map[name]  = tid
                    teams_map[short] = tid
    return {"teams": teams_map}

@app.get("/api/espn/team_form/{sport_key}/{team_id}")
async def get_team_form(sport_key: str, team_id: str, last_n: int = 10):
    """팀 최근 N경기 폼 + 홈/원정 승률 + 평균 득점"""
    cfg = SPORT_CONFIGS.get(sport_key)
    if not cfg or not cfg.get("espn_path"):
        return {"form": [], "record": {}}
    url = f"{ESPN_BASE}/{cfg['espn_path']}/teams/{team_id}/schedule"
    async with httpx.AsyncClient(timeout=12) as client:
        r = await client.get(url)
        if not r.is_success:
            return {"form": [], "record": {}}
        data = r.json()

    results = []
    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            if not comp.get("status", {}).get("type", {}).get("completed", False):
                continue
            competitors = comp.get("competitors", [])
            me = next((c for c in competitors if c.get("id") == team_id), None)
            opp = next((c for c in competitors if c.get("id") != team_id), None)
            if not me or not opp:
                continue
            home_away = me.get("homeAway", "home")
            my_score = int(me.get("score", 0) or 0)
            opp_score = int(opp.get("score", 0) or 0)
            won = me.get("winner", False)
            results.append({
                "won": won,
                "home": home_away == "home",
                "my_score": my_score,
                "opp_score": opp_score,
                "opp_name": opp.get("team", {}).get("shortDisplayName", ""),
                "date": event.get("date", ""),
            })

    results = results[-last_n:] if len(results) > last_n else results
    if not results:
        return {"form": [], "record": {}}

    home_games = [r for r in results if r["home"]]
    away_games = [r for r in results if not r["home"]]
    recent5 = results[-5:] if len(results) >= 5 else results

    return {
        "form": [{"w": r["won"], "home": r["home"], "pts": r["my_score"], "opp_pts": r["opp_score"], "opp": r["opp_name"]} for r in results],
        "record": {
            "total": {"w": sum(1 for r in results if r["won"]), "l": sum(1 for r in results if not r["won"]), "g": len(results)},
            "home":  {"w": sum(1 for r in home_games if r["won"]),  "l": sum(1 for r in home_games if not r["won"]),  "g": len(home_games)},
            "away":  {"w": sum(1 for r in away_games if r["won"]), "l": sum(1 for r in away_games if not r["won"]), "g": len(away_games)},
            "avg_pts": round(sum(r["my_score"] for r in results) / len(results), 1) if results else 0,
            "recent5_w": sum(1 for r in recent5 if r["won"]),
        }
    }


@app.get("/api/espn/injury_impact/{sport_key}/{team_id}")
async def get_injury_impact(sport_key: str, team_id: str):
    """팀 부상자 리스트 + 선수별 득점 영향도 추정"""
    cfg = SPORT_CONFIGS.get(sport_key)
    if not cfg or not cfg.get("espn_path"):
        return {"players": [], "total_pts_lost": 0}

    async with httpx.AsyncClient(timeout=12) as client:
        # 부상자 목록
        inj_r = await client.get(f"{ESPN_BASE}/{cfg['espn_path']}/injuries")
        # 팀 로스터 (선수별 스탯 포함)
        roster_r = await client.get(f"{ESPN_BASE}/{cfg['espn_path']}/teams/{team_id}/roster")

    injured_players = []
    roster_stats = {}

    if roster_r.is_success:
        roster_data = roster_r.json()
        for athlete in roster_data.get("athletes", []):
            for a in (athlete.get("items") or [athlete]):
                aid = a.get("id", "")
                name = a.get("fullName", a.get("displayName", ""))
                stats = a.get("statistics", {})
                # 시즌 평균 득점 추출
                pts = 0.0
                if isinstance(stats, dict):
                    for cat in stats.get("splits", {}).get("categories", []):
                        for stat in cat.get("stats", []):
                            if stat.get("name") in ("avgPoints", "points", "pointsPerGame"):
                                try: pts = float(stat.get("value", 0)); break
                                except: pass
                if aid:
                    roster_stats[aid] = {"name": name, "avg_pts": pts}

    if inj_r.is_success:
        inj_data = inj_r.json()
        for inj in inj_data.get("injuries", []):
            # ESPN injury API는 리그 전체 부상자를 반환 - 팀 ID로 필터
            team_info = inj.get("team", {})
            if team_info.get("id") != team_id:
                continue
            athlete = inj.get("athlete", {})
            aid = athlete.get("id", "")
            aname = athlete.get("displayName", athlete.get("shortName", ""))
            status = inj.get("status", "")
            desc = inj.get("longComment", inj.get("shortComment", ""))
            stats_info = roster_stats.get(aid, {})
            avg_pts = stats_info.get("avg_pts", 0)
            injured_players.append({
                "id": aid,
                "name": aname,
                "status": status.lower(),
                "desc": desc[:60] if desc else "",
                "avg_pts": avg_pts,
                "impact": f"-{avg_pts:.1f}점" if avg_pts > 0 else "영향 미상",
            })

    injured_players.sort(key=lambda x: x["avg_pts"], reverse=True)
    out_players = [p for p in injured_players if p["status"] in ("out", "doubtful", "questionable")]
    total_pts_lost = sum(p["avg_pts"] for p in out_players if p["status"] == "out")

    return {
        "players": injured_players,
        "out_count": len([p for p in injured_players if p["status"] == "out"]),
        "total_pts_lost": round(total_pts_lost, 1),
    }


@app.get("/api/analytics/smart_picks/{sport_key}")
async def get_smart_picks(sport_key: str):
    """종합 점수 기반 전문 픽 (폼+홈어드밴티지+부상+배당 밸류)"""
    cfg = SPORT_CONFIGS.get(sport_key)
    if not cfg:
        raise HTTPException(404, "Unknown sport")

    async with httpx.AsyncClient(timeout=15) as client:
        odds_r = await client.get(
            f"{ODDS_BASE}/sports/{sport_key}/odds/",
            params={"apiKey": API_KEY, "regions": cfg["region"],
                    "markets": "h2h,spreads,totals", "oddsFormat": "decimal", "dateFormat": "iso"}
        )
        if not odds_r.is_success:
            raise HTTPException(odds_r.status_code, "Odds fetch failed")

        inj_r = await client.get(f"{ESPN_BASE}/{cfg['espn_path']}/injuries") if cfg.get("espn_path") else None
        team_map_r = await client.get(f"{ESPN_BASE}/{cfg['espn_path']}/teams", params={"limit": 100}) if cfg.get("espn_path") else None

    games_data = odds_r.json()
    inj_map = {}  # teamId -> {out_count, pts_lost}
    team_name_to_id = {}

    # 팀 이름 → ESPN ID 매핑
    if team_map_r and team_map_r.is_success:
        tdata = team_map_r.json()
        for sport_item in tdata.get("sports", []):
            for league in sport_item.get("leagues", []):
                for t in league.get("teams", []):
                    tt = t.get("team", {})
                    tid = tt.get("id", "")
                    for key in [tt.get("displayName",""), tt.get("shortDisplayName",""), tt.get("name",""), tt.get("abbreviation","")]:
                        if key: team_name_to_id[key.lower()] = tid

    # 부상자 맵 구성
    if inj_r and inj_r.is_success:
        for inj in inj_r.json().get("injuries", []):
            tid = inj.get("team", {}).get("id", "")
            if not tid: continue
            if tid not in inj_map: inj_map[tid] = {"out": 0, "pts": 0.0}
            if inj.get("status", "").lower() == "out":
                inj_map[tid]["out"] += 1

    picks = []
    now = __import__("datetime").datetime.utcnow()

    for g in games_data:
        if not g.get("bookmakers"): continue
        try:
            game_dt = __import__("datetime").datetime.fromisoformat(g["commence_time"].replace("Z",""))
        except: continue
        if game_dt < now: continue

        bm = g["bookmakers"][0]
        h2h = next((m for m in bm.get("markets",[]) if m["key"]=="h2h"), None)
        if not h2h: continue

        hp = next((o["price"] for o in h2h["outcomes"] if o["name"]==g["home_team"]), None)
        ap = next((o["price"] for o in h2h["outcomes"] if o["name"]==g["away_team"]), None)
        if not hp or not ap: continue

        # 노비그 확률
        total = 1/hp + 1/ap
        home_p = (1/hp) / total * 100
        away_p = (1/ap) / total * 100

        # ESPN 팀 ID 찾기
        home_id = team_name_to_id.get(g["home_team"].lower(), "")
        away_id = team_name_to_id.get(g["away_team"].lower(), "")
        home_inj = inj_map.get(home_id, {"out": 0, "pts": 0.0})
        away_inj = inj_map.get(away_id, {"out": 0, "pts": 0.0})

        # 종합 점수 계산
        for is_home in [True, False]:
            team = g["home_team"] if is_home else g["away_team"]
            base_p = home_p if is_home else away_p
            my_inj = home_inj if is_home else away_inj
            op_inj = away_inj if is_home else home_inj

            # 1. 배당 밸류 점수 (30점)
            odds_score = min(base_p * 0.30, 30)
            # 2. 홈 어드밴티지 (20점)
            home_bonus = 20 if is_home else 0
            # 3. 부상자 영향 (25점) - 상대 결장 많을수록 유리
            inj_score = min(op_inj["out"] * 5, 15) - min(my_inj["out"] * 5, 10)
            inj_score = max(0, min(inj_score + 12, 25))
            # 4. 폼 점수 (25점) - 기본값 12.5 (폼 데이터 없을 때)
            form_score = 12.5

            total_score = odds_score + home_bonus + inj_score + form_score

            if total_score < 50: continue

            confidence = "HIGH" if total_score >= 70 else "MEDIUM" if total_score >= 55 else "LOW"
            reasons = []
            reasons.append(f"승률 {base_p:.1f}%")
            if is_home: reasons.append("홈 어드밴티지")
            if op_inj["out"] > 0: reasons.append(f"상대 결장 {op_inj['out']}명")
            if my_inj["out"] > 0: reasons.append(f"우리팀 결장 {my_inj['out']}명 ⚠")

            picks.append({
                "home": g["home_team"],
                "away": g["away_team"],
                "time": g["commence_time"],
                "pick_team": team,
                "is_home_pick": is_home,
                "odds": hp if is_home else ap,
                "win_prob": round(base_p, 1),
                "score": round(total_score, 1),
                "confidence": confidence,
                "reasons": reasons,
                "my_out": my_inj["out"],
                "op_out": op_inj["out"],
            })
            break  # 한 경기당 1픽

    picks.sort(key=lambda x: x["score"], reverse=True)
    return {"picks": picks[:20], "sport": cfg["label"]}


@app.get("/api/espn/h2h/{sport_key}/{team1_id}/{team2_id}")
async def get_h2h(sport_key: str, team1_id: str, team2_id: str):
    """팀 시즌 스케줄에서 상대전적 계산"""
    cfg = SPORT_CONFIGS.get(sport_key)
    if not cfg:
        raise HTTPException(404, "Unknown sport")
    espn_path = cfg["espn_path"]
    url = f"{ESPN_BASE}/{espn_path}/teams/{team1_id}/schedule"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        if not r.is_success:
            raise HTTPException(r.status_code, "ESPN schedule fetch failed")
        data = r.json()
    wins = losses = 0
    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            competitors = comp.get("competitors", [])
            opp = next((c for c in competitors if c.get("id") == team2_id), None)
            if not opp:
                continue
            me = next((c for c in competitors if c.get("id") == team1_id), None)
            if not me:
                continue
            # 완료된 경기만
            status = comp.get("status", {}).get("type", {}).get("completed", False)
            if not status:
                continue
            if me.get("winner"):
                wins += 1
            else:
                losses += 1
    return {"team1_id": team1_id, "team2_id": team2_id, "wins": wins, "losses": losses, "total": wins + losses}

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
