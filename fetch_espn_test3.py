import asyncio, httpx, json

async def main():
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries")
        data = r.json()
        injuries = data.get("injuries", [])
        # 첫 팀 확인
        t = injuries[0]
        print("팀 최상위 키:", list(t.keys()))
        print("displayName:", t.get("displayName"))
        p = t["injuries"][0]
        print("\n선수 키:", list(p.keys()))
        print("athlete 키:", list(p.get("athlete",{}).keys()))
        ath = p.get("athlete",{})
        print("이름:", ath.get("displayName"))
        print("포지션:", ath.get("position",{}).get("abbreviation"))
        print("status:", p.get("status"))

asyncio.run(main())
