"""멀티 플랫폼 업로더 — YouTube / TikTok / Instagram / Facebook 동시 업로드."""
import asyncio
import contextlib
import mimetypes
import os
import re
import secrets
import shutil
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, jobs
from .config import (
    BASE_DIR,
    MAX_UPLOAD_BYTES,
    PLATFORM_ORDER,
    PLATFORMS,
    PUBLIC_BASE_URL,
    UPLOAD_DIR,
    demo_platform,
)
from .platforms import meta, tiktok, youtube
from .platforms.base import PublishError

STATIC_DIR = BASE_DIR / "static"

@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    db.connect()
    jobs.cleanup_old_files()
    yield


app = FastAPI(title="멀티 플랫폼 업로더", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# provider → (auth_url, exchange_code)
PROVIDERS = {
    "youtube": (youtube.auth_url, youtube.exchange_code),
    "tiktok": (tiktok.auth_url, tiktok.exchange_code),
    "meta": (meta.auth_url, meta.exchange_code),
}


# ── 정적 파일 ────────────────────────────────────────────────
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ── 플랫폼 / 계정 ────────────────────────────────────────────
@app.get("/api/platforms")
async def get_platforms() -> dict:
    accounts = db.list_accounts()
    out = []
    for key in PLATFORM_ORDER:
        cfg = PLATFORMS[key]
        out.append({
            "key": key,
            "label": cfg.label,
            "color": cfg.color,
            "provider": cfg.provider,
            "configured": cfg.configured,
            "demo": demo_platform(key),
            "redirect_uri": cfg.redirect_uri,
            "accounts": [a for a in accounts if a["platform"] == key],
        })
    return {"platforms": out, "public_base_url": PUBLIC_BASE_URL}


@app.get("/api/oauth/{platform}/start")
async def oauth_start(platform: str):
    if platform not in PLATFORMS:
        raise HTTPException(404, "알 수 없는 플랫폼")
    cfg = PLATFORMS[platform]

    if demo_platform(platform):
        _create_demo_accounts(platform)
        return RedirectResponse(f"/?connected={platform}&demo=1", status_code=303)

    state = secrets.token_urlsafe(24)
    db.save_state(state, cfg.provider)
    return RedirectResponse(PROVIDERS[cfg.provider][0](state), status_code=303)


@app.get("/api/oauth/{provider}/callback")
async def oauth_callback(provider: str, request: Request):
    if provider not in PROVIDERS:
        raise HTTPException(404, "알 수 없는 인증 공급자")
    params = request.query_params
    if params.get("error"):
        detail = params.get("error_description") or params.get("error")
        return RedirectResponse(f"/?error={detail}", status_code=303)

    state = params.get("state") or ""
    if db.pop_state(state) != provider:
        return RedirectResponse("/?error=인증 state가 유효하지 않습니다. 다시 시도하세요.", status_code=303)

    code = params.get("code")
    if not code:
        return RedirectResponse("/?error=인가 코드가 없습니다.", status_code=303)

    try:
        await PROVIDERS[provider][1](code)
    except PublishError as exc:
        return RedirectResponse(f"/?error={exc}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"/?error={type(exc).__name__}: {exc}", status_code=303)
    return RedirectResponse(f"/?connected={provider}", status_code=303)


class AccountPatch(BaseModel):
    label: str = ""


@app.patch("/api/accounts/{account_id}")
async def rename_account(account_id: str, payload: AccountPatch) -> dict:
    if not db.get_account(account_id, with_tokens=False):
        raise HTTPException(404, "계정을 찾을 수 없습니다.")
    db.set_account_label(account_id, payload.label.strip()[:60])
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
async def disconnect(account_id: str) -> dict:
    if not db.get_account(account_id, with_tokens=False):
        raise HTTPException(404, "계정을 찾을 수 없습니다.")
    db.delete_account(account_id)
    return {"ok": True}


def _create_demo_accounts(platform: str) -> None:
    """API 키 없이도 화면을 테스트할 수 있도록 가짜 계정을 만든다.

    누를 때마다 새 계정이 하나씩 추가되므로 다중 계정 운영도 미리 확인할 수 있다.
    """
    demo_names = {
        "youtube": ("데모 채널", "UCdemo"),
        "tiktok": ("demo_creator", "tt_demo"),
        "instagram": ("demo.official", "ig_demo"),
        "facebook": ("데모 페이지", "fb_demo"),
    }
    name, external = demo_names[platform]
    n = db.count_accounts(platform) + 1
    db.upsert_account(
        platform, f"{external}_{n}", f"{name} {n}", access_token="demo", meta={"demo": True}
    )


# ── 채널 세트 ────────────────────────────────────────────────
class SetIn(BaseModel):
    name: str = ""
    account_ids: list[str] = Field(default_factory=list)


def _clean_set(payload: SetIn) -> tuple[str, list[str]]:
    name = payload.name.strip()[:40]
    if not name:
        raise HTTPException(400, "세트 이름을 입력하세요.")
    ids, seen = [], set()
    for account_id in payload.account_ids:
        if account_id in seen:
            continue
        if not db.get_account(account_id, with_tokens=False):
            raise HTTPException(400, "세트에 담을 수 없는 계정이 있습니다. 목록을 새로고침하세요.")
        seen.add(account_id)
        ids.append(account_id)
    if not ids:
        raise HTTPException(400, "세트에 넣을 계정을 1개 이상 선택하세요.")
    return name, ids


@app.get("/api/sets")
async def get_sets() -> dict:
    return {"sets": db.list_sets()}


@app.post("/api/sets")
async def create_set(payload: SetIn) -> dict:
    name, ids = _clean_set(payload)
    return {"id": db.create_set(name, ids)}


@app.patch("/api/sets/{set_id}")
async def update_set(set_id: str, payload: SetIn) -> dict:
    if not db.get_set(set_id):
        raise HTTPException(404, "세트를 찾을 수 없습니다.")
    name, ids = _clean_set(payload)
    db.update_set(set_id, name=name, account_ids=ids)
    return {"ok": True}


@app.delete("/api/sets/{set_id}")
async def remove_set(set_id: str) -> dict:
    if not db.get_set(set_id):
        raise HTTPException(404, "세트를 찾을 수 없습니다.")
    db.delete_set(set_id)
    return {"ok": True}


# ── 미디어 업로드 ────────────────────────────────────────────
@app.post("/api/media")
async def upload_media(file: UploadFile) -> dict:
    if not file.filename:
        raise HTTPException(400, "파일명이 없습니다.")
    content_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "video/mp4"
    if not content_type.startswith("video/"):
        raise HTTPException(400, "영상 파일만 업로드할 수 있습니다.")

    suffix = Path(file.filename).suffix[:10] or ".mp4"
    dest = Path(UPLOAD_DIR) / f"{secrets.token_hex(12)}{suffix}"
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "파일이 너무 큽니다.")
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    media = db.create_media(
        path=str(dest), name=file.filename, size=size, content_type=content_type
    )
    return {
        "id": media["id"],
        "name": media["name"],
        "size": media["size"],
        "content_type": media["content_type"],
        "preview_url": f"/media/{media['token']}",
    }


@app.post("/api/media/{media_id}/thumbnail")
async def upload_thumbnail(media_id: str, file: UploadFile) -> dict:
    media = db.get_media(media_id)
    if not media:
        raise HTTPException(404, "미디어를 찾을 수 없습니다.")
    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(400, "이미지 파일만 업로드할 수 있습니다.")
    suffix = Path(file.filename or "thumb.jpg").suffix[:10] or ".jpg"
    dest = Path(UPLOAD_DIR) / f"{media_id}_thumb{suffix}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    db.set_media_thumb(media_id, str(dest))
    return {"ok": True}


RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


@app.get("/media/{token}")
async def serve_media(token: str, request: Request):
    """업로드된 영상을 공개 제공한다(Instagram이 이 URL로 영상을 가져간다)."""
    media = db.get_media_by_token(token)
    if not media:
        job = db.find_job_by_media_token(token)
        media = (
            {"path": job["video_path"], "content_type": "video/mp4", "size": job["video_size"]}
            if job else None
        )
    if not media or not os.path.exists(media["path"]):
        raise HTTPException(404, "미디어를 찾을 수 없습니다.")

    path = Path(media["path"])
    total = path.stat().st_size
    content_type = media.get("content_type") or "video/mp4"
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(
            path,
            media_type=content_type,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(total)},
        )

    match = RANGE_RE.fullmatch(range_header.strip())
    if not match:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
    start = int(match.group(1)) if match.group(1) else 0
    end = int(match.group(2)) if match.group(2) else total - 1
    end = min(end, total - 1)
    if start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})

    def iter_range():
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = fh.read(min(1024 * 1024, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(
        iter_range(),
        status_code=206,
        media_type=content_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        },
    )


# ── 업로드 작업 ──────────────────────────────────────────────
class TargetIn(BaseModel):
    platform: str
    account_id: str


class JobIn(BaseModel):
    media_id: str
    title: str = ""
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    targets: list[TargetIn]
    options: dict = Field(default_factory=dict)


@app.post("/api/jobs")
async def create_job(payload: JobIn, background: BackgroundTasks) -> dict:
    media = db.get_media(payload.media_id)
    if not media or not os.path.exists(media["path"]):
        raise HTTPException(404, "업로드된 영상을 찾을 수 없습니다. 영상을 다시 올려주세요.")
    if not payload.targets:
        raise HTTPException(400, "게시할 플랫폼을 1개 이상 선택하세요.")

    for target in payload.targets:
        if target.platform not in PLATFORMS:
            raise HTTPException(400, f"알 수 없는 플랫폼: {target.platform}")
        account = db.get_account(target.account_id, with_tokens=False)
        if not account or account["platform"] != target.platform:
            raise HTTPException(400, f"{PLATFORMS[target.platform].label} 계정이 연결되어 있지 않습니다.")

    job_id = db.create_job(
        title=payload.title.strip(),
        description=payload.description.strip(),
        hashtags=[t.strip().lstrip("#") for t in payload.hashtags if t.strip()],
        video_path=media["path"],
        video_name=media["name"],
        video_size=media["size"],
        thumb_path=media.get("thumb_path"),
        media_token=media["token"],
        options=payload.options,
    )
    for target in payload.targets:
        db.add_target(job_id, target.platform, target.account_id)

    background.add_task(_launch, job_id)
    return {"job_id": job_id}


async def _launch(job_id: str) -> None:
    # 백그라운드에서 게시를 진행하고, 응답은 즉시 반환한다.
    await asyncio.shield(jobs.run_job(job_id))


@app.get("/api/jobs")
async def get_jobs(limit: int = 30) -> dict:
    return {"jobs": db.list_jobs(limit)}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    job.pop("video_path", None)
    return job


@app.exception_handler(PublishError)
async def publish_error_handler(_: Request, exc: PublishError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=400)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def run() -> None:
    import uvicorn

    from .config import PORT

    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=False)


if __name__ == "__main__":
    run()
