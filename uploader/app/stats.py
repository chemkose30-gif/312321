"""조회수·시청시간 수집.

게시에 성공한 영상마다 플랫폼에 물어서 지표를 가져와 저장한다.
하루에 한 줄씩 쌓아 추이를 볼 수 있게 한다.
"""
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import db
from .config import GRAPH
from .platforms import instagram_login, youtube

KST = timezone(timedelta(hours=9))
YT_ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"


def today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _unique(items: list[str]) -> list[str]:
    """순서를 유지하면서 중복을 없앤다(같은 영상을 두 번 물어보지 않도록)."""
    seen: set[str] = set()
    return [i for i in items if i and not (i in seen or seen.add(i))]


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _num(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


# ── 유튜브 ──────────────────────────────────────────────────
# 한 번에 보낼 수 있는 개수 — 유튜브가 정한 한도.
YT_STATS_CHUNK = 50        # videos.list 의 id 파라미터
YT_ANALYTICS_CHUNK = 200   # Analytics 의 video 필터


async def youtube_stats(account: dict, video_ids: list[str]) -> dict[str, dict]:
    """조회수·좋아요·댓글은 videos.list, 시청시간은 Analytics API."""
    token = await youtube._access_token(account)
    headers = {"Authorization": f"Bearer {token}"}
    out: dict[str, dict] = {}
    video_ids = _unique(video_ids)

    async with httpx.AsyncClient(timeout=30) as client:
        for chunk in _chunks(video_ids, YT_STATS_CHUNK):
            res = await client.get(
                f"{youtube.API}/videos",
                params={"part": "statistics", "id": ",".join(chunk)},
                headers=headers,
            )
            if res.status_code >= 400:
                print(f"[stats] 유튜브 조회수 실패 {res.status_code} {res.text[:200]}")
                continue
            for item in (res.json() or {}).get("items") or []:
                st = item.get("statistics") or {}
                out[item["id"]] = {
                    "views": _num(st.get("viewCount")),
                    "likes": _num(st.get("likeCount")),
                    "comments": _num(st.get("commentCount")),
                }

        # 시청시간 — 채널 단위 Analytics. 권한이 없으면 조용히 건너뛴다.
        start = (datetime.now(KST) - timedelta(days=365)).strftime("%Y-%m-%d")
        for chunk in _chunks(video_ids, YT_ANALYTICS_CHUNK):
            res = await client.get(
                YT_ANALYTICS,
                params={
                    "ids": "channel==MINE",
                    "startDate": start,
                    "endDate": today(),
                    "metrics": "estimatedMinutesWatched,views",
                    "dimensions": "video",
                    "filters": "video==" + ",".join(chunk),
                    "maxResults": YT_ANALYTICS_CHUNK,
                },
                headers=headers,
            )
            if res.status_code >= 400:
                print(f"[stats] 유튜브 시청시간 실패 {res.status_code} {res.text[:200]}")
                break
            body = res.json() or {}
            cols = [c["name"] for c in body.get("columnHeaders") or []]
            for row in body.get("rows") or []:
                data = dict(zip(cols, row))
                vid = data.get("video")
                if vid:
                    out.setdefault(vid, {})["watch_sec"] = (
                        _num(data.get("estimatedMinutesWatched")) * 60
                    )
    return out


# ── 인스타그램 ──────────────────────────────────────────────
IG_METRICS = "views,likes,comments,shares,ig_reels_video_view_total_time"


async def instagram_stats(account: dict, media_ids: list[str]) -> dict[str, dict]:
    login_mode = (account.get("meta") or {}).get("auth") == "instagram_login"
    base = instagram_login.GRAPH if login_mode else GRAPH
    token = (
        await instagram_login._fresh_token(account) if login_mode else account["access_token"]
    )
    out: dict[str, dict] = {}

    async with httpx.AsyncClient(timeout=30) as client:
        for media_id in _unique(media_ids):
            res = await client.get(
                f"{base}/{media_id}/insights",
                params={"metric": IG_METRICS, "access_token": token},
            )
            if res.status_code >= 400:
                print(f"[stats] 인스타 {media_id} 실패 {res.status_code} {res.text[:160]}")
                continue
            row: dict = {}
            for item in (res.json() or {}).get("data") or []:
                name = item.get("name")
                values = item.get("values") or [{}]
                value = _num(values[0].get("value"))
                if name in ("views", "plays"):
                    row["views"] = value
                elif name == "likes":
                    row["likes"] = value
                elif name == "comments":
                    row["comments"] = value
                elif name == "shares":
                    row["shares"] = value
                elif name == "ig_reels_video_view_total_time":
                    row["watch_sec"] = value // 1000      # 밀리초로 온다
            if row:
                out[media_id] = row
    return out


# ── 페이스북 ────────────────────────────────────────────────
FB_METRICS = "total_video_views,total_video_view_total_time"


async def facebook_stats(account: dict, video_ids: list[str]) -> dict[str, dict]:
    token = account["access_token"]
    out: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for video_id in _unique(video_ids):
            res = await client.get(
                f"{GRAPH}/{video_id}/video_insights",
                params={"metric": FB_METRICS, "access_token": token},
            )
            if res.status_code >= 400:
                print(f"[stats] 페북 {video_id} 실패 {res.status_code} {res.text[:160]}")
                continue
            row: dict = {}
            for item in (res.json() or {}).get("data") or []:
                values = item.get("values") or [{}]
                value = _num(values[0].get("value"))
                if item.get("name") == "total_video_views":
                    row["views"] = value
                elif item.get("name") == "total_video_view_total_time":
                    row["watch_sec"] = value // 1000
            if row:
                out[video_id] = row
    return out


COLLECTORS = {
    "youtube": youtube_stats,
    "instagram": instagram_stats,
    "facebook": facebook_stats,
}


# 이 기간 안에 올린 영상만 새로 받아온다.
# 인스타·페북은 영상 1개당 요청 1번이라, 전체를 계속 다시 도는 것은 감당이 안 된다.
# 기간이 지난 영상은 마지막으로 받아둔 값이 그대로 남는다.
COLLECT_WINDOW_DAYS = 90


async def collect(days: int | None = COLLECT_WINDOW_DAYS) -> dict:
    """게시된 영상들의 지표를 모아 저장한다."""
    targets = db.published_targets(time.time() - days * 86400 if days else None)
    if not targets:
        return {"targets": 0, "saved": 0}

    # 계정별로 묶어서 한 번에 조회한다.
    by_account: dict[str, list[dict]] = {}
    for target in targets:
        by_account.setdefault(target["account_id"], []).append(target)

    day, saved = today(), 0
    for account_id, group in by_account.items():
        account = db.get_account(account_id)
        if not account or not account.get("access_token"):
            continue
        collector = COLLECTORS.get(account["platform"])
        if collector is None:
            continue
        ids = [t["remote_id"] for t in group if t.get("remote_id")]
        try:
            result = await collector(account, ids)
        except Exception as exc:
            print(f"[stats] {account['platform']} {account.get('name')} 수집 실패:"
                  f" {type(exc).__name__}: {exc}")
            continue
        for target in group:
            data = result.get(target.get("remote_id") or "")
            if data:
                db.save_stats(target["id"], day, data)
                saved += 1
    print(f"[stats] {len(targets)}개 중 {saved}개 지표를 저장했습니다.")
    return {"targets": len(targets), "saved": saved}


# ── 집계 ────────────────────────────────────────────────────
def summary(days: int | None = 30) -> dict:
    """성과 화면에 필요한 값을 한 번에 만든다."""
    since = time.time() - days * 86400 if days else None
    targets = db.published_targets(since)
    latest = db.latest_stats()
    accounts = {a["id"]: a for a in db.list_accounts()}

    total = {"views": 0, "watch_sec": 0, "likes": 0, "comments": 0, "videos": 0}
    by_account: dict[str, dict] = {}
    by_category: dict[str, dict] = {}
    by_platform: dict[str, dict] = {}
    videos: list[dict] = []

    for target in targets:
        stat = latest.get(target["id"]) or {}
        views, watch = stat.get("views", 0), stat.get("watch_sec", 0)
        account = accounts.get(target["account_id"]) or {}
        name = account.get("display_name") or "삭제된 계정"
        category = account.get("category") or "미분류"

        total["views"] += views
        total["watch_sec"] += watch
        total["likes"] += stat.get("likes", 0)
        total["comments"] += stat.get("comments", 0)
        total["videos"] += 1

        for bucket, key in (
            (by_account, name),
            (by_category, category),
            (by_platform, target["platform"]),
        ):
            row = bucket.setdefault(key, {"views": 0, "watch_sec": 0, "videos": 0})
            row["views"] += views
            row["watch_sec"] += watch
            row["videos"] += 1

        videos.append({
            "title": target.get("job_title") or target.get("video_name") or "",
            "platform": target["platform"],
            "account": account.get("display_name") or "삭제된 계정",
            "category": category,
            "url": target.get("url"),
            "created_at": target.get("job_created_at"),
            "views": views,
            "watch_sec": watch,
            "likes": stat.get("likes", 0),
            "has_stat": bool(stat),
        })

    def rows(bucket: dict) -> list[dict]:
        return sorted(
            ({"name": k, **v} for k, v in bucket.items()),
            key=lambda r: r["views"], reverse=True,
        )

    videos.sort(key=lambda v: v["views"], reverse=True)
    fetched = max((s.get("fetched_at") or 0 for s in latest.values()), default=0)
    return {
        "total": total,
        "by_account": rows(by_account),
        "by_category": rows(by_category),
        "by_platform": rows(by_platform),
        "videos": videos[:50],
        "daily": list(reversed(db.stats_by_day(30))),
        "fetched_at": fetched or None,
        "measured": sum(1 for v in videos if v["has_stat"]),
    }
