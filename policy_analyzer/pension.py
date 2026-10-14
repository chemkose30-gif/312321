"""
연금·저축성 계약 수익률 계산.

  증권의 월보험료·납입기간·계약일 + (선택) 설계사가 입력한 현재 적립금·공시이율로
  - 지금까지 낸 돈, 실제 수익률(적립금 입력 시)
  - 납입 완료 시점 예상 적립금·환급률·연수익률 (사업비 반영)
  - 세액공제를 포함한 체감 수익률(연금저축만)
을 계산한다. 사업비는 상품마다 다르므로 입력값이 없으면 일반적인 가정치를 쓴다.
"""

import re
from datetime import date

import analysis
import terms

# 사업비 기본 가정 (기본보험료 대비 %, 교보 연금저축 약관 예시 수준)
DEFAULT_ACQ_PCT = 2.98      # 계약체결비용
DEFAULT_ACQ_YEARS = 10
DEFAULT_MGMT_PCT = 4.17     # 계약관리비용 (납입기간 동안)
DEFAULT_RATE = 2.5          # 공시이율 기본 가정
PENSION_TAX_PCT = 5.5       # 연금 수령 시 연금소득세 (대략, 지방세 포함)
PENSION_WORDS = ("연금", "저축")
EXCLUDE_WORDS = ("연금전환", "보장")  # 보장성 상품의 '연금전환특약' 등 제외


def is_pension(p: dict) -> bool:
    name = p.get("product_name") or ""
    return any(w in name for w in PENSION_WORDS) and not any(w in name for w in EXCLUDE_WORDS)


def is_tax_qualified(p: dict) -> bool:
    """세액공제 대상 연금저축인지 (상품명 기준)"""
    return "연금저축" in (p.get("product_name") or "")


def pay_years(p: dict) -> int | None:
    m = re.search(r"(\d+)\s*년", p.get("payment_period") or "")
    return int(m.group(1)) if m else None


def months_paid(p: dict, total_months: int, today: date | None = None) -> int:
    start = analysis.parse_date(p.get("contract_date"))
    if not start:
        return 0
    today = today or date.today()
    n = (today.year - start.year) * 12 + (today.month - start.month) + (1 if today.day >= start.day else 0)
    return max(0, min(n, total_months))


def fee_table(years: int, acq_pct: float, mgmt_pct: float) -> list[dict]:
    return [{"from_year": 1, "to_year": DEFAULT_ACQ_YEARS, "pct_of_premium": acq_pct},
            {"from_year": 1, "to_year": years, "pct_of_premium": mgmt_pct}]


# 해지공제 기본 가정: 교보 연금저축 약관 예시(월 30만원 기준, 경과연도별 원) → 월보험료 비례로 환산
DEFAULT_PENALTY_300K = {1: 500000, 2: 410000, 3: 330000, 4: 250000, 5: 170000, 6: 80000}
SURRENDER_TAX_PCT = 16.5    # 연금저축 중도해지 기타소득세 (지방세 포함)


def value_after(fees: list[dict], monthly: float, months: int, rate_pct: float) -> float:
    """months 개월 납입 후 사업비 반영 적립금"""
    i = (1 + rate_pct / 100) ** (1 / 12) - 1
    v = 0.0
    for m in range(months):
        v = (v + monthly * (1 - terms.fee_rate(fees, m // 12 + 1))) * (1 + i)
    return v


def surrender(p: dict, monthly: float, n: int, fees: list[dict], rate: float, current: float, cfg: dict) -> dict | None:
    """지금 해지하면 받는 금액(세전·세후)과 손실. 적립금 입력값이 있으면 그것을 해지환급금으로 본다."""
    if not n:
        return None
    paid = cfg.get("paid_amount") or monthly * n
    if current:
        before_tax, penalty, basis = current, 0.0, "입력한 해지환급금 기준"
    else:
        est = value_after(fees, monthly, n, rate)
        year = max(1, n // 12)
        penalty = DEFAULT_PENALTY_300K.get(year, 0) * monthly / 300000
        before_tax, basis = max(est - penalty, 0), f"추정 (공시이율 {rate:g}%, 해지공제 약 {round(penalty, -3):,.0f}원 가정)"
    tax = before_tax * SURRENDER_TAX_PCT / 100 if is_tax_qualified(p) else 0.0
    after = before_tax - tax
    return {"basis": basis, "paid": paid, "before_tax": before_tax, "penalty": penalty,
            "tax": tax, "after_tax": after, "loss": paid - after,
            "credits_received": paid * (cfg.get("credit_pct") or 13.2) / 100 if is_tax_qualified(p) else 0.0}


def analyze(p: dict, today: date | None = None) -> dict | None:
    """연금 수익률 결과. 계산에 필요한 값(월보험료·납입기간)이 없으면 이유와 함께 반환."""
    if not is_pension(p):
        return None
    cfg = p.get("pension") or {}
    monthly = p.get("monthly_premium") or 0
    # 증권에 월 보험료가 없어도 직접 적은 납입액÷회차로 계산
    if not monthly and cfg.get("paid_amount") and cfg.get("paid_count"):
        monthly = cfg["paid_amount"] / cfg["paid_count"]
    years = pay_years(p)
    if not monthly:
        return {"ok": False, "reason": "월 보험료를 몰라 계산할 수 없습니다. 아래에 실제 납입회차·납입액을 적어 주세요."}
    if not analysis.parse_date(p.get("contract_date")) and not cfg.get("paid_count"):
        return {"ok": False, "reason": "계약일을 몰라 납입회차를 알 수 없습니다. 아래에 실제 납입회차를 적어 주세요."}
    # 납입기간을 모르면 계약일부터 오늘까지 낸 것으로 보고, 만기 예상은 생략
    total_months = years * 12 if years else 1200
    n = months_paid(p, total_months, today)
    if not years:
        years = 0
    # 보험사 앱의 실제 납입 기록을 입력했으면 그것을 우선 사용
    if cfg.get("paid_count"):
        n = int(cfg["paid_count"])
    elif cfg.get("paid_amount"):
        n = max(1, round(cfg["paid_amount"] / monthly))
    rate = cfg.get("rate") or DEFAULT_RATE
    fees = fee_table(years or 99, cfg.get("acq_pct") if cfg.get("acq_pct") is not None else DEFAULT_ACQ_PCT,
                     cfg.get("mgmt_pct") if cfg.get("mgmt_pct") is not None else DEFAULT_MGMT_PCT)
    result = {"ok": True, "monthly": monthly, "years": years, "months_paid": n, "total_months": total_months,
              "paid": cfg.get("paid_amount") or monthly * n, "paid_actual": bool(cfg.get("paid_amount") or cfg.get("paid_count")),
              "rate": rate, "custom_fees": "acq_pct" in cfg or "mgmt_pct" in cfg,
              "tax_qualified": is_tax_qualified(p)}

    # 현재 적립금을 입력했으면 실제 수익률
    current = cfg.get("current_value") or 0
    if current and n:
        paid_now = cfg.get("paid_amount") or monthly * n
        result["actual"] = {"value": current, "ratio": current / paid_now,
                            "irr": terms._irr(paid_now / n, n, current), "as_of": cfg.get("as_of") or ""}

    result["surrender"] = surrender(p, monthly, n, fees, rate, current, cfg)

    # 납입 완료 시점 예상 (최저 수준 1% / 입력 공시이율) — 납입기간을 알 때만
    result["projections"] = [terms.simulate(fees, monthly, years, r) for r in sorted({1.0, rate})] if years else []

    # 연금저축 세액공제
    if result["tax_qualified"] and years:
        annual = monthly * 12
        credit_pct = cfg.get("credit_pct") or 13.2
        main = next(s for s in result["projections"] if s["rate"] == rate)
        net_monthly = monthly * (1 - credit_pct / 100)
        result["tax"] = {"annual": annual, "credit_pct": credit_pct, "credit": annual * credit_pct / 100,
                         "felt_irr": terms._irr(net_monthly, total_months, main["value"] * (1 - PENSION_TAX_PCT / 100))}
    return result
