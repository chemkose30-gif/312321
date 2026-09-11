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
    status = res.status_code
    message = error.get("error_user_msg") or error.get("message") or (res.text or "").strip()[:300]

    # 본문 없이 실패하는 경우가 있어(프록시 거절, 서버 오류) 상태 코드로 설명한다.
    if not message:
        message = {
            408: "요청 시간이 초과됐습니다.",
            413: "영상 용량이 너무 커서 거부됐습니다.",
            429: "요청이 너무 잦아 잠시 막혔습니다.",
            500: "페이스북 서버 오류입니다.",
            502: "페이스북 게이트웨이 오류입니다.",
            503: "페이스북이 일시적으로 응답하지 않습니다.",
            504: "페이스북 응답이 시간 초과됐습니다.",
        }.get(status, "페이스북이 내용 없이 거부했습니다.")
        message = f"{message} (HTTP {status}, 응답 본문 없음)"

    transient = (
        code in TRANSIENT_CODES
        or subcode in TRANSIENT_SUBCODES
        or status in (408, 429, 500, 502, 503, 504)
    )

    if code in AUTH_CODES:
        return PublishError(
            f"페이스북 로그인이 만료됐습니다. 계정 관리에서 다시 연결하세요. ({message})"
        )
    if code == 200 or subcode == 1363047:
        return PublishError(
            f"이 페이지에 영상을 올릴 권한이 없습니다. 페이지 관리자 권한을 확인하세요. ({message})"
        )
    if code:
        tail = f" [code {code}" + (f"/{subcode}" if subcode else "") + f", HTTP {status}]"
    elif "HTTP" in message:
        tail = ""          # 이미 상태 코드를 문구에 넣었다
    else:
        tail = f" [HTTP {status}]"
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
