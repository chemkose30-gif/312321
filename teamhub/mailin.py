"""메일에서 일정 후보 찾기.

info@ 로 전달된 메일(또는 직접 올린 .eml)을 읽어 날짜를 찾고, 주변 단어로 종류를 정한다.
  ship  : 입고·선적 예정 (ETA/ETD/도착/입항/선적/통관…)  → 🚢 입고예정
  event : 미팅·방문 약속 (meeting/visit/미팅/회의/방문…)   → 📅 일정
  task  : 납기·마감 요청 (deadline/due/납기/까지/마감…)     → 📋 업무
사람이 확인하고 등록하도록 '후보'만 만든다.
"""
import email
import hashlib
import html
import re
from datetime import date, datetime, timedelta
from email import policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"

KEYWORDS = {
    "ship": r"e\.?t\.?a\b|e\.?t\.?d\b|arriv|arrival|departure|vessel|shipment|shipping|shipped|b/?l\b|awb|on board|"
            r"도착|입항|입고|선적|출항|통관|배송|운송|포워더|컨테이너",
    "event": r"meeting|visit|conference|appointment|call\b|seminar|exhibition|"
             r"미팅|회의|방문|면담|약속|상담|출장|세미나|전시회|식사|점심|저녁|교육",
    "task": r"deadline|due\b|no later than|until|request|delivery date|"
            r"납기|마감|까지|요청|회신|제출|납품일|기한",
}
HEADER_LINE = re.compile(r"^\s*(from|sent|date|to|cc|subject|보낸\s*사람|받는\s*사람|보낸\s*날짜|날짜|참조|제목)\s*[:：]", re.I)
REPLY_MARK = re.compile(r"^\s*(-{2,}\s*original message|-{2,}\s*forwarded message|-{2,}\s*원본 메시지|on .{5,80} wrote:|_{8,})", re.I)


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)>", "\n", s)
    s = re.sub(r"(?i)</t[dh]>", "\t", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t ]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def message_text(msg) -> str:
    """본문(텍스트 우선, 없으면 HTML→텍스트). 첨부 메일(message/rfc822)은 제외."""
    plain, htm = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() == "message/rfc822":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            txt = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            txt = payload.decode(part.get_content_charset() or "utf-8", "replace")
        (plain if ctype == "text/plain" else htm).append(txt)
    if plain and sum(len(p.strip()) for p in plain) > 20:
        return "\n".join(plain).strip()
    return html_to_text("\n".join(htm)) if htm else "\n".join(plain).strip()


def _sent_at(msg) -> datetime:
    try:
        d = parsedate_to_datetime(msg["date"])
        return d.astimezone().replace(tzinfo=None) if d.tzinfo else d
    except Exception:
        return datetime.now()


# 메일 주인(전달한 직원) 찾는 순서
#  - '전달'·'첨부로 전달'한 메일: 보낸 사람(From)이 직원
#  - 자동 전체 전달(원래 메일이 그대로 넘어옴): 받는 사람(To/Cc)·전달 표시 머리글 쪽이 직원
FWD_ORDER = ("from", "resent-from", "x-forwarded-for", "x-forwarded-to", "delivered-to", "x-original-to", "to", "cc")
REDIRECT_ORDER = ("resent-from", "x-forwarded-for", "x-forwarded-to", "delivered-to", "x-original-to", "to", "cc",
                  "from", "return-path")


def header_addrs(msg, order=FWD_ORDER) -> list:
    """메일 주인 후보 주소들 (앞에 있을수록 우선)."""
    out = []
    for h in order:
        for v in msg.get_all(h, []) or []:
            for _, a in getaddresses([str(v)]):
                a = a.lower().strip()
                if a and a not in out:
                    out.append(a)
    return out


def is_bulk(msg) -> bool:
    """광고·뉴스레터·자동 발송 메일."""
    prec = str(msg.get("precedence", "") or "").lower()
    auto = str(msg.get("auto-submitted", "") or "").lower()
    return bool(msg.get("list-unsubscribe") or msg.get("list-id") or prec in ("bulk", "list", "junk")
                or (auto and auto != "no"))


def split_messages(raw: bytes) -> list:
    """원본 메일 → [(메일, 주인 후보 주소들)]. '첨부로 전달'한 메일이 여러 개면 각각 꺼낸다."""
    outer = email.message_from_bytes(raw, policy=policy.default)
    inner = [p.get_payload()[0] if isinstance(p.get_payload(), list) else p.get_payload()
             for p in outer.walk() if p.get_content_type() == "message/rfc822"]
    inner = [m for m in inner if hasattr(m, "get")]
    if inner:
        owners = header_addrs(outer)
        return [(m, owners + [a for a in header_addrs(m, REDIRECT_ORDER) if a not in owners]) for m in inner]
    forwarded = re.match(r"^\s*(fw|fwd|전달)\s*[:：]", str(outer.get("subject", "") or ""), re.I)
    return [(outer, header_addrs(outer, FWD_ORDER if forwarded else REDIRECT_ORDER))]


def clean_subject(s: str) -> str:
    s = re.sub(r"^\s*((re|fw|fwd|답장|전달|회신)\s*[:：]\s*|\[[^\]]{0,30}\]\s*)+", "", str(s or ""), flags=re.I)
    return s.strip()


def forwarded_header(text: str) -> dict:
    """본문 안에 붙은 전달 머리글(From:/보낸 사람:, Subject:/제목:)에서 원래 보낸 사람·제목."""
    out = {}
    for line in text.splitlines()[:80]:
        m = re.match(r"^\s*(from|보낸\s*사람)\s*[:：]\s*(.+)$", line, re.I)
        if m and "from" not in out:
            out["from"] = m[2].strip()
        m = re.match(r"^\s*(subject|제목)\s*[:：]\s*(.+)$", line, re.I)
        if m and "subject" not in out:
            out["subject"] = m[2].strip()
    return out


def _mk(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _year_fix(d: date, ref: date) -> date:
    """연도가 없는 날짜: 메일 날짜보다 두 달 넘게 이전이면 다음 해로."""
    if d and d < ref - timedelta(days=60):
        return _mk(d.year + 1, d.month, d.day) or d
    return d


def find_dates(text: str, ref: date) -> list:
    """[(날짜, 시작, 끝)] — 숫자 날짜, 한글 날짜, 영어 월 이름, 내일/모레."""
    out = []
    pats = [
        (r"(?<!\d)(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})(?!\d)\s*일?", lambda m: _mk(int(m[1]), int(m[2]), int(m[3]))),
        (r"(?<![\d.])(\d{1,2})\s*월\s*(\d{1,2})\s*일", lambda m: _year_fix(_mk(ref.year, int(m[1]), int(m[2])), ref)),
        (r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s\-]+" + MON + r"(?:[\s\-,]+(20\d{2}|\d{2})(?!\d))?",
         lambda m: _yr(_mk(_y(m[3], ref), MONTHS[m[2][:3].lower()], int(m[1])), m[3], ref)),
        (MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?(?!\d)(?:,?\s*(20\d{2}))?",
         lambda m: _yr(_mk(_y(m[3], ref), MONTHS[m[1][:3].lower()], int(m[2])), m[3], ref)),
        (r"(?<![\d/.:])(\d{1,2})/(\d{1,2})(?:/(20\d{2}|\d{2}))?(?![\d/])", _slash(ref)),
    ]
    for pat, fn in pats:
        for m in re.finditer(pat, text, re.I):
            if any(s <= m.start() < e for _, s, e in out):
                continue
            try:
                d = fn(m)
            except (KeyError, ValueError, TypeError):
                d = None
            if d:
                out.append((d, m.start(), m.end()))
    for word, days in (("내일", 1), ("모레", 2), ("tomorrow", 1)):
        for m in re.finditer(word, text, re.I):
            out.append((ref + timedelta(days=days), m.start(), m.end()))
    return sorted(out, key=lambda x: x[1])


def _y(y, ref):
    if not y:
        return ref.year
    y = int(y)
    return y + 2000 if y < 100 else y


def _yr(d, y, ref):
    return d if y else _year_fix(d, ref)


def _slash(ref):
    def fn(m):
        a, b = int(m[1]), int(m[2])
        mo, dd = (a, b) if a <= 12 else (b, a)      # 13/10 처럼 앞이 12보다 크면 일/월
        d = _mk(_y(m[3], ref), mo, dd)
        return d if m[3] else _year_fix(d, ref)
    return fn


def find_time(s: str):
    m = re.search(r"(오전|오후)\s*(\d{1,2})\s*(?:시\s*(?:(\d{1,2})\s*분|(반))?|:(\d{2}))", s)
    if m:
        h = int(m[2]) % 12 + (12 if m[1] == "오후" else 0)
        mi = int(m[3] or m[5] or (30 if m[4] else 0))
        return f"{h:02d}:{mi:02d}"
    m = re.search(r"(?<!\d)(\d{1,2})\s*시\s*(?:(\d{1,2})\s*분|(반))?", s)
    if m and int(m[1]) <= 23:
        h = int(m[1])
        h = h + 12 if 1 <= h <= 7 else h          # '3시' → 오후 3시로 봄
        return f"{h:02d}:{int(m[2] or (30 if m[3] else 0)):02d}"
    m = re.search(r"(?<![\d:])(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?", s, re.I)
    if m and int(m[1]) <= 23 and int(m[2]) < 60:
        h = int(m[1])
        if m[3] and m[3][0].lower() == "p" and h < 12:
            h += 12
        return f"{h:02d}:{m[2]}"
    m = re.search(r"(?<!\d)(\d{1,2})\s*(am|pm)\b", s, re.I)
    if m and int(m[1]) <= 12:
        h = int(m[1]) % 12 + (12 if m[2].lower() == "pm" else 0)
        return f"{h:02d}:00"
    return None


def body_lines(text: str) -> list:
    """머리글 줄·인용(>) 줄을 빼고, 회신 기록이 두 번 나오면 그 뒤는 버린다 (예전 메일의 날짜 제외)."""
    out, marks = [], 0
    for line in text.splitlines():
        if REPLY_MARK.match(line):
            marks += 1
            if marks >= 2:
                break
            continue
        if line.lstrip().startswith(">") or HEADER_LINE.match(line):
            continue
        if line.strip():
            out.append(line.strip())
    return out


def own_text(text: str) -> str:
    """회신 기록(인용된 예전 메일) 앞까지 = 이 메일에서 새로 쓴 부분."""
    out = []
    for line in text.splitlines():
        started = any(x.strip() for x in out)
        if REPLY_MARK.match(line) or re.match(r"^\s*(from|보낸\s*사람)\s*[:：]", line, re.I):
            if started:
                break
            continue
        if not started and HEADER_LINE.match(line):
            continue                 # 전달 머리글(Date:/Subject:/To: …)은 본문이 아님
        if not line.lstrip().startswith(">"):
            out.append(line)
    return "\n".join(out).strip()


def thread_subject(subject: str) -> str:
    """같은 대화 판별용 제목: RE/FW/[태그]·띄어쓰기·대소문자 무시."""
    return re.sub(r"\s+", " ", clean_subject(subject)).strip().lower()


def extract(subject: str, text: str, sent: datetime) -> list:
    """일정 후보 목록: [{kind, date, time, label, context, bl_no}]"""
    ref = sent.date()
    subj_kind = next((k for k, p in KEYWORDS.items() if re.search(p, subject, re.I)), None)
    lines = [subject] + body_lines(text)
    bl = re.search(r"(?:B/?L|BL|AWB|H\.?B/?L|M\.?B/?L)\s*(?:No\.?|#|번호)?\s*[:：.]?\s*([A-Z]{2,}[A-Z0-9\-]{5,})", text, re.I)
    cands, seen = [], set()
    for i, line in enumerate(lines[:400]):
        for d, s, e in find_dates(line, ref):
            if not (ref - timedelta(days=7) <= d <= ref + timedelta(days=400)):
                continue
            before = line[max(0, s - 60):s]
            near = before + " " + line[e:e + 40]
            ctx = (lines[i - 1] + " " if i > 0 else "") + near
            kind = None
            for k in ("ship", "event", "task"):        # 날짜 바로 앞 단어 우선
                if re.search(KEYWORDS[k], before[-25:], re.I):
                    kind = k
                    break
            if not kind:
                kind = next((k for k in ("ship", "event", "task") if re.search(KEYWORDS[k], near, re.I)), None)
            if not kind:
                kind = next((k for k in ("ship", "event", "task") if re.search(KEYWORDS[k], ctx, re.I)), None)
            t = find_time(line[e:e + 25]) or find_time(line[max(0, s - 15):s])
            if not kind:
                kind = "event" if t else subj_kind
            if not kind:
                continue
            label = ""
            if kind == "ship":
                lab = r"(e\.?t\.?a|e\.?t\.?d|도착|입항|입고|선적|출항|통관)"
                m = re.search(lab, line[e:e + 12], re.I) or re.search(lab + r"(?!.*" + lab + ")", before, re.I) \
                    or re.search(lab, near, re.I)
                label = (m[1].upper().replace(".", "") if m else "")
            key = (kind, d, t)
            if key in seen:
                continue
            seen.add(key)
            cands.append({"kind": kind, "date": d.isoformat(), "time": t, "label": label,
                          "context": line.strip()[:160], "bl_no": bl[1] if bl and kind == "ship" else ""})
    # 선적: ETA 가 있으면 ETD 는 뒤로
    cands.sort(key=lambda c: ({"ship": 0, "event": 1, "task": 2}[c["kind"]], c["label"] == "ETD", c["date"]))
    return cands[:12]


def parse_raw(raw: bytes) -> list:
    """원본 메일 → 저장할 항목 [{msg_id, from_addr, from_name, subject, sent_at, body, candidates, forwarder}]"""
    items = []
    for msg, owners in split_messages(raw):
        text = message_text(msg)
        subject = str(msg.get("subject", "") or "")
        name, addr = parseaddr(str(msg.get("from", "") or ""))
        fh = forwarded_header(text) if re.match(r"^\s*(fw|fwd|전달)\s*[:：]", subject, re.I) else {}
        if fh.get("from"):
            n2, a2 = parseaddr(fh["from"])
            name, addr = (n2 or fh["from"]), a2 or addr
        if fh.get("subject"):
            subject = fh["subject"]
        sent = _sent_at(msg)
        subject = clean_subject(subject)
        refs = re.findall(r"<[^<>\s]+>", f"{msg.get('in-reply-to', '') or ''} {msg.get('references', '') or ''}")
        # 중복 판별용 지문: 보낸 사람 + 제목 + 이 메일에서 새로 쓴 내용 (전달 방식이 달라도 같게 나오도록)
        core = re.sub(r"\s+", "", own_text(text) or text)[:1500].lower()
        fp = hashlib.sha1(f"{(addr or '').lower()}|{thread_subject(subject)}|{core}".encode()).hexdigest()
        items.append({"msg_id": str(msg.get("message-id", "") or "").strip()[:250], "refs": refs[-20:], "fp": fp,
                      "from_addr": (addr or "").lower(), "from_name": name or addr or "",
                      "subject": subject[:300], "sent_at": sent.strftime("%Y-%m-%d %H:%M"),
                      "body": text[:20000], "candidates": extract(subject, text, sent), "owners": owners, "bulk": is_bulk(msg),
                      "to": ", ".join(a for _, a in getaddresses([str(msg.get("to", "") or "")]))[:300]})
    return items


# ---- B/L · 항공 운송장(AWB) 번호 찾기
BL_LABEL = re.compile(
    r"(?P<lab>M\s*\.?\s*B\s*/?\s*L|H\s*\.?\s*B\s*/?\s*L|MASTER\s*B/?L|HOUSE\s*B/?L|M?AWB|H?AWB|B\s*/\s*L|\bBL\b|"
    r"BILL\s+OF\s+LADING|AIR\s*WAY\s*BILL|운송장|선하증권|비엘)"
    r"\s*(?:NO\.?|NUMBER|#|번호)?\s*[:：.\-]?\s*(?P<num>[A-Z]{4}\s\d{6,12}(?![\w\-])|\d{3}[\s\-]\d{8}(?!\d)|[A-Z0-9][A-Z0-9\-]{6,24}[A-Z0-9])",
    re.I)
AWB_BARE = re.compile(r"(?<![\d\-])(\d{3})[\s\-]?(\d{8})(?![\d\-])")


def awb_ok(num: str) -> bool:
    """항공 운송장 11자리: 뒤 8자리 중 앞 7자리를 7로 나눈 나머지가 마지막 숫자."""
    d = re.sub(r"\D", "", num)
    return len(d) == 11 and int(d[3:10]) % 7 == int(d[10])


def norm_bl(num: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(num or "").upper())


def find_bl_numbers(text: str) -> list:
    """[(번호, 'M'|'H'|'')] — 'B/L No:' 같은 표시가 붙은 번호 + 표시 없이 쓴 항공 운송장(검증 숫자가 맞는 것)."""
    out, seen = [], set()
    for m in BL_LABEL.finditer(text or ""):
        num = norm_bl(m["num"])
        if len(num) < 8 or sum(ch.isdigit() for ch in num) < 5 or num in seen:
            continue
        if num.isdigit() and len(num) not in (11, 12) and not re.search(r"B\s*/?\s*L|비엘|선하", m["lab"], re.I):
            continue                     # 숫자만 있고 길이가 이상하면 (전화번호 등) 제외
        lab = m["lab"].upper().replace(" ", "")
        kind = "M" if lab.startswith(("M", "MASTER")) else "H" if lab.startswith(("H", "HOUSE")) else ""
        seen.add(num)
        out.append((num, kind))
    for m in AWB_BARE.finditer(text or ""):
        num = m[1] + m[2]
        if num not in seen and awb_ok(num):
            seen.add(num)
            out.append((num, ""))
    return out[:10]
