import asyncio, httpx, json

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

async def main():
    async with httpx.AsyncClient(timeout=10) as c:
        # injuries 엔드포인트 구조 확인
        r = await c.get(f"{ESPN_BASE}/basketball/nba/injuries")
        data = r.json()
        injuries = data.get("injuries", [])
        print(f"부상 팀 수: {len(injuries)}")
        if injuries:
            team_entry = injuries[0]
            print(f"\n첫 번째 항목 키: {list(team_entry.keys())}")
            print(f"팀: {team_entry.get('team',{}).get('displayName')}")
            players = team_entry.get("injuries", [])
            print(f"선수 수: {len(players)}")
            if players:
                p = players[0]
                print(f"\n선수 항목 키: {list(p.keys())}")
                print(json.dumps(p, indent=2, ensure_ascii=False)[:500])

asyncio.run(main())
