"""Facebook 페이지 영상 게시 (Graph API)."""
import httpx

from ..config import GRAPH, GRAPH_VIDEO
from .base import Progress, PublishError, PublishResult, build_caption, multipart_body

# 다시 하면 되는 페이스북 오류들.
#  1 = An unknown error occurred / 2 = Service temporarily unavailable
#  4, 17, 32, 613 = 요청량 제한 / 1363030, 1363019 = 영상 처리 일시 실패
TRANSIENT_CODES = {1, 2, 4, 17, 32, 613}
TRANSIENT_SUBCODES = {1363030, 1363019, 1363037}
AUTH_CODES = {102, 190, 463, 467}


def _explain(res) -> PublishError:
    """페이스북 응답을 읽을 수 있는 메시지로 바꾼다."""
    try:
        error = (res.json() or {}).get("error") or {}
    except ValueError:
        error = {}
    code = error.get("code")
    subcode = error.get("error_subcode")
    message = (
        error.get("error_user_msg")
        or error.get("message")
        or res.text[:300]
    )
    transient = code in TRANSIENT_CODES or subcode in TRANSIENT_SUBCODES

    if code in AUTH_CODES:
        return PublishError(
            f"페이스북 로그인이 만료됐습니다. 계정 관리에서 다시 연결하세요. ({message})"
        )
    if code == 200 or subcode == 1363047:
        return PublishError(
            f"이 페이지에 영상을 올릴 권한이 없습니다. 페이지 관리자 권한을 확인하세요. ({message})"
        )
    tail = f" [code {code}" + (f"/{subcode}" if subcode else "") + "]" if code else ""
    return PublishError(f"Facebook 업로드 실패: {message}{tail}", transient=transient)


async def publish(account: dict, job: dict, options: dict, progress: Progress) -> PublishResult:
    token = account["access_token"]
    page_id = account["external_id"]
    title = (options.get("title") or job["title"] or "")[:255]
    description = build_caption(job, include_title=False, limit=5000)

    fields = {"access_token": token, "description": description}
    if title:
        fields["title"] = title
    if options.get("published") is False:
        fields["published"] = "false"

    boundary, length, factory = multipart_body(
        fields, "source", job["video_path"], job["video_name"]
    )

    await progress(5, "Facebook 업로드 시작")
    async with httpx.AsyncClient(timeout=None) as client:
        res = await client.post(
            f"{GRAPH_VIDEO}/{page_id}/videos",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(length),
            },
            content=factory(progress),
        )
        if res.status_code >= 400:
            raise _explain(res)
        video_id = (res.json() or {}).get("id")

        url = f"https://www.facebook.com/{video_id}" if video_id else None
        if video_id:
            await progress(95, "게시물 링크 확인 중")
            link = await client.get(
                f"{GRAPH}/{video_id}",
                params={"fields": "permalink_url", "access_token": token},
            )
            if link.status_code < 400:
                permalink = (link.json() or {}).get("permalink_url")
                if permalink:
                    url = f"https://www.facebook.com{permalink}" if permalink.startswith("/") else permalink

    return PublishResult(remote_id=video_id, url=url, message=f"{account['name']} 페이지에 게시 완료")
