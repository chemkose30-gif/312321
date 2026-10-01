"""조회수·시청시간 수집.

게시에 성공한 영상마다 플랫폼에 물어서 지표를 가져와 저장한다.
하루에 한 줄씩 쌓아 추이를 볼 수 있게 한다.
"""
import asyncio
import json
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
# 메타(인스타·페북)는 영상마다 따로 물어봐야 해서 수가 많다. 동시에 보내되
# 너무 세게 보내면 rate limit 에 걸리므로 이만큼으로 묶어 둔다.
META_CONCURRENCY = 6
_GATE: asyncio.Semaphore | None = None


def _gate() -> asyncio.Semaphore:
    """이벤트 루프가 생긴 뒤에 만들어야 해서 쓸 때 만든다."""
    global _GATE
    if _GATE is None:
        _GATE = asyncio.Semaphore(META_CONCURRENCY)
    return _GATE


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
        async def one(media_id: str) -> None:
            async with _gate():
                res = await client.get(
                    f"{base}/{media_id}/insights",
                    params={"metric": IG_METRICS, "access_token": token},
                )
            if res.status_code >= 400:
                print(f"[stats] 인스타 {media_id} 실패 {res.status_code} {res.text[:160]}")
                return
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

        await asyncio.gather(*(one(m) for m in _unique(media_ids)))
    return out


# ── 페이스북 ────────────────────────────────────────────────
FB_METRICS = "total_video_views,total_video_view_total_time"


async def facebook_stats(account: dict, video_ids: list[str]) -> dict[str, dict]:
    token = account["access_token"]
    out: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        async def one(video_id: str) -> None:
            async with _gate():
                res = await client.get(
                    f"{GRAPH}/{video_id}/video_insights",
                    params={"metric": FB_METRICS, "access_token": token},
                )
            if res.status_code >= 400:
                print(f"[stats] 페북 {video_id} 실패 {res.status_code} {res.text[:160]}")
                return
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

        await asyncio.gather(*(one(v) for v in _unique(video_ids)))
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

    day = today()

    async def one_account(account_id: str, group: list[dict]) -> int:
        account = db.get_account(account_id)
        if not account or not account.get("access_token"):
            return 0
        collector = COLLECTORS.get(account["platform"])
        if collector is None:
            return 0
        ids = [t["remote_id"] for t in group if t.get("remote_id")]
        try:
            result = await collector(account, ids)
        except Exception as exc:  # noqa: BLE001
            print(f"[stats] {account['platform']} {account.get('name')} 수집 실패:"
                  f" {type(exc).__name__}: {exc}")
            return 0
        done = 0
        for target in group:
            data = result.get(target.get("remote_id") or "")
            if data:
                db.save_stats(target["id"], day, data)
                done += 1
        return done

    # 계정을 한 줄로 세우지 않고 동시에 처리한다(플랫폼 쪽 동시 요청 수는 따로 제한한다).
    counts = await asyncio.gather(*(one_account(a, g) for a, g in by_account.items()))
    saved = sum(counts)
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
        "by_platform": rows(by_platform),
        "by_account": rows(by_account),
        "videos": videos[:50],
        "daily": list(reversed(db.stats_by_day(30))),
        "fetched_at": fetched or None,
        "measured": sum(1 for v in videos if v["has_stat"]),
    }


# ── 채널에 실제로 올라가 있는 영상 목록 ─────────────────────
# 앱으로 올린 것만이 아니라, 사용자가 유튜브에서 직접 올린 것까지 세려면
# 각 플랫폼에 '네 채널에 뭐가 올라가 있냐' 를 물어봐야 한다.
UPLOAD_PAGE = 50
UPLOAD_MAX_PAGES = 12       # 넉넉히 600개까지


def _api_error(res, where: str) -> str:
    """플랫폼이 돌려준 거절 사유를 사람이 읽을 수 있게 바꾼다."""
    detail = ""
    try:
        body = res.json() or {}
        err = body.get("error") or {}
        detail = err.get("message") or (err if isinstance(err, str) else "")
    except Exception:  # noqa: BLE001
        detail = ""
    if not detail:
        detail = (res.text or "")[:200]
    hint = {
        401: " — 로그인이 만료됐습니다. 계정 관리에서 다시 로그인하세요.",
        403: " — 권한이 없거나 하루 사용량을 넘겼습니다. 다시 로그인하면 권한이 추가됩니다.",
        404: " — 채널을 찾을 수 없습니다.",
    }.get(res.status_code, "")
    return f"{where} 실패 ({res.status_code}){hint} {detail}".strip()


def _iso_to_epoch(value: str) -> float:
    """2026-09-28T12:34:56Z → epoch 초."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


async def youtube_uploads(account: dict, since: float) -> list[dict]:
    """채널 업로드 재생목록을 훑어 올린 영상과 날짜를 모은다."""
    token = await youtube._access_token(account)
    headers = {"Authorization": f"Bearer {token}"}
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            f"{youtube.API}/channels",
            params={"part": "contentDetails", "mine": "true"},
            headers=headers,
        )
        if res.status_code >= 400:
            raise RuntimeError(_api_error(res, "유튜브 채널 조회"))
        items = (res.json() or {}).get("items") or []
        if not items:
            raise RuntimeError("유튜브가 이 계정의 채널을 알려주지 않았습니다"
                               " — 계정 관리에서 다시 로그인해 보세요.")
        playlist = (((items[0].get("contentDetails") or {})
                     .get("relatedPlaylists") or {}).get("uploads"))
        if not playlist:
            raise RuntimeError("이 채널에는 업로드 재생목록이 없습니다.")

        page = None
        for _ in range(UPLOAD_MAX_PAGES):
            params = {"part": "contentDetails,snippet", "playlistId": playlist,
                      "maxResults": UPLOAD_PAGE}
            if page:
                params["pageToken"] = page
            res = await client.get(f"{youtube.API}/playlistItems", params=params, headers=headers)
            if res.status_code >= 400:
                if not out:      # 한 개도 못 받았으면 이유를 알려야 한다
                    raise RuntimeError(_api_error(res, "유튜브 영상 목록"))
                print(f"[uploads] 유튜브 목록 중단 {res.status_code} {res.text[:200]}")
                break
            body = res.json() or {}
            oldest = None
            for item in body.get("items") or []:
                details = item.get("contentDetails") or {}
                published = _iso_to_epoch(
                    details.get("videoPublishedAt")
                    or (item.get("snippet") or {}).get("publishedAt") or ""
                )
                oldest = published if oldest is None else min(oldest, published)
                if published and published >= since:
                    out.append({
                        "remote_id": details.get("videoId") or "",
                        "published_at": published,
                        "title": (item.get("snippet") or {}).get("title") or "",
                    })
            page = body.get("nextPageToken")
            # 목록은 최신순이므로, 기간 밖으로 넘어갔으면 더 볼 필요가 없다.
            if not page or (oldest is not None and oldest < since):
                break
    return out


async def instagram_uploads(account: dict, since: float) -> list[dict]:
    login_mode = (account.get("meta") or {}).get("auth") == "instagram_login"
    base = instagram_login.GRAPH if login_mode else GRAPH
    token = (
        await instagram_login._fresh_token(account) if login_mode else account["access_token"]
    )
    user_id = "me" if login_mode else account["external_id"]
    out: list[dict] = []
    url = f"{base}/{user_id}/media"
    params: dict = {"fields": "id,timestamp,caption", "limit": UPLOAD_PAGE,
                    "access_token": token}
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(UPLOAD_MAX_PAGES):
            res = await client.get(url, params=params)
            if res.status_code >= 400:
                if not out:
                    raise RuntimeError(_api_error(res, "인스타 영상 목록"))
                print(f"[uploads] 인스타 목록 중단 {res.status_code} {res.text[:200]}")
                break
            body = res.json() or {}
            oldest = None
            for item in body.get("data") or []:
                published = _iso_to_epoch(item.get("timestamp") or "")
                oldest = published if oldest is None else min(oldest, published)
                if published and published >= since:
                    out.append({"remote_id": str(item.get("id") or ""),
                                "published_at": published,
                                "title": (item.get("caption") or "")[:120]})
            nxt = ((body.get("paging") or {}).get("next"))
            if not nxt or (oldest is not None and oldest < since):
                break
            url, params = nxt, {}       # next 에 이미 모든 값이 붙어 있다
    return out


async def facebook_uploads(account: dict, since: float) -> list[dict]:
    token = account["access_token"]
    out: list[dict] = []
    url = f"{GRAPH}/{account['external_id']}/videos"
    params: dict = {"fields": "id,created_time,description", "limit": UPLOAD_PAGE,
                    "access_token": token}
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(UPLOAD_MAX_PAGES):
            res = await client.get(url, params=params)
            if res.status_code >= 400:
                if not out:
                    raise RuntimeError(_api_error(res, "페북 영상 목록"))
                print(f"[uploads] 페북 목록 중단 {res.status_code} {res.text[:200]}")
                break
            body = res.json() or {}
            oldest = None
            for item in body.get("data") or []:
                published = _iso_to_epoch(item.get("created_time") or "")
                oldest = published if oldest is None else min(oldest, published)
                if published and published >= since:
                    out.append({"remote_id": str(item.get("id") or ""),
                                "published_at": published,
                                "title": (item.get("description") or "")[:120]})
            nxt = ((body.get("paging") or {}).get("next"))
            if not nxt or (oldest is not None and oldest < since):
                break
            url, params = nxt, {}
    return out


UPLOAD_LISTERS = {
    "youtube": youtube_uploads,
    "instagram": instagram_uploads,
    "facebook": facebook_uploads,
}

WEEKLY_WEEKS = 12


async def collect_uploads(weeks: int = WEEKLY_WEEKS) -> dict:
    """연결된 모든 채널에서 '실제로 올라가 있는 영상' 목록을 받아 저장한다."""
    since = _week_start(time.time()) - (weeks - 1) * 7 * 86400
    failed: list[str] = []

    async def one_account(account: dict) -> int:
        lister = UPLOAD_LISTERS.get(account["platform"])
        if lister is None:
            return 0
        try:
            rows = await lister(account, since)
        except Exception as exc:  # noqa: BLE001
            message = str(exc) or type(exc).__name__
            failed.append(f"{account.get('name')} — {message}"[:300])
            print(f"[uploads] {account['platform']} {account.get('name')} 실패: {message}")
            return 0
        return db.save_channel_uploads(account["id"], rows)

    todo = [a for a in db.list_accounts(with_tokens=True) if a.get("access_token")]
    saved = sum(await asyncio.gather(*(one_account(a) for a in todo)))
    print(f"[uploads] {saved}개 영상을 기록했습니다.")
    # 버튼을 누르지 않아도 화면에서 볼 수 있게 마지막 결과를 남긴다.
    db.set_setting(UPLOAD_RESULT_KEY, json.dumps(
        {"at": time.time(), "saved": saved, "failed": failed}, ensure_ascii=False))
    return {"saved": saved, "failed": failed}


UPLOAD_RESULT_KEY = "uploads_last_result"


def last_upload_result() -> dict:
    try:
        return json.loads(db.get_setting(UPLOAD_RESULT_KEY) or "{}")
    except ValueError:
        return {}


def _week_start(ts: float) -> float:
    """그 시각이 속한 주의 월요일 0시(한국 시간) epoch."""
    kst_day = datetime.fromtimestamp(ts, KST).replace(hour=0, minute=0, second=0, microsecond=0)
    monday = kst_day - timedelta(days=kst_day.weekday())
    return monday.timestamp()


def weekly(weeks: int = WEEKLY_WEEKS) -> dict:
    """주차별로 올린 영상 수·게시 수를 센다.

    채널에서 받아온 목록이 기준이라 유튜브에 직접 올린 것도 함께 잡힌다.
    앱으로 올린 것은 id 로 구분해 '직접 올림' 과 나눠 센다.
    """
    this_week = _week_start(time.time())
    since = this_week - (weeks - 1) * 7 * 86400
    accounts = {a["id"]: a for a in db.list_accounts()}
    ours = db.app_remote_ids()

    buckets: dict[float, dict] = {}
    for i in range(weeks):
        start = since + i * 7 * 86400
        buckets[start] = {"start": start, "posts": 0, "app_posts": 0, "manual_posts": 0,
                          "by_platform": {}}

    # 게시 건수는 두 곳을 합쳐서 센다. 채널에서 받아온 목록이 더 정확하지만
    # (직접 올린 것까지 들어 있다) 아직 안 받아왔을 수도 있으므로, 앱이
    # 올린 기록도 같이 넣고 같은 영상은 하나로 친다.
    posts: dict[tuple, dict] = {}

    for target in db.published_targets(since=None):
        when = target.get("updated_at") or target.get("job_created_at") or 0
        if when < since:
            continue
        posts[(target["account_id"], target["remote_id"])] = {
            "at": when, "platform": target["platform"], "manual": False,
        }

    for row in db.channel_uploads_since(since):
        key = (row["account_id"], row["remote_id"])
        platform = (accounts.get(row["account_id"]) or {}).get("platform") or "기타"
        # 채널이 알려준 게시 시각이 더 정확하므로 그걸로 덮어쓴다.
        posts[key] = {
            "at": row["published_at"], "platform": platform,
            "manual": row["remote_id"] not in ours,
        }

    for info in posts.values():
        bucket = buckets.get(_week_start(info["at"]))
        if bucket is None:
            continue
        bucket["posts"] += 1
        bucket["manual_posts" if info["manual"] else "app_posts"] += 1
        bucket["by_platform"][info["platform"]] = (
            bucket["by_platform"].get(info["platform"], 0) + 1
        )

    # 영상 수(같은 영상을 여러 채널에 올린 것은 하나로) — 앱 기록으로만 알 수 있다.
    for job in db.list_jobs(500):
        done = [t for t in job["targets"] if t["status"] == "success"]
        if not done:
            continue
        when = max((t.get("updated_at") or job["created_at"]) for t in done)
        bucket = buckets.get(_week_start(when))
        if bucket is not None:
            bucket["videos"] = bucket.get("videos", 0) + 1

    # 화면에서 플랫폼마다 칸을 따로 쓰므로, 어떤 플랫폼이 나오는지 함께 알려준다.
    order = ["youtube", "instagram", "facebook", "tiktok"]
    seen = {p for b in buckets.values() for p in b["by_platform"]}
    seen |= {a["platform"] for a in accounts.values() if a.get("linked")}
    platforms = [p for p in order if p in seen] + sorted(seen - set(order))

    rows = []
    for start in sorted(buckets, reverse=True):
        b = buckets[start]
        b["videos"] = b.get("videos", 0)
        b["label"] = datetime.fromtimestamp(start, KST).strftime("%m/%d")
        rows.append(b)
    return {"weeks": rows, "platforms": platforms,
            "fetched_at": db.uploads_fetched_at() or None,
            "last_fetch": last_upload_result()}
