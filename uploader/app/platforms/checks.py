"""게시 전 사전 점검(preflight) — 플랫폼 API에 물어보고 걸릴 만한 것을 미리 찾는다."""
import httpx

from ..config import GRAPH, PUBLIC_BASE_URL
from . import instagram_login
from .base import (
    IG_MAX_HASHTAGS,
    IG_MODE_KEY,
    build_caption,
    count_hashtags,
    ig_process_budget,
)

Issue = dict  # {"level": "error"|"warn"|"ok", "text": str}


def err(text: str) -> Issue:
    return {"level": "error", "text": text}


def warn(text: str) -> Issue:
    return {"level": "warn", "text": text}


def ok(text: str) -> Issue:
    return {"level": "ok", "text": text}


def _has_error(items: list[Issue]) -> bool:
    return any(i["level"] == "error" for i in items)


def _mmss(sec: float | int | None) -> str:
    if not sec:
        return "-"
    sec = int(sec)
    return f"{sec // 60}분 {sec % 60}초"


# ── YouTube ─────────────────────────────────────────────────
async def youtube(account: dict, job: dict, options: dict) -> list[Issue]:
    from . import youtube as yt

    out: list[Issue] = []
    title = (options.get("title") or job.get("title") or "").strip()
    if not title:
        out.append(err("제목이 비어 있습니다. 유튜브는 제목 없이 업로드할 수 없습니다."))
    if len(title) > 100:
        out.append(warn(f"제목이 {len(title)}자입니다. 100자까지만 올라가고 나머지는 잘립니다."))
    if "<" in title or ">" in title:
        out.append(err("제목에 < 또는 > 가 있습니다. 유튜브가 거부하는 문자입니다."))
    description = build_caption(job, include_title=False, limit=None, max_tags=15)
    if "<" in description or ">" in description:
        out.append(err("설명에 < 또는 > 가 있습니다. 유튜브가 거부하는 문자입니다."))
    if len(description) > 5000:
        out.append(warn(f"설명이 {len(description)}자입니다. 5000자까지만 올라갑니다."))
    if count_hashtags(description) > 15:
        out.append(warn("해시태그가 15개를 넘어 앞의 15개만 태그로 올라갑니다."))

    try:
        token = await yt._access_token(account)
    except Exception as exc:  # 토큰 갱신 실패
        out.append(err(f"로그인이 만료됐습니다. 계정 관리에서 다시 연결하세요. ({str(exc)[:120]})"))
        return out

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.get(
                f"{yt.API}/channels",
                params={"part": "status,snippet,contentDetails", "mine": "true"},
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        out.append(warn(f"유튜브 상태를 확인하지 못했습니다: {exc}"))
        return out

    if res.status_code >= 400:
        out.append(err(f"유튜브가 계정 조회를 거부했습니다: {res.text[:200]}"))
        return out
    items = (res.json() or {}).get("items") or []
    if not items:
        out.append(err("이 구글 계정에 유튜브 채널이 없습니다."))
        return out

    ch = items[0]
    status = ch.get("status") or {}
    if ch.get("id") and account.get("external_id") and ch["id"] != account["external_id"]:
        out.append(warn(
            f"저장된 채널과 지금 로그인된 채널이 다릅니다 (지금: {ch.get('snippet', {}).get('title')}). "
            "그 채널로 올라갑니다."
        ))
    long_uploads = status.get("longUploadsStatus")
    duration = job.get("duration") or 0
    if long_uploads in ("disallowed", "eligible") and duration > 15 * 60:
        out.append(err(
            f"이 채널은 15분이 넘는 영상을 올릴 수 없습니다(현재 {_mmss(duration)}). "
            "유튜브에서 전화번호 인증을 마치면 풀립니다."
        ))
    if status.get("isLinked") is False:
        out.append(err("구글 계정에 유튜브 채널이 연결되어 있지 않습니다."))
    privacy = options.get("privacy") or "private"
    if privacy not in ("public", "unlisted", "private"):
        out.append(err(f"알 수 없는 공개 범위: {privacy}"))
    if not _has_error(out):
        out.append(ok(f"채널 '{ch.get('snippet', {}).get('title')}' 업로드 가능"))
    out.append(warn(
        "구글 API 기본 하루 할당량(10,000)으로는 업로드가 하루 6개까지입니다. "
        "더 필요하면 구글 클라우드 콘솔에서 할당량 상향을 신청하세요."
    ))
    return out


# ── TikTok ──────────────────────────────────────────────────
async def tiktok(account: dict, job: dict, options: dict) -> list[Issue]:
    from . import tiktok as tt

    out: list[Issue] = []
    caption = build_caption(job, limit=None)
    if len(caption) > 2200:
        out.append(warn(f"캡션이 {len(caption)}자입니다. 2200자까지만 올라갑니다."))

    try:
        token = await tt._access_token(account)
    except Exception as exc:
        out.append(err(f"로그인이 만료됐습니다. 계정 관리에서 다시 연결하세요. ({str(exc)[:120]})"))
        return out

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(
                f"{tt.API}/post/publish/creator_info/query/",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
            )
    except httpx.HTTPError as exc:
        out.append(warn(f"틱톡 상태를 확인하지 못했습니다: {exc}"))
        return out

    body = res.json() if res.status_code < 500 else {}
    error = (body.get("error") or {})
    if res.status_code >= 400 or (error.get("code") not in (None, "ok")):
        out.append(err(
            "틱톡이 게시 가능 여부 조회를 거부했습니다: "
            f"{error.get('message') or res.text[:200]}"
        ))
        return out

    data = body.get("data") or {}
    max_sec = data.get("max_video_post_duration_sec")
    duration = job.get("duration") or 0
    if max_sec and duration and duration > max_sec:
        out.append(err(f"이 계정이 올릴 수 있는 최대 길이는 {_mmss(max_sec)}인데 영상은 {_mmss(duration)}입니다."))
    allowed = data.get("privacy_level_options") or []
    privacy = options.get("privacy") or "SELF_ONLY"
    if allowed and privacy not in allowed:
        out.append(err(
            f"'{privacy}' 공개 범위를 쓸 수 없는 계정입니다. 가능한 값: {', '.join(allowed)}"
        ))
    if data.get("comment_disabled") and not options.get("disable_comment"):
        out.append(warn("이 계정은 댓글이 꺼져 있습니다. 댓글 허용 설정은 무시됩니다."))
    if data.get("stitch_disabled") and not options.get("disable_stitch"):
        out.append(warn("이 계정은 이어찍기(Stitch)가 꺼져 있습니다."))
    if data.get("duet_disabled") and not options.get("disable_duet"):
        out.append(warn("이 계정은 듀엣이 꺼져 있습니다."))
    if not _has_error(out):
        out.append(ok(f"크리에이터 '{data.get('creator_nickname') or account['name']}' 게시 가능"))
    return out


# ── Instagram ───────────────────────────────────────────────
async def instagram(account: dict, job: dict, options: dict) -> list[Issue]:
    out: list[Issue] = []
    token = account.get("access_token")
    ig_user_id = account.get("external_id")
    login_mode = (account.get("meta") or {}).get("auth") == "instagram_login"
    base = instagram_login.GRAPH if login_mode else GRAPH

    from .. import db
    pull_mode = (db.get_setting(IG_MODE_KEY) or "") == "pull"
    if pull_mode and not PUBLIC_BASE_URL.startswith("https://"):
        out.append(err(
            "인스타그램이 이 서버에서 영상을 내려받는 방식인데 공개 https 주소가 아닙니다 "
            f"(현재 {PUBLIC_BASE_URL}). 게시가 실패합니다."
        ))

    caption = build_caption(job, limit=None)
    tags = count_hashtags(caption)
    if tags > IG_MAX_HASHTAGS:
        out.append(warn(
            f"해시태그가 {tags}개입니다. 인스타 한도({IG_MAX_HASHTAGS}개)에 맞춰 앞에서부터 "
            f"{IG_MAX_HASHTAGS}개만 올리고 나머지 {tags - IG_MAX_HASHTAGS}개는 뺍니다. "
            "다른 플랫폼에는 태그가 그대로 다 올라갑니다."
        ))
    if len(caption) > 2200:
        out.append(warn(f"캡션이 {len(caption)}자입니다. 2200자까지만 올라갑니다."))

    size_mb = (job.get("video_size") or 0) / 1024 / 1024
    if size_mb >= 300:
        out.append(warn(
            f"영상이 {size_mb:.0f}MB입니다. 인스타그램이 이 서버에서 원본을 내려받아 인코딩하므로 "
            f"게시까지 최대 {ig_process_budget(job.get('video_size') or 0) // 60}분까지 걸릴 수 있습니다. "
            "원본 화질 그대로 올라가며, 화면을 닫아도 서버에서 계속 진행됩니다."
        ))

    duration = job.get("duration") or 0
    if duration and duration < 3:
        out.append(err(f"릴스는 3초 이상이어야 합니다(현재 {_mmss(duration)})."))
    if duration and duration > 15 * 60:
        out.append(err(f"릴스는 15분을 넘길 수 없습니다(현재 {_mmss(duration)})."))
    w, h = job.get("width") or 0, job.get("height") or 0
    if w and h and w > h:
        out.append(warn("가로 영상입니다. 릴스는 9:16 세로를 권장하며 양옆이 잘릴 수 있습니다."))

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.get(
                f"{base}/{ig_user_id}/content_publishing_limit",
                params={"fields": "config,quota_usage", "access_token": token},
            )
    except httpx.HTTPError as exc:
        out.append(warn(f"인스타그램 상태를 확인하지 못했습니다: {exc}"))
        return out

    if res.status_code >= 400:
        message = ((res.json() or {}).get("error") or {}).get("message") or res.text[:200]
        out.append(err(f"인스타그램이 계정 조회를 거부했습니다(로그인 만료일 수 있습니다): {message}"))
        return out

    rows = (res.json() or {}).get("data") or [{}]
    row = rows[0] if rows else {}
    used = row.get("quota_usage")
    total = (row.get("config") or {}).get("quota_total") or 25
    if used is not None:
        left = max(total - used, 0)
        if left <= 0:
            out.append(err(f"최근 24시간 게시 한도({total}개)를 모두 썼습니다. 시간이 지나야 다시 올릴 수 있습니다."))
        elif left <= 3:
            out.append(warn(f"최근 24시간 안에 {used}개를 올렸습니다. {left}개 남았습니다."))
        elif not _has_error(out):
            out.append(ok(f"24시간 게시 한도 {used}/{total}개 사용 · {left}개 남음"))
    elif not _has_error(out):
        out.append(ok("게시 가능"))
    return out


# ── Facebook ────────────────────────────────────────────────
async def facebook(account: dict, job: dict, options: dict) -> list[Issue]:
    out: list[Issue] = []
    token = account.get("access_token")
    page_id = account.get("external_id")

    description = build_caption(job, include_title=False, limit=None)
    if len(description) > 5000:
        out.append(warn(f"설명이 {len(description)}자입니다. 5000자까지만 올라갑니다."))

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.get(
                f"{GRAPH}/{page_id}",
                params={"fields": "id,name,tasks", "access_token": token},
            )
    except httpx.HTTPError as exc:
        out.append(warn(f"페이스북 상태를 확인하지 못했습니다: {exc}"))
        return out

    if res.status_code >= 400:
        message = ((res.json() or {}).get("error") or {}).get("message") or res.text[:200]
        out.append(err(f"페이스북이 페이지 조회를 거부했습니다(로그인 만료일 수 있습니다): {message}"))
        return out

    data = res.json() or {}
    tasks = data.get("tasks") or []
    if tasks and "CREATE_CONTENT" not in tasks:
        out.append(err(
            f"'{data.get('name')}' 페이지에 글을 쓸 권한이 없습니다. "
            "페이지 관리자 권한(콘텐츠 만들기)이 필요합니다."
        ))
    elif not _has_error(out):
        out.append(ok(f"페이지 '{data.get('name') or account['name']}' 게시 가능"))
    return out


CHECKERS = {
    "youtube": youtube,
    "tiktok": tiktok,
    "instagram": instagram,
    "facebook": facebook,
}
