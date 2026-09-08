"""플랫폼 어댑터 공통 타입/헬퍼."""
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

# 진행률 콜백: (0~100, 메시지)
Progress = Callable[[int, str], Awaitable[None]]


class PublishError(Exception):
    """플랫폼 API가 거부했거나 업로드가 실패했을 때."""


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


def build_caption(job: dict, *, limit: int | None = None, include_title: bool = True) -> str:
    """설명 + 해시태그를 합쳐 플랫폼 캡션을 만든다."""
    parts = []
    if include_title and job.get("title"):
        parts.append(job["title"])
    if job.get("description"):
        parts.append(job["description"])
    tags = " ".join(f"#{t.lstrip('#')}" for t in job.get("hashtags", []) if t.strip())
    if tags:
        parts.append(tags)
    caption = "\n\n".join(parts).strip()
    if limit and len(caption) > limit:
        caption = caption[: limit - 1].rstrip() + "…"
    return caption


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
