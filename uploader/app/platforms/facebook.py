"""Facebook 페이지 영상 게시 (Graph API)."""
import httpx

from ..config import GRAPH, GRAPH_VIDEO
from .base import Progress, PublishError, PublishResult, build_caption, multipart_body


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
            raise PublishError(f"Facebook 업로드 실패: {res.text}")
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
