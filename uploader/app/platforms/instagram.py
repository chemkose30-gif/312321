"""Instagram 릴스 게시 (Graph API 컨테이너 방식).

Instagram은 파일 업로드를 받지 않고 '공개적으로 접근 가능한 영상 URL'만 받는다.
그래서 이 앱은 업로드된 파일을 {PUBLIC_BASE_URL}/media/{token} 으로 임시 공개한다.
로컬에서 테스트할 때는 PUBLIC_BASE_URL이 외부에서 접근 가능한 주소여야 한다.
"""

import httpx

from ..config import GRAPH, PUBLIC_BASE_URL
from .base import (
    IG_MAX_HASHTAGS,
    Progress,
    PublishError,
    PublishResult,
    build_caption,
    wait_for_ig_container,
)


def media_url(job: dict) -> str:
    return f"{PUBLIC_BASE_URL}/media/{job['media_token']}"


async def publish(account: dict, job: dict, options: dict, progress: Progress) -> PublishResult:
    token = account["access_token"]
    ig_user_id = account["external_id"]
    video_url = media_url(job)

    if PUBLIC_BASE_URL.startswith("http://localhost") or PUBLIC_BASE_URL.startswith("http://127."):
        raise PublishError(
            "Instagram은 외부에서 접근 가능한 영상 URL이 필요합니다. "
            ".env의 PUBLIC_BASE_URL을 공개 주소(예: ngrok https 주소)로 설정하세요."
        )

    caption = build_caption(job, limit=2200, max_tags=IG_MAX_HASHTAGS)
    params = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "share_to_feed": "true" if options.get("share_to_feed", True) else "false",
        "access_token": token,
    }

    await progress(8, "릴스 컨테이너 생성 중")
    async with httpx.AsyncClient(timeout=None) as client:
        create = await client.post(f"{GRAPH}/{ig_user_id}/media", params=params)
        if create.status_code >= 400:
            raise PublishError(f"Instagram 컨테이너 생성 실패: {create.text}")
        container_id = (create.json() or {}).get("id")
        if not container_id:
            raise PublishError("Instagram이 컨테이너 ID를 반환하지 않았습니다.")

        await wait_for_ig_container(
            client, GRAPH, container_id, token, progress,
            size_bytes=job.get("video_size") or 0,
        )

        await progress(92, "게시 중")
        publish_res = await client.post(
            f"{GRAPH}/{ig_user_id}/media_publish",
            params={"creation_id": container_id, "access_token": token},
        )
        if publish_res.status_code >= 400:
            raise PublishError(f"Instagram 게시 실패: {publish_res.text}")
        media_id = (publish_res.json() or {}).get("id")

        url = None
        if media_id:
            link = await client.get(
                f"{GRAPH}/{media_id}", params={"fields": "permalink", "access_token": token}
            )
            if link.status_code < 400:
                url = (link.json() or {}).get("permalink")

    return PublishResult(remote_id=media_id, url=url, message=f"@{account['name']} 릴스 게시 완료")
