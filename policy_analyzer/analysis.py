"""추출된 계약 데이터로 보장 합산 및 체크 포인트 계산 (규칙 기반, 결과가 매번 같음)"""

from datetime import date

# 권장 기준 (설계사 상담용 참고값 - 회사 기준에 맞게 수정하세요)
BENCHMARKS = {
    "일반암": 30_000_000,
    "뇌혈관질환": 20_000_000,
    "허혈성심장질환": 20_000_000,
    "질병수술비": 1_000_000,
    "질병사망": 50_000_000,
    "질병후유장해": 50_000_000,
}

# 기준 판단 시 함께 인정하는 담보 (일반사망은 질병으로 사망해도 지급)
ALSO_COUNTS = {"질병사망": ("일반사망",)}

# 보장 범위가 좁은 담보 -> 넓은 담보 (넓은 담보가 없으면 안내)
NARROW = {"뇌졸중·뇌출혈": "뇌혈관질환", "급성심근경색": "허혈성심장질환"}

SUMMARY_ORDER = [
    "일반암", "특정암", "유사암·소액암", "암치료비", "암통원", "뇌혈관질환", "뇌졸중·뇌출혈", "허혈성심장질환", "급성심근경색",
    "실손의료비", "질병수술비", "상해수술비", "입원일당", "일반사망", "질병사망", "상해사망",
    "질병후유장해", "상해후유장해", "치매·간병", "운전자", "배상책임", "치아", "기타",
]

MIN_COVER_AGE = 80  # 주요 보장 만기가 이 나이 전에 끝나면 안내


def won(v) -> str:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return "-"
    if v >= 10_000 and v % 10_000 == 0:
        man = v // 10_000
        return f"{man // 10_000}억 {man % 10_000:,}만원".replace(" 0만원", "원") if man >= 10_000 else f"{man:,}만원"
    return f"{v:,}원"


def parse_date(s) -> date | None:
    s = "".join(ch for ch in str(s or "") if ch.isdigit())
    if len(s) != 8:
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    except ValueError:
        return None


def age_on(birth: date | None, on: date) -> int | None:
    if not birth:
        return None
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


PAY_STATUSES = ["정상", "미납", "실효", "해지"]
OWNERS = {"": "확인 전", "mine": "내 계약", "other_planner": "다른 설계사 계약", "other_company": "타사 계약"}
ALERT_DAYS = 90  # 만기·갱신 알림 기간


def manage(p: dict) -> dict:
    return p.get("manage") or {}


def is_active(p: dict, today: date) -> bool:
    """보장이 살아 있는 계약 (만기 전이고 실효·해지가 아님)"""
    if manage(p).get("pay_status") in ("실효", "해지"):
        return False
    end = parse_date(p.get("maturity_date"))
    return end is None or end >= today


def policy_alerts(p: dict, today: date | None = None) -> list[tuple[str, str, str]]:
    """계약 1건의 알림 [(날짜 YYYY-MM-DD, level, 내용)]  level: warn / info"""
    today = today or date.today()
    m = manage(p)
    label = f"{p.get('company') or '?'} {p.get('product_name') or ''}".strip()
    out = []
    status = m.get("pay_status")
    if status == "미납":
        out.append((m.get("checked_at") or today.isoformat(), "warn", f"보험료 미납 · {label} — 실효 전에 납입 안내 필요"))
    elif status == "실효":
        out.append((m.get("checked_at") or today.isoformat(), "warn", f"계약 실효 · {label} — 보장 중단 상태, 부활 가능 여부 확인"))
    if status in ("실효", "해지"):
        return out
    limit = today.toordinal() + ALERT_DAYS
    end = parse_date(p.get("maturity_date"))
    if end and today <= end and end.toordinal() <= limit:
        out.append((end.isoformat(), "info", f"계약 만기 예정 · {label}"))
    renew = sorted({d for c in p.get("coverages") or []
                    if c.get("renewable") and (d := parse_date(c.get("end_date"))) and today <= d and d.toordinal() <= limit})
    if renew:
        n = sum(1 for c in p.get("coverages") or [] if c.get("renewable") and parse_date(c.get("end_date")) == renew[0])
        out.append((renew[0].isoformat(), "info", f"갱신 예정 (담보 {n}개, 보험료 인상 가능) · {label}"))
    return out


def analyze(insured: dict, policies: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    birth = parse_date(insured.get("birth_date"))
    active = [p for p in policies if is_active(p, today)]

    totals: dict[str, int] = {}
    sources: dict[str, list[str]] = {}
    renewable_items = []
    for p in active:
        for c in p.get("coverages") or []:
            cat = c.get("category") or "기타"
            amt = c.get("amount") or 0
            if cat == "실손의료비":  # 실손은 중복 보상되지 않으므로 합산하지 않고 최대값
                totals[cat] = max(totals.get(cat, 0), amt)
            else:
                totals[cat] = totals.get(cat, 0) + amt
            sources.setdefault(cat, []).append(f"{p.get('company') or '?'} {c.get('name')}")
            if c.get("renewable") or (c.get("renewable") is None and p.get("is_renewable")):
                renewable_items.append(f"{p.get('company') or '?'} · {c.get('name')}")

    checks = []  # (level, 제목, 설명)  level: warn / info / ok

    # 실손 중복/미가입
    silson = [p for p in active if any(c.get("category") == "실손의료비" for c in p.get("coverages") or [])]
    if len(silson) > 1:
        names = ", ".join(f"{p.get('company')} {p.get('product_name')}" for p in silson)
        checks.append(("warn", "실손보험 중복 가입",
                       f"실손보험이 {len(silson)}건 있습니다 ({names}). 실손은 중복 가입해도 실제 손해액 안에서 나눠 보상되므로 보험료만 더 낼 수 있습니다."))
    elif not silson:
        checks.append(("warn", "실손보험 없음", "실손의료비 보장이 확인되지 않습니다."))

    # 기준 대비 부족
    for cat, need in BENCHMARKS.items():
        have = totals.get(cat, 0) + sum(totals.get(c, 0) for c in ALSO_COUNTS.get(cat, ()))
        if have < need:
            narrow = [k for k, v in NARROW.items() if v == cat and totals.get(k)]
            extra = f" (좁은 범위 담보 '{narrow[0]}' {won(totals[narrow[0]])}만 있음)" if narrow else ""
            checks.append(("warn" if have == 0 else "info", f"{cat} 보장 부족",
                           f"현재 {won(have) if have else '없음'} / 참고 기준 {won(need)}{extra}"))

    # 좁은 범위 담보만 있는 경우
    for narrow, wide in NARROW.items():
        if totals.get(narrow) and not totals.get(wide) and wide not in BENCHMARKS:
            checks.append(("info", f"{narrow}만 보장", f"'{wide}' 전체를 보장하는 담보가 없습니다."))

    # 갱신형
    if renewable_items:
        checks.append(("info", f"갱신형 담보 {len(renewable_items)}개",
                       "갱신 시 보험료가 오를 수 있습니다: " + ", ".join(renewable_items[:8])
                       + (" 외" if len(renewable_items) > 8 else "")))

    # 만기가 짧은 계약
    if birth:
        for p in active:
            end = parse_date(p.get("maturity_date"))
            end_age = age_on(birth, end) if end else None
            if end_age is not None and end_age < MIN_COVER_AGE and not p.get("is_renewable"):
                checks.append(("info", "보장 만기가 짧음",
                               f"{p.get('company')} {p.get('product_name')}: {end_age}세 만기 ({end:%Y.%m.%d})"))

    # 부담보 등 인수조건
    for p in active:
        if p.get("exclusions"):
            checks.append(("info", "부담보·인수조건 있음",
                           f"{p.get('company')} {p.get('product_name')}: " + " / ".join(p["exclusions"])))

    # 해약환급금이 없거나 적은 상품
    for p in active:
        name = p.get("product_name") or ""
        if any(k in name for k in ("해약환급금 미지급", "해약환급금미지급", "무해지", "해약환급금 일부지급", "저해지")):
            checks.append(("info", "해지 시 환급금 없음·적음",
                           f"{p.get('company')} {name}: 납입기간 중 해지하면 낸 보험료를 거의 돌려받지 못합니다. "
                           "보장 변경이 필요하면 해지보다 부족한 보장을 추가하는 쪽을 먼저 검토하세요."))

    # 납입 상태
    for p in policies:
        st = manage(p).get("pay_status")
        if st in ("미납", "실효"):
            checks.append(("warn", f"보험료 {st}" if st == "미납" else "계약 실효",
                           f"{p.get('company')} {p.get('product_name')}: "
                           + ("납입최고기간이 지나면 실효됩니다. 납입 안내가 필요합니다." if st == "미납"
                              else "보장이 중단된 상태로 보장 합산에서 제외했습니다. 부활 가능 기간인지 확인하세요.")))

    # 계약자와 피보험자가 다른 경우
    for p in active:
        c, i = p.get("contractor_name"), p.get("insured_name")
        if c and i and c != i:
            checks.append(("info", "계약자≠피보험자", f"{p.get('company')} {p.get('product_name')}: 계약자 {c}, 피보험자 {i}"))

    premium = sum(p.get("monthly_premium") or 0 for p in active)
    summary = [{"category": cat, "amount": totals[cat], "sources": sources.get(cat, []),
                "benchmark": BENCHMARKS.get(cat)} for cat in SUMMARY_ORDER if cat in totals]

    return {
        "age": age_on(birth, today),
        "active_count": len(active),
        "expired_count": len(policies) - len(active),
        "monthly_premium": premium,
        "summary": summary,
        "missing_benchmarks": [cat for cat in BENCHMARKS
                               if cat not in totals and not any(c in totals for c in ALSO_COUNTS.get(cat, ()))],
        "checks": checks,
    }
