"""Claude 로 메일 읽기: 해외(수입) 메일 한국어 번역 + 일정 후보 찾기.

TEAMHUB_ANTHROPIC_API_KEY (또는 ANTHROPIC_API_KEY) 가 있을 때만 쓴다. 없으면 mailin.py 의 규칙 방식만 쓴다.
번역할 때 제조사 제품명(예: Pineapple Flavouring Natural F13865, Hedione HC, Damascenone 937450)은 원문 그대로 둔다.
"""
import json
import os
import re

MODEL = os.getenv("TEAMHUB_AI_MODEL", "claude-opus-5-5")
API_KEY = os.getenv("TEAMHUB_ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or ""

SYSTEM = """당신은 향료·화학 원료 수입 회사(이알씨)의 업무 비서입니다. 받은 메일 하나를 읽고 JSON 으로 답합니다.

1) translation: 메일이 한국어가 아니면(영어·중국어 등 해외 공급사·포워더 메일) 본문을 자연스러운 한국어 업무 문체로 번역합니다.
   - 제조사 제품명·품목명·제품 코드(예: "Pineapple Flavouring Natural F13865", "Hedione HC", "Damascenone 937450", "cis-3-Hexenyl acetate"),
     회사명, 사람 이름, B/L·컨테이너·인보이스 번호, 선박명, 항구명, 숫자·단위는 번역하지 말고 원문 그대로 둡니다.
   - 서명, 법적 고지문, 이전 메일 인용(회신 기록)은 번역하지 않고 생략합니다.
   - 메일이 이미 한국어면 빈 문자열("")로 둡니다.
2) schedules: 메일에 나온 앞으로의 일정만 뽑습니다 (지난 날짜, 인용된 예전 메일의 날짜, 메일 발송일은 제외).
   - kind: "ship"(입고·선적·도착·통관 — ETA/ETD 등), "event"(미팅·방문·통화 약속), "task"(납기·회신·제출 기한)
   - date: YYYY-MM-DD (연도가 없으면 메일 발송일 기준으로 가장 가까운 앞날짜), time: "HH:MM" 또는 ""
   - label: 짧은 구분 (예: "ETA 부산", "ETD", "통관", "미팅", "납기")
   - title: 등록할 제목 (한국어, 제품명은 원문). 입고는 "품목명 수량" 형태 (예: "Vanillin 500kg")
   - item / qty / unit / supplier / bl_no: 입고(ship)일 때 아는 만큼 (모르면 "" 또는 0)
   - context: 근거가 된 원문 문장 (짧게)
3) summary: 한 줄 한국어 요약."""

SCHEMA = {
    "type": "object",
    "properties": {
        "translation": {"type": "string"},
        "summary": {"type": "string"},
        "schedules": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["ship", "event", "task"]},
                "date": {"type": "string"},
                "time": {"type": "string"},
                "label": {"type": "string"},
                "title": {"type": "string"},
                "item": {"type": "string"},
                "qty": {"type": "number"},
                "unit": {"type": "string"},
                "supplier": {"type": "string"},
                "bl_no": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["kind", "date", "time", "label", "title", "item", "qty", "unit", "supplier", "bl_no", "context"],
            "additionalProperties": False,
        }},
    },
    "required": ["translation", "summary", "schedules"],
    "additionalProperties": False,
}

_client = None


def enabled() -> bool:
    return bool(API_KEY)


def is_foreign(text: str) -> bool:
    letters = re.findall(r"[A-Za-z가-힣一-鿿]", text[:4000])
    if len(letters) < 20:
        return False
    return sum(1 for ch in letters if "가" <= ch <= "힣") / len(letters) < 0.2


def analyze(subject: str, sender: str, sent_at: str, text: str) -> dict:
    """→ {"translation", "summary", "schedules"} . 실패하면 예외."""
    global _client
    import anthropic
    if _client is None:
        _client = anthropic.Anthropic(api_key=API_KEY, timeout=120.0)
    body = text if len(text) <= 60000 else text[:60000] + "\n…(이하 생략)"
    response = _client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content":
                   f"보낸 사람: {sender}\n발송일: {sent_at}\n제목: {subject}\n\n본문:\n{body}"}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("AI 가 이 메일 처리를 거절했습니다.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("메일이 너무 길어 끝까지 처리하지 못했습니다.")
    text_out = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text_out)


def to_candidates(result: dict) -> list:
    """AI 결과 → mailin 후보 형식 (화면에서 쓰는 모양)"""
    out = []
    for s in result.get("schedules") or []:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", s.get("date", "")):
            continue
        t = s.get("time") or None
        if t and not re.match(r"^\d{2}:\d{2}$", t):
            t = None
        out.append({"kind": s["kind"], "date": s["date"], "time": t, "label": s.get("label", "")[:20],
                    "context": s.get("context", "")[:160], "bl_no": s.get("bl_no", ""), "title": s.get("title", ""),
                    "item": s.get("item", ""), "qty": s.get("qty") or 0, "unit": s.get("unit", ""),
                    "supplier": s.get("supplier", ""), "ai": True})
    return out[:12]
