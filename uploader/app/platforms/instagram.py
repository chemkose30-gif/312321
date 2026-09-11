"""Instagram 릴스 게시 (Graph API 컨테이너 방식).

전송 방식은 두 가지이고, 되는 쪽을 자동으로 골라 기억한다.
- resumable : 우리가 메타 업로드 서버로 파일을 직접 밀어넣는다. 더 빠르고 진행률이 보인다.
- pull      : 인스타그램이 {PUBLIC_BASE_URL}/media/{token} 에 접속해 직접 내려받는다.
              이 방식일 때만 PUBLIC_BASE_URL 이 외부 접근 가능한 https 주소여야 한다.
"""

import httpx

from ..config import GRAPH, META_API_VERSION, PUBLIC_BASE_URL
from .base import (
    IG_MAX_HASHTAGS,
    Progress,
    PublishError,
    PublishResult,
    build_caption,
    ig_media_publish,
    ig_publish_with_retry,
)


def media_url(job: dict) -> str:
    return f"{PUBLIC_BASE_URL}/media/{job['media_token']}"


async def publish(account: dict, job: dict, options: dict, progress: Progress) -> PublishResult:
    token = account["access_token"]
    ig_user_id = account["external_id"]
    video_url = media_url(job)

    params = {
        "media_type": "REELS",
        "caption": build_caption(job, limit=2200, max_tags=IG_MAX_HASHTAGS),
        "share_to_feed": "true" if options.get("share_to_feed", True) else "false",
    }

    async with httpx.AsyncClient(timeout=None) as client:
        container_id = await ig_publish_with_retry(
            client, GRAPH, META_API_VERSION, ig_user_id, token, params, job, progress, video_url,
        )

        await progress(92, "게시 중")
        media_id = await ig_media_publish(
            client, GRAPH, ig_user_id, container_id, token, progress,
        )

        url = None
        if media_id:
            link = await client.get(
                f"{GRAPH}/{media_id}", params={"fields": "permalink", "access_token": token}
            )
            if link.status_code < 400:
                url = (link.json() or {}).get("permalink")

    return PublishResult(remote_id=media_id, url=url, message=f"@{account['name']} 릴스 게시 완료")
