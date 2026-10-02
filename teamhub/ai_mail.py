"""Claude 로 메일 읽기: 해외(수입) 메일 한국어 번역 + 일정 후보 찾기.

TEAMHUB_ANTHROPIC_API_KEY (또는 ANTHROPIC_API_KEY) 가 있을 때만 쓴다. 없으면 mailin.py 의 규칙 방식만 쓴다.
번역할 때 제조사 제품명(예: Pineapple Flavouring Natural F13865, Hedione HC, Damascenone 937450)은 원문 그대로 둔다.
"""
import json
import os
import re

MODEL = os.getenv("TEAMHUB_AI_MODEL", "claude-opus-5-5")
API_KEY = os.getenv("TEAMHUB_ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or ""

SYSTEM = """당신은 향료·화학 원료 수입 회사(이알씨)의 업무 비서입니다. 같은 건으로 주고받은 메일 쓰레드(한 통 이상, 오래된 것부터 시간순)를 읽고 JSON 으로 답합니다.
쓰레드 전체를 처음부터 끝까지 읽고, 앞 메일에만 나온 정보(품목, 수량, 공급사, B/L 등)도 활용합니다.

1) translation: 가장 최근 메일이 한국어가 아니면(영어·중국어 등 해외 공급사·포워더 메일) 그 메일 본문을 자연스러운 한국어 업무 문체로 번역합니다.
   - 제조사 제품명·품목명·제품 코드(예: "Pineapple Flavouring Natural F13865", "Hedione HC", "Damascenone 937450", "cis-3-Hexenyl acetate"),
     회사명, 사람 이름, B/L·컨테이너·인보이스 번호, 선박명, 항구명, 숫자·단위는 번역하지 말고 원문 그대로 둡니다.
   - 서명, 법적 고지문, 이전 메일 인용(회신 기록)은 번역하지 않고 생략합니다.
   - 메일이 이미 한국어면 빈 문자열("")로 둡니다.
2) schedules: 쓰레드 전체 기준으로 지금 유효한 최신 일정만 뽑습니다 (취소·변경 전 날짜, 이미 지난 날짜, 메일 발송일은 제외).
   - 같은 일정이 나중 메일에서 바뀌었으면(예: ETA 10/12 → 10/15 연기) 바뀐 날짜를 date 에, 이전 날짜를 prev_date 에 넣습니다. 바뀐 적 없으면 prev_date 는 "".
   - kind: "ship"(입고·선적·도착·통관 — ETA/ETD 등), "event"(미팅·방문·통화 약속), "task"(납기·회신·제출 기한)
   - date: YYYY-MM-DD (연도가 없으면 메일 발송일 기준으로 가장 가까운 앞날짜), time: "HH:MM" 또는 ""
   - label: 짧은 구분 (예: "ETA 부산", "ETD", "통관", "미팅", "납기")
   - title: 등록할 제목 (한국어, 제품명은 원문). 입고는 "품목명 수량" 형태 (예: "Vanillin 500kg")
   - item / qty / unit / supplier / bl_no: 입고(ship)일 때 아는 만큼 (모르면 "" 또는 0)
   - context: 근거가 된 원문 문장 (짧게)
3) summary: 쓰레드 전체 진행 상황을 한국어 1~2문장으로 요약 (예: "Vanillin 500kg 발주 → 선적 완료, 부산 ETA 10/12에서 10/15로 연기").
4) topic: 이 쓰레드의 업무 분류 하나 — "import"(수입·선적·통관·운송·특송), "sales"(견적·발주·주문·가격·납품),
   "finance"(결제·송금·계산서·미수), "quality"(샘플·COA·MSDS·규격·인증·클레임), "etc"(그 밖)"""

SCHEMA = {
    "type": "object",
    "properties": {
        "translation": {"type": "string"},
        "summary": {"type": "string"},
        "topic": {"type": "string", "enum": ["import", "sales", "finance", "quality", "etc"]},
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
                "prev_date": {"type": "string"},
            },
            "required": ["kind", "date", "time", "label", "title", "item", "qty", "unit", "supplier", "bl_no", "context",
                         "prev_date"],
            "additionalProperties": False,
        }},
    },
    "required": ["translation", "summary", "schedules", "topic"],
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


def thread_text(messages: list, limit: int = 80000) -> str:
    """[{from, sent_at, subject, body}] (오래된 것부터) → AI 에 줄 글.
    너무 길면 오래된 메일부터 앞부분만 남기고, 그래도 길면 가장 오래된 것부터 뺀다 (최신 메일은 항상 전부)."""
    parts = [f"=== 메일 {i + 1}/{len(messages)} · 보낸 사람: {m['from']} · 발송일: {m['sent_at']} · 제목: {m['subject']} ===\n{m['body']}"
             for i, m in enumerate(messages)]
    for k in range(len(parts) - 1):
        if sum(map(len, parts)) <= limit:
            break
        if len(parts[k]) > 2000:
            parts[k] = parts[k][:2000] + "\n…(이하 생략)"
    while len(parts) > 1 and sum(map(len, parts)) > limit:
        parts.pop(0)
    return "\n\n".join(parts)


def analyze(subject: str, sender: str, sent_at: str, text: str) -> dict:
    """→ {"translation", "summary", "schedules"} . 실패하면 예외. text 는 쓰레드 전체(thread_text)."""
    global _client
    import anthropic
    if _client is None:
        _client = anthropic.Anthropic(api_key=API_KEY, timeout=120.0)
    body = text if len(text) <= 100000 else text[-100000:]
    response = _client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content":
                   f"가장 최근 메일 — 보낸 사람: {sender} / 발송일: {sent_at} / 제목: {subject}\n\n쓰레드:\n{body}"}],
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
                    "supplier": s.get("supplier", ""), "prev_date": s.get("prev_date", "") if re.match(
                        r"^\d{4}-\d{2}-\d{2}$", s.get("prev_date", "")) else "", "ai": True})
    return out[:12]
