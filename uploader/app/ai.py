"""제목·설명·해시태그를 영어로 옮긴다 (유튜브 현지화용).

API 키는 계정 관리 화면에서 입력받아 암호화 보관하고 ANTHROPIC_API_KEY 환경변수도 지원한다.
키가 없으면 이 기능만 꺼지고 나머지 동작에는 영향이 없다.
"""
import json
import os

import anthropic

from . import db
from .config import decrypt, encrypt

KEY_SETTING = "cred:anthropic:api_key"
MODEL = "claude-opus-5"

SYSTEM = """한국 소셜미디어 채널의 영상 정보를 영어권 시청자용으로 옮기는 작업입니다.

- 제목: 직역하지 말고 영어권에서 자연스럽게 읽히도록 다시 씁니다. 원문의 의미와 후킹 포인트는 유지하세요.
  100자를 넘기지 마세요. 핵심 키워드를 앞쪽에 두세요.
- 설명: 같은 내용을 자연스러운 영어로. 문단 구조는 원문을 따르세요.
- 해시태그: **직역하지 마세요.** 영어권에서 실제로 쓰이는 태그로 바꿉니다.
  (예: '자취요리' → 'easyrecipe', 'quickmeal' / '먹방' → 'mukbang', 'foodie')
  소문자, 띄어쓰기 없이. 개수는 원문과 비슷하게, 최대 15개.
- 고유명사(채널명, 인명, 브랜드)는 그대로 두거나 통용되는 표기를 씁니다.
- 원문에 없는 내용을 지어내지 마세요.
- 이모지는 넣지 마세요."""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "영어 제목 (100자 이하)"},
        "description": {"type": "string", "description": "영어 설명"},
        "hashtags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "영어 해시태그 (# 없이, 소문자)",
        },
    },
    "required": ["title", "description", "hashtags"],
    "additionalProperties": False,
}


def api_key() -> str:
    """앱에 저장한 키가 우선, 없으면 환경변수."""
    return decrypt(db.get_setting(KEY_SETTING) or "") or (os.getenv("ANTHROPIC_API_KEY") or "")


def save_api_key(key: str) -> None:
    db.set_setting(KEY_SETTING, encrypt(key.strip()))


def clear_api_key() -> None:
    db.delete_setting(KEY_SETTING)


def enabled() -> bool:
    return bool(api_key())


async def translate(title: str, description: str, hashtags: list[str]) -> dict:
    """한국어 제목/설명/해시태그를 영어로 옮긴다."""
    key = api_key()
    if not key:
        raise RuntimeError("Anthropic API 키가 없습니다.")

    parts = [f"제목: {title}"]
    if description:
        parts.append(f"설명:\n{description[:3000]}")
    if hashtags:
        parts.append(f"해시태그: {', '.join(h.lstrip('#') for h in hashtags[:20])}")

    client = anthropic.AsyncAnthropic(api_key=key)
    response = await client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        messages=[{"role": "user", "content": "\n\n".join(parts)}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("요청이 거부되었습니다. 내용을 바꿔 다시 시도해 주세요.")

    text = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(text or "{}")
    return {
        "title": (data.get("title") or "")[:100],
        "description": data.get("description") or "",
        "hashtags": [h.lstrip("#").strip() for h in (data.get("hashtags") or []) if h.strip()][:15],
    }
