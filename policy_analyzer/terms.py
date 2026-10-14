"""
보험 약관(특히 연금·저축성) 분석.

  약관 PDF → 이율·사업비·해지공제·세제 관련 페이지만 선별 → Claude 로 구조화 추출
  → 사업비를 반영한 적립금·실질 수익률 시뮬레이션

약관에는 개인 계약 숫자가 없으므로, 결과는 '상품 구조'와 '예시 시뮬레이션'이다.
"""

import base64
import json
import os

# 약관에서 수익률에 영향을 주는 페이지를 찾는 단어
TERMS_KEYS = ("최저보증이율", "공시이율", "수수료 안내표", "수수료 합산표", "계약체결비용",
              "계약관리비용", "해지공제", "세액공제", "연금수령", "적용이율", "사업비")
MAX_TERMS_PAGES = 12

_s = {"type": "string"}
_n = {"type": "number"}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _arr(item: dict) -> dict:
    return {"type": "array", "items": item}


# union(nullable) 타입을 쓰지 않는다 (Claude 구조화 출력 한도)
TERMS_SCHEMA = _obj({
    "company": {**_s, "description": "보험사명. 모르면 빈 문자열"},
    "product_name": _s,
    "product_type": {**_s, "description": "예: 연금저축보험, 연금보험, 저축보험, 보장성"},
    "rate_type": {**_s, "description": "적립 이율 방식 한 줄 요약 (예: 공시이율 변동, 매월 확정)"},
    "min_guarantee": _arr(_obj({
        "period": {**_s, "description": "예: 가입 후 5년 미만"},
        "rate_pct": {**_n, "description": "연복리 % 숫자 (예: 1.25)"},
    })),
    "fees": _arr(_obj({
        "name": {**_s, "description": "예: 계약체결비용, 계약관리비용"},
        "from_year": {**_n, "description": "적용 시작 경과연도 (1부터). 납입기간 이후면 납입기간+1"},
        "to_year": {**_n, "description": "적용 끝 경과연도. 끝이 없으면 99"},
        "pct_of_premium": {**_n, "description": "기본보험료 대비 % (예: 2.98). 보험료 기준이 아니면 0"},
        "note": _s,
    })),
    "fee_example_basis": {**_s, "description": "수수료 안내표의 예시 기준 (예: 남자 30세, 월 30만원, 20년납)"},
    "example_monthly_premium": {**_n, "description": "예시 기준 월보험료(원). 없으면 0"},
    "example_pay_years": {**_n, "description": "예시 기준 납입기간(년). 없으면 0"},
    "extra_payment_fee_pct": {**_n, "description": "추가납입보험료 비용 %. 없으면 0"},
    "annuity_fee_note": {**_s, "description": "연금 수령 시 떼는 비용 요약"},
    "surrender_penalty": _arr(_obj({
        "year": {**_n, "description": "경과연도"},
        "amount_won": {**_n, "description": "해지공제 금액(원). 만원 단위면 원으로 환산"},
    })),
    "tax_notes": _arr(_s),
    "cautions": {**_arr(_s), "description": "계약자에게 불리할 수 있는 주의점"},
})

SYSTEM = """당신은 한국 보험 약관을 읽고 상품 구조를 정리하는 도우미입니다.
- 첨부된 약관 페이지에서 이율(공시이율·최저보증이율), 사업비(계약체결비용·계약관리비용),
  해지공제, 연금 수령 시 비용, 세제(세액공제·연금소득세·기타소득세) 내용을 추출하세요.
- 약관에 적힌 숫자만 쓰고 지어내지 마세요. 없는 글자 항목은 빈 문자열, 숫자는 0, 목록은 빈 배열.
- 사업비는 '기본보험료의 몇 %'로 적힌 값을 pct_of_premium 에 넣고, 경과연도 구간으로 나누세요.
  (예: 10년 이내 2.98% → from_year 1, to_year 10)
- cautions 에는 조기 해지 손실, 높은 초기 사업비, 최저보증 하락 등 계약자가 알아야 할 점을 짧게 적으세요."""


class TermsError(Exception):
    pass


def is_terms_pdf(pdf: bytes) -> bool:
    """약관 문서인지 판별: 앞부분에 '약관'이 있고, 증권 표시(증권번호·가입금액)가 거의 없으며 쪽수가 많음"""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf, filetype="pdf")
    except Exception:
        return False
    try:
        if doc.page_count < 20:
            return False
        head = "".join(doc.load_page(i).get_text() or "" for i in range(min(5, doc.page_count)))
        return "약관" in head and head.count("증권번호") == 0 and head.count("가입금액") < 3
    finally:
        doc.close()


def select_pages(pdf: bytes, max_pages: int = MAX_TERMS_PAGES) -> tuple[bytes, int, list[int]]:
    """수익률 관련 단어가 많은 페이지만 남긴 PDF, 원본 페이지 수, 선택 페이지 번호(1부터)"""
    import fitz  # PyMuPDF
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        total = doc.page_count
        scored = []
        for i in range(total):
            text = doc.load_page(i).get_text() or ""
            score = sum(text.count(k) for k in TERMS_KEYS)
            if score:
                scored.append((score, i))
        if not scored:
            raise TermsError("약관에서 이율·사업비 관련 내용을 찾지 못했습니다. 글자가 있는(스캔 아닌) PDF인지 확인해 주세요.")
        keep = sorted(i for _, i in sorted(scored, reverse=True)[:max_pages])
        out = fitz.open()
        for i in keep:
            out.insert_pdf(doc, from_page=i, to_page=i)
        data = out.tobytes()
        out.close()
        return data, total, [i + 1 for i in keep]
    finally:
        doc.close()


def extract_terms(pdf: bytes) -> dict:
    """선별한 약관 페이지를 Claude 로 구조화. claude 백엔드에서만 동작."""
    if os.environ.get("ANALYZER_MOCK") == "1":
        return dict(MOCK_TERMS)
    if os.environ.get("EXTRACT_BACKEND", "local") != "claude":
        raise TermsError("약관 분석은 Claude 모드(EXTRACT_BACKEND=claude)에서만 됩니다.")
    import anthropic
    client = anthropic.Anthropic()
    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                         "data": base64.standard_b64encode(pdf).decode()}},
        {"type": "text", "text": "이 약관의 이율·사업비·해지공제·세제 구조를 추출해 주세요."},
    ]
    try:
        with client.beta.messages.stream(
            model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": TERMS_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise TermsError("Claude API 키가 올바르지 않습니다.") from e
    except anthropic.RateLimitError as e:
        raise TermsError("요청이 많아 잠시 후 다시 시도해 주세요.") from e
    except anthropic.APIStatusError as e:
        raise TermsError(f"분석 서버 오류 ({e.status_code}): {getattr(e, 'message', '')}") from e
    except anthropic.APIConnectionError as e:
        raise TermsError("분석 서버에 연결할 수 없습니다.") from e
    if msg.stop_reason == "refusal":
        raise TermsError("이 문서는 분석할 수 없습니다.")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise TermsError("분석 결과를 읽지 못했습니다. 다시 시도해 주세요.") from e


def fee_rate(fees: list[dict], year: int) -> float:
    """해당 경과연도에 기본보험료에서 떼는 비율 합계 (0~1)"""
    pct = sum(f.get("pct_of_premium") or 0 for f in fees
              if (f.get("from_year") or 1) <= year <= (f.get("to_year") or 99))
    return min(max(pct, 0), 100) / 100


def simulate(fees: list[dict], monthly: float, years: int, rate_pct: float) -> dict:
    """매달 보험료에서 사업비를 떼고 나머지를 rate_pct(연복리)로 적립했을 때 결과"""
    months = int(years * 12)
    i = (1 + rate_pct / 100) ** (1 / 12) - 1
    value = 0.0
    for m in range(months):
        value = (value + monthly * (1 - fee_rate(fees, m // 12 + 1))) * (1 + i)
    paid = monthly * months
    return {"rate": rate_pct, "paid": paid, "value": value,
            "ratio": value / paid if paid else 0, "irr": _irr(monthly, months, value)}


def _irr(monthly: float, months: int, target: float) -> float:
    """매달 같은 금액을 넣어 target 이 되는 연 수익률(%)을 이분법으로 계산"""
    if months == 0 or target <= 0:
        return 0.0
    lo, hi = -0.2, 0.3
    for _ in range(80):
        mid = (lo + hi) / 2
        j = (1 + mid) ** (1 / 12) - 1
        fv = sum(monthly * (1 + j) ** (months - m) for m in range(months))
        lo, hi = (mid, hi) if fv < target else (lo, mid)
    return lo * 100


# ANALYZER_MOCK=1 화면 시험용 (교보 연금저축보험 약관 예시 값)
MOCK_TERMS = {
    "company": "교보생명", "product_name": "[샘플] 교보연금저축보험", "product_type": "연금저축보험",
    "rate_type": "매월 1일 공시이율을 1개월간 확정 적용",
    "min_guarantee": [{"period": "가입 후 5년 미만", "rate_pct": 1.25},
                      {"period": "5년 이상 10년 미만", "rate_pct": 1.0},
                      {"period": "10년 이상", "rate_pct": 0.5}],
    "fees": [{"name": "계약체결비용", "from_year": 1, "to_year": 10, "pct_of_premium": 2.98, "note": "10년 이내 매월"},
             {"name": "계약관리비용", "from_year": 1, "to_year": 20, "pct_of_premium": 4.17, "note": "납입기간 이내 매월"},
             {"name": "계약관리비용", "from_year": 21, "to_year": 99, "pct_of_premium": 1.0, "note": "납입기간 이후 매월"}],
    "fee_example_basis": "남자 30세, 월 30만원, 60세 연금개시, 20년납",
    "example_monthly_premium": 300000, "example_pay_years": 20, "extra_payment_fee_pct": 1.5,
    "annuity_fee_note": "종신연금형 연금액의 0.7%, 확정기간연금형 0.5%",
    "surrender_penalty": [{"year": y, "amount_won": a} for y, a in
                          ((1, 500000), (2, 410000), (3, 330000), (4, 250000), (5, 170000), (6, 80000))],
    "tax_notes": ["납입보험료 세액공제 12~15% (약관 작성 당시 세법 기준)",
                  "연금 외 수령(중도해지 등) 시 기타소득세 16.5%"],
    "cautions": ["초기 10년 사업비가 높아 조기 해지 시 원금 손실", "최저보증이율이 10년 후 0.5%로 낮아짐"],
}
