"""ESPN API 응답 구조 확인용"""
import asyncio, httpx, json

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

async def main():
    async with httpx.AsyncClient(timeout=10) as c:
        # 스코어보드 확인
        r = await c.get(f"{ESPN_BASE}/basketball/nba/scoreboard")
        data = r.json()
        events = data.get("events", [])
        print(f"경기 수: {len(events)}")
        if events:
            ev = events[0]
            print(f"\n경기: {ev.get('name')}")
            for comp in ev.get("competitions", [{}])[0].get("competitors", []):
                team = comp.get("team", {}).get("displayName")
                roster = comp.get("roster", [])
                print(f"\n팀: {team} (로스터: {len(roster)}명)")
                for p in roster[:5]:
                    print(f"  {p.get('athlete',{}).get('displayName')} - active:{p.get('active')} starter:{p.get('starter')}")

asyncio.run(main())
