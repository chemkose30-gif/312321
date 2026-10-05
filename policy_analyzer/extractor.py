"""보험증권(PDF/이미지) -> 구조화 데이터 추출 (Claude API)"""

import base64
import copy
import json
import os
import re

import anthropic

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")

CATEGORIES = [
    "일반암", "특정암", "유사암·소액암", "암치료비", "암통원", "뇌혈관질환", "뇌졸중·뇌출혈", "허혈성심장질환", "급성심근경색",
    "실손의료비", "질병수술비", "상해수술비", "입원일당", "일반사망", "질병사망", "상해사망",
    "질병후유장해", "상해후유장해", "치매·간병", "운전자", "배상책임", "치아", "기타",
]

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PDF_TYPE = "application/pdf"

_s = {"type": ["string", "null"]}
_i = {"type": ["integer", "null"]}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SCHEMA = _obj({
    "insured": _obj({
        "name": _s,
        "birth_date": {**_s, "description": "YYYYMMDD. 주민번호 앞자리+뒷자리 첫 숫자로 계산 가능"},
        "gender": {**_s, "description": "남 또는 여"},
        "address": _s,
        "phone": _s,
    }),
    "policies": {"type": "array", "items": _obj({
        "company": _s,
        "product_name": _s,
        "policy_no": _s,
        "contractor_name": _s,
        "insured_name": _s,
        "contract_date": {**_s, "description": "YYYYMMDD"},
        "maturity_date": {**_s, "description": "YYYYMMDD"},
        "payment_period": {**_s, "description": "예: 20년납, 전기납"},
        "payment_cycle": {**_s, "description": "예: 월납"},
        "monthly_premium": {**_i, "description": "월 환산 보험료(원)"},
        "is_renewable": {"type": ["boolean", "null"], "description": "주계약/대부분 특약이 갱신형이면 true"},
        "exclusions": {"type": "array", "items": {"type": "string"},
                       "description": "부담보·할증 등 인수조건 문구 그대로"},
        "coverages": {"type": "array", "items": _obj({
            "name": {"type": "string", "description": "증권에 적힌 담보/특약명 그대로"},
            "category": {"type": "string", "enum": CATEGORIES},
            "amount": {**_i, "description": "가입금액(원). 일당은 1일 금액"},
            "renewable": {"type": ["boolean", "null"]},
            "end_date": {**_s, "description": "담보별 보장 종료일 YYYYMMDD (있으면)"},
        })},
    })},
    "warnings": {"type": "array", "items": {"type": "string"},
                 "description": "읽기 어려웠던 부분, 확실하지 않은 값"},
})

SYSTEM = """당신은 한국 보험증권을 읽어 데이터를 추출하는 도우미입니다.
- 첨부된 모든 파일(여러 증권일 수 있음)에서 피보험자 정보와 계약별 담보를 빠짐없이 추출하세요.
- 증권에 없는 값은 지어내지 말고 null 로 두세요. 금액은 원 단위 정수로 변환하세요 (예: 3천만원 -> 30000000).
- 주민등록번호 뒷자리는 절대 출력하지 마세요. 생년월일과 성별 계산에만 사용하세요.
- 담보마다 category 를 가장 가까운 것으로 분류하세요. 애매하면 '기타'.
  · 유사암(갑상선암·제자리암·경계성종양·기타피부암)은 '유사암·소액암'.
  · 일반암과 별도로 특정 암(유방암·전립선암·고액암 등)만 보장하는 진단비는 '특정암'.
  · 암 수술·방사선·항암약물·표적항암 등 암 치료비는 '암치료비', 암 통원 일당은 '암통원'.
  · 보험료 납입면제 특약은 '기타'로 하고 amount 는 null.
  · 뇌혈관질환 전체를 보장하면 '뇌혈관질환', 뇌졸중/뇌출혈만이면 '뇌졸중·뇌출혈'.
  · 허혈성심장질환 전체면 '허혈성심장질환', 급성심근경색만이면 '급성심근경색'.
- 부담보, 할증, 특정부위 제외 같은 인수조건 문구가 있으면 exclusions 에 그대로 적으세요.
- 증권이 아닌 문서가 섞여 있으면 warnings 에 적으세요."""

RRN_RE = re.compile(r"(\d{6})\s*[-–]?\s*[1-8]\d{6}")


def mask_rrn(obj):
    """혹시 주민번호 뒷자리가 섞여 나오면 가린다."""
    if isinstance(obj, str):
        return RRN_RE.sub(r"\1-*******", obj)
    if isinstance(obj, list):
        return [mask_rrn(x) for x in obj]
    if isinstance(obj, dict):
        return {k: mask_rrn(v) for k, v in obj.items()}
    return obj


class ExtractError(Exception):
    pass


def build_content(files: list[tuple[str, str, bytes]]) -> list[dict]:
    """files: [(파일명, MIME, 바이트)]"""
    blocks = []
    for name, mime, data in files:
        b64 = base64.standard_b64encode(data).decode()
        if mime == PDF_TYPE:
            blocks.append({"type": "document", "source": {"type": "base64", "media_type": PDF_TYPE, "data": b64},
                           "title": name})
        elif mime in IMAGE_TYPES:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
        else:
            raise ExtractError(f"지원하지 않는 파일 형식입니다: {name} (PDF, JPG, PNG, WEBP 가능)")
    blocks.append({"type": "text", "text": "첨부한 보험증권에서 정보를 추출해 주세요."})
    return blocks


def extract(files: list[tuple[str, str, bytes]]) -> dict:
    if os.environ.get("ANALYZER_MOCK") == "1":
        return copy.deepcopy(MOCK_RESULT)

    client = anthropic.Anthropic()
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            system=SYSTEM,
            messages=[{"role": "user", "content": build_content(files)}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.BadRequestError as e:
        raise ExtractError(f"파일을 처리할 수 없습니다 (크기·페이지 수를 확인하세요): {e.message}") from e
    except anthropic.AuthenticationError as e:
        raise ExtractError("Claude API 키가 올바르지 않습니다.") from e
    except anthropic.RateLimitError as e:
        raise ExtractError("요청이 많아 잠시 후 다시 시도해 주세요.") from e
    except anthropic.APIStatusError as e:
        raise ExtractError(f"분석 서버 오류 ({e.status_code}). 잠시 후 다시 시도해 주세요.") from e
    except anthropic.APIConnectionError as e:
        raise ExtractError("분석 서버에 연결할 수 없습니다.") from e

    if msg.stop_reason == "refusal":
        raise ExtractError("이 파일은 분석할 수 없습니다.")
    if msg.stop_reason == "max_tokens":
        raise ExtractError("증권 내용이 너무 많습니다. 파일을 나눠서 올려 주세요.")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        return mask_rrn(json.loads(text))
    except json.JSONDecodeError as e:
        raise ExtractError("분석 결과를 읽지 못했습니다. 다시 시도해 주세요.") from e


# ANALYZER_MOCK=1 일 때 쓰는 가짜 결과 (화면 시험용, 실제 데이터 아님)
MOCK_RESULT = {
    "insured": {"name": "김샘플", "birth_date": "19850315", "gender": "여",
                "address": "서울특별시 중구 세종대로 110", "phone": "010-0000-0000"},
    "policies": [
        {"company": "가상생명", "product_name": "[샘플] 종합건강보험", "policy_no": "S-0001",
         "contractor_name": "김샘플", "insured_name": "김샘플", "contract_date": "20150401", "maturity_date": "20450401",
         "payment_period": "20년납", "payment_cycle": "월납", "monthly_premium": 87000, "is_renewable": False,
         "exclusions": ["위·십이지장 5년 부담보"],
         "coverages": [
             {"name": "암진단비", "category": "일반암", "amount": 20000000, "renewable": False, "end_date": "20450401"},
             {"name": "유사암진단비", "category": "유사암·소액암", "amount": 4000000, "renewable": False, "end_date": "20450401"},
             {"name": "뇌출혈진단비", "category": "뇌졸중·뇌출혈", "amount": 20000000, "renewable": False, "end_date": "20450401"},
             {"name": "급성심근경색진단비", "category": "급성심근경색", "amount": 20000000, "renewable": False, "end_date": "20450401"},
             {"name": "질병사망", "category": "질병사망", "amount": 30000000, "renewable": False, "end_date": "20450401"},
         ]},
        {"company": "가상화재", "product_name": "[샘플] 실손의료비보험", "policy_no": "S-0002",
         "contractor_name": "김샘플", "insured_name": "김샘플", "contract_date": "20180701", "maturity_date": "20330701",
         "payment_period": "전기납", "payment_cycle": "월납", "monthly_premium": 23000, "is_renewable": True,
         "exclusions": [],
         "coverages": [
             {"name": "질병입원의료비", "category": "실손의료비", "amount": 50000000, "renewable": True, "end_date": None},
             {"name": "상해수술비", "category": "상해수술비", "amount": 1000000, "renewable": True, "end_date": None},
         ]},
        {"company": "다른가상손보", "product_name": "[샘플] 실손보험", "policy_no": "S-0003",
         "contractor_name": "김샘플", "insured_name": "김샘플", "contract_date": "20210101", "maturity_date": "20360101",
         "payment_period": "전기납", "payment_cycle": "월납", "monthly_premium": 15000, "is_renewable": True,
         "exclusions": [],
         "coverages": [{"name": "실손의료비", "category": "실손의료비", "amount": 50000000, "renewable": True, "end_date": None}]},
    ],
    "warnings": [],
}
