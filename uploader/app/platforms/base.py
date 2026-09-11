"""플랫폼 어댑터 공통 타입/헬퍼."""
import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

# 진행률 콜백: (0~100, 메시지)
Progress = Callable[[int, str], Awaitable[None]]


class PublishError(Exception):
    """플랫폼 API가 거부했거나 업로드가 실패했을 때.

    transient=True 면 플랫폼 쪽 일시 오류라 다시 시도해 볼 가치가 있다.
    """

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass
class PublishResult:
    remote_id: str | None = None
    url: str | None = None
    message: str = "업로드 완료"


CHUNK = 1024 * 1024


async def stream_file(
    path: str | Path,
    progress: Progress | None = None,
    *,
    start: int = 0,
    end: int | None = None,
    base_pct: int = 10,
    span_pct: int = 80,
    label: str = "업로드 중",
) -> AsyncIterator[bytes]:
    """파일을 청크로 흘려보내면서 진행률을 보고한다."""
    path = Path(path)
    total = (end if end is not None else path.stat().st_size) - start
    sent = 0
    last_pct = -1
    with path.open("rb") as fh:
        fh.seek(start)
        while sent < total:
            data = fh.read(min(CHUNK, total - sent))
            if not data:
                break
            sent += len(data)
            yield data
            if progress:
                pct = base_pct + int(span_pct * sent / max(total, 1))
                if pct != last_pct:
                    last_pct = pct
                    await progress(pct, f"{label} ({sent * 100 // max(total, 1)}%)")
            await asyncio.sleep(0)


# 인스타그램 캡션당 해시태그 한도 (초과 시 code 36004 로 거부됨)
IG_MAX_HASHTAGS = 30

HASHTAG_RE = re.compile(r"#[^\s#]+")


def count_hashtags(text: str) -> int:
    return len(HASHTAG_RE.findall(text or ""))


def _trim_hashtags(text: str, keep: int) -> str:
    """텍스트 안의 해시태그를 앞에서부터 keep개만 남기고, 나머지는 '#'만 떼어낸다."""
    seen = 0

    def sub(m: "re.Match[str]") -> str:
        nonlocal seen
        seen += 1
        return m.group(0) if seen <= keep else m.group(0)[1:]

    return HASHTAG_RE.sub(sub, text or "")


def build_caption(
    job: dict,
    *,
    limit: int | None = None,
    include_title: bool = True,
    max_tags: int | None = None,
) -> str:
    """설명 + 해시태그를 합쳐 플랫폼 캡션을 만든다.

    max_tags를 주면 캡션 전체의 해시태그 개수를 그 값 이하로 맞춘다.
    (인스타그램은 캡션당 해시태그 30개를 넘으면 게시가 거부된다.)
    본문에 이미 들어 있는 해시태그를 먼저 세고, 남는 자리만큼만 태그 목록을 붙인다.
    """
    parts = []
    if include_title and job.get("title"):
        parts.append(job["title"])
    if job.get("description"):
        parts.append(job["description"])

    tag_list = [f"#{t.lstrip('#')}" for t in job.get("hashtags", []) if t.strip()]
    if max_tags is not None:
        used = sum(count_hashtags(p) for p in parts)
        room = max(max_tags - used, 0)
        tag_list = tag_list[:room]
        if used > max_tags:
            # 본문 자체가 이미 한도를 넘으면 초과분은 '#'을 떼어 일반 단어로 남긴다.
            budget = max_tags
            for i, part in enumerate(parts):
                n = count_hashtags(part)
                parts[i] = _trim_hashtags(part, min(n, budget))
                budget = max(budget - n, 0)

    tags = " ".join(tag_list)
    if tags:
        parts.append(tags)
    caption = "\n\n".join(parts).strip()
    if limit and len(caption) > limit:
        caption = caption[: limit - 1].rstrip() + "…"
    return caption


# 인스타그램이 우리 서버에서 영상을 내려받아 인코딩할 때까지 기다리는 시간(초).
# 큰 파일은 다운로드만으로도 몇 분이 걸리기 때문에 넉넉히 잡는다.
IG_PROCESS_BUDGET_SEC = 20 * 60


IG_PROCESS_BUDGET_MAX_SEC = 45 * 60


def ig_process_budget(size_bytes: int) -> int:
    """파일이 클수록 인스타가 내려받는 시간도 길어지므로 대기 시간을 늘려 잡는다."""
    mb = (size_bytes or 0) / 1024 / 1024
    return int(min(max(IG_PROCESS_BUDGET_SEC, mb * 3), IG_PROCESS_BUDGET_MAX_SEC))


def _size_hint(size_bytes: int) -> str:
    if not size_bytes:
        return ""
    mb = size_bytes / 1024 / 1024
    if mb < 300:
        return ""
    return (
        f" 영상이 {mb:.0f}MB라 인스타그램이 내려받는 데 시간이 오래 걸립니다. "
        "잠시 뒤 다시 시도하면 대개 성공합니다."
    )


async def wait_for_ig_container(
    client,
    graph: str,
    container_id: str,
    token: str,
    progress: Progress,
    *,
    size_bytes: int = 0,
    budget_sec: int | None = None,
) -> None:
    """컨테이너가 FINISHED 가 될 때까지 기다린다. 실패하면 PublishError."""
    if budget_sec is None:
        budget_sec = ig_process_budget(size_bytes)
    started = time.monotonic()
    last_code = None
    delay = 5
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= budget_sec:
            raise PublishError(
                f"Instagram이 {int(budget_sec) // 60}분 안에 영상 처리를 끝내지 못했습니다"
                f"(마지막 상태: {last_code or '응답 없음'})."
                + (_size_hint(size_bytes) or " 잠시 후 다시 시도해 보세요."),
                transient=True,
            )
        await asyncio.sleep(delay)
        # 처음에는 자주, 이후에는 뜸하게 확인한다.
        if time.monotonic() - started > 120:
            delay = 15

        res = await client.get(
            f"{graph}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
        )
        body = res.json() if res.status_code < 400 else {}
        last_code = body.get("status_code") or last_code
        mins = int((time.monotonic() - started) // 60)
        pct = min(15 + int((time.monotonic() - started) / budget_sec * 70), 85)
        await progress(
            pct,
            f"Instagram 처리 중 ({last_code or '대기'}"
            + (f" · {mins}분 경과" if mins else "") + ")",
        )
        if last_code == "FINISHED":
            return
        if last_code in ("ERROR", "EXPIRED"):
            detail = body.get("status") or last_code
            raise PublishError(
                f"Instagram 영상 처리 실패: {detail}", transient=(last_code == "ERROR")
            )


def multipart_body(
    fields: dict[str, str],
    file_field: str,
    file_path: str | Path,
    file_name: str,
    content_type: str = "video/mp4",
    boundary: str | None = None,
) -> tuple[str, int, Callable[[Progress | None], AsyncIterator[bytes]]]:
    """스트리밍 가능한 multipart/form-data 바디를 만든다.

    반환값: (boundary, content_length, body_factory)
    """
    import uuid

    boundary = boundary or f"----uploader{uuid.uuid4().hex}"
    file_path = Path(file_path)
    file_size = file_path.stat().st_size

    head = b""
    for key, value in fields.items():
        head += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
        ).encode()
    head += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    length = len(head) + file_size + len(tail)

    def factory(progress: Progress | None) -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            yield head
            async for chunk in stream_file(file_path, progress, base_pct=8, span_pct=80):
                yield chunk
            yield tail

        return gen()

    return boundary, length, factory


# ── 인스타그램 resumable 업로드 ──────────────────────────────
# 기본(pull) 방식은 인스타그램이 우리 서버에 접속해 영상을 내려받는다.
# 큰 파일이면 인스타 쪽 대기열 + 다운로드 때문에 오래 걸리고 진행률도 알 수 없다.
# resumable 방식은 우리가 메타 서버로 직접 밀어넣어서 더 빠르고 진행률이 보인다.
RUPLOAD = "https://rupload.facebook.com/ig-api-upload"


async def ig_resumable_upload(
    client,
    api_version: str,
    container_id: str,
    token: str,
    job: dict,
    progress: Progress,
) -> None:
    """영상 바이트를 메타 업로드 서버로 직접 전송한다. 실패하면 PublishError."""
    size = job["video_size"]
    res = await client.post(
        f"{RUPLOAD}/{api_version}/{container_id}",
        headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(size),
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
        },
        content=stream_file(
            job["video_path"], progress, base_pct=10, span_pct=55, label="Instagram 전송 중"
        ),
    )
    if res.status_code >= 400:
        raise PublishError(
            f"Instagram 전송 실패: {res.text[:300]}", transient=ig_is_transient(res.text)
        )
    body = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
    if body and body.get("success") is False:
        raise PublishError(f"Instagram 전송 실패: {res.text[:300]}")


# 어떤 방식이 되는지 한 번 확인하면 기억해 둔다 ("resumable" | "pull").
IG_MODE_KEY = "ig_upload_mode"

# 인스타그램 쪽 일시 오류 — 같은 영상이라도 다시 하면 되는 경우가 많다.
# 2207085 "내부 서버 오류가 발생했습니다. 나중에 다시 시도해주세요."
IG_TRANSIENT_MARKERS = ("2207085", "2207001", "2207003", '"is_transient":true', "please retry")
IG_PUBLISH_TRIES = 3
# 컨테이너는 실패하면 영상을 다시 보내야 해서(대용량이면 비싸다) 한 번만 더 시도한다.
IG_CONTAINER_TRIES = 2


def ig_is_transient(text: str) -> bool:
    low = (text or "").lower()
    return any(m.lower() in low for m in IG_TRANSIENT_MARKERS)


async def ig_create_container(
    client,
    base: str,
    api_version: str,
    ig_user_id: str,
    token: str,
    params: dict,
    job: dict,
    progress: Progress,
    video_url: str,
) -> str:
    """릴스 컨테이너를 만든다.

    가능하면 resumable(직접 전송) 방식을 쓰고, 안 되면 기존 URL(pull) 방식으로 돌아간다.
    한 번 판별한 결과는 저장해서 다음부터는 헛걸음하지 않는다.
    """
    from .. import db

    if (db.get_setting(IG_MODE_KEY) or "") != "pull":
        # 1단계: 세션만 먼저 열어 본다. 여기서 실패해야 pull 로 돌아갈 수 있다.
        #        (영상 전송을 시작한 뒤에 폴백하면 같은 파일을 두 번 보내게 된다.)
        container_id = None
        unsupported = False   # 계정/앱이 이 방식을 아예 안 받는 경우에만 기억한다
        try:
            await progress(6, "Instagram 업로드 세션 생성 중")
            res = await client.post(
                f"{base}/{ig_user_id}/media",
                params={**params, "upload_type": "resumable", "access_token": token},
            )
            container_id = (res.json() or {}).get("id") if res.status_code < 400 else None
            unsupported = not container_id and 400 <= res.status_code < 500
        except Exception:  # 네트워크 오류 — 이번만 기존 방식으로 넘어간다
            pass

        if container_id:
            # 2단계: 전송 시작. 여기서 실패하면 그대로 알린다 (재전송하지 않는다).
            await ig_resumable_upload(client, api_version, container_id, token, job, progress)
            db.set_setting(IG_MODE_KEY, "resumable")
            return container_id

        if unsupported:
            db.set_setting(IG_MODE_KEY, "pull")
        await progress(8, "직접 전송이 안 돼 기존 방식으로 전환합니다")

    if not video_url.startswith("https://"):
        raise PublishError(
            "Instagram이 이 서버에서 영상을 내려받아야 하는데 공개 주소가 아닙니다"
            f"(현재: {video_url}). PUBLIC_BASE_URL 을 외부에서 접근 가능한 https 주소로 설정하세요."
        )
    res = await client.post(
        f"{base}/{ig_user_id}/media",
        params={**params, "video_url": video_url, "access_token": token},
    )
    if res.status_code >= 400:
        raise PublishError(
            f"Instagram 컨테이너 생성 실패: {res.text[:300]}",
            transient=ig_is_transient(res.text),
        )
    container_id = (res.json() or {}).get("id")
    if not container_id:
        raise PublishError("Instagram이 컨테이너 ID를 반환하지 않았습니다.")
    return container_id


async def ig_publish_with_retry(
    client,
    base: str,
    api_version: str,
    ig_user_id: str,
    token: str,
    params: dict,
    job: dict,
    progress: Progress,
    video_url: str,
) -> str:
    """컨테이너를 만들고 처리가 끝날 때까지 기다린다.

    인스타그램 쪽 일시 오류(내부 서버 오류, 인코딩 실패)는 다시 하면 되는 경우가
    많아서 몇 번 재시도한다. 거부 사유가 분명한 오류는 곧바로 알린다.
    """
    last: PublishError | None = None
    for attempt in range(IG_CONTAINER_TRIES):
        if attempt:
            wait = 15 * attempt
            await progress(
                4,
                f"Instagram 일시 오류 — {wait}초 뒤 바로 다시 시도합니다 "
                f"({attempt + 1}/{IG_CONTAINER_TRIES})",
            )
            await asyncio.sleep(wait)
        try:
            container_id = await ig_create_container(
                client, base, api_version, ig_user_id, token, params, job, progress, video_url,
            )
            await wait_for_ig_container(
                client, base, container_id, token, progress,
                size_bytes=job.get("video_size") or 0,
            )
            return container_id
        except PublishError as exc:
            if not getattr(exc, "transient", False):
                raise
            last = exc
    raise PublishError(
        f"{last} (바로 {IG_CONTAINER_TRIES}번 다시 시도했지만 같은 오류였습니다.)",
        transient=True,
    )


async def ig_recent_media(
    client, base: str, ig_user_id: str, token: str, caption: str
) -> tuple[str | None, bool]:
    """방금 올린 릴스가 실제로는 게시됐는지 최근 게시물에서 찾는다.

    2207085 같은 오류는 게시가 끝난 뒤 응답만 실패하는 경우가 있어서,
    그대로 실패 처리하면 사용자가 다시 올려 같은 영상이 두 번 올라간다.

    (찾은 게시물 id, 확인에 성공했는지) 를 돌려준다.
    """
    head = re.sub(r"\s+", " ", (caption or "").strip())[:40]
    try:
        res = await client.get(
            f"{base}/{ig_user_id}/media",
            params={
                "fields": "id,caption,timestamp,media_type",
                "limit": 5,
                "access_token": token,
            },
        )
    except Exception:
        return None, False
    if res.status_code >= 400:
        return None, False

    cutoff = time.time() - 30 * 60
    for item in (res.json() or {}).get("data") or []:
        item_caption = re.sub(r"\s+", " ", (item.get("caption") or "").strip())
        if head and item_caption.startswith(head):
            return item.get("id"), True
        stamp = item.get("timestamp") or ""
        try:  # 2026-09-11T09:00:00+0000
            posted = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        if posted > cutoff and not head:
            return item.get("id"), True
    return None, True


async def ig_media_publish(
    client,
    base: str,
    ig_user_id: str,
    container_id: str,
    token: str,
    progress: Progress,
    caption: str = "",
) -> tuple[str, str]:
    """처리가 끝난 컨테이너를 실제로 게시한다. (게시물 id, 안내 메시지) 반환.

    영상은 이미 인스타그램에 올라가 있으므로 재시도 비용이 없다.
    일시 오류로 여기서 실패하는 경우가 잦아 여러 번 시도하고,
    그래도 안 되면 실제로 올라갔는지 계정의 최근 게시물에서 확인한다.
    """
    last = ""
    for attempt in range(IG_PUBLISH_TRIES):
        if attempt:
            wait = 10 * attempt
            await progress(
                92,
                f"Instagram 바로 다시 시도 중 ({attempt + 1}/{IG_PUBLISH_TRIES}) — {wait}초 대기",
            )
            await asyncio.sleep(wait)
        res = await client.post(
            f"{base}/{ig_user_id}/media_publish",
            params={"creation_id": container_id, "access_token": token},
        )
        if res.status_code < 400:
            media_id = (res.json() or {}).get("id")
            if media_id:
                return media_id, ""
            last = "인스타그램이 게시물 ID를 돌려주지 않았습니다."
            continue
        last = res.text[:300]
        if not ig_is_transient(res.text):
            raise PublishError(f"Instagram 게시 실패: {last}")

    # 오류는 났지만 실제로 올라갔을 수 있다 — 확인하고 성공 처리한다.
    await progress(95, "게시됐는지 확인하는 중")
    found, checked = await ig_recent_media(client, base, ig_user_id, token, caption)
    if found:
        return found, "인스타그램이 오류를 냈지만 게시물은 정상적으로 올라갔습니다."

    tail = (
        "계정에도 올라가지 않은 것을 확인했습니다."
        if checked
        else "실제로 올라갔는지는 확인하지 못했습니다(중복 게시 주의)."
    )
    raise PublishError(
        f"Instagram 게시 실패: {last} "
        f"(바로 {IG_PUBLISH_TRIES}번 다시 시도했지만 같은 오류였습니다. {tail})",
        transient=checked,   # 확인 결과 안 올라갔을 때만 자동 재시도한다
    )
