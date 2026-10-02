"""관세청 UNI-PASS 화물통관진행정보조회 (OpenAPI 'API001').

GET https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo
    ?crkyCn=인증키 & (mblNo=M B/L | hblNo=H B/L) & blYy=연도      또는  ?crkyCn=…&cargMtNo=화물관리번호
응답(XML) cargCsclPrgsInfoQryRtnVo
    tCnt 건수, ntceInfo 안내(오류)문구 — "[N00]" 으로 시작하면 여러 건(목록만 옴 → 화물관리번호로 다시 조회)
    cargCsclPrgsInfoQryVo   요약: cargMtNo 화물관리번호, prgsStts 진행상태, csclPrgsStts 통관진행상태, prnm 품명,
                            etprDt 입항일(YYYYMMDD), dsprNm 양륙항, shipNm 선박명, ttwg/wghtUt 중량, pckGcnt/pckUt 포장,
                            prcsDttm 처리일시(YYYYMMDDHHmmss), mblNo, hblNo, frwrEntsConm 포워더
    cargCsclPrgsInfoDtlQryVo 처리 이력: cargTrcnRelaBsopTpcd 처리구분(입항보고·하선신고·반입신고·수입신고수리·반출신고…),
                            prcsDttm 처리일시, shedNm 장치장명, rlbrDttm 반입일시(YYYY-MM-DD HH:mm:ss), rlbrCn 반입내용, dclrNo 신고번호
"""
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

ENDPOINT = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"


class UnipassError(Exception):
    pass


def _get(params: dict) -> ET.Element:
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = r.read()
    except urllib.error.URLError as e:
        raise UnipassError(f"UNI-PASS 에 연결하지 못했습니다: {e}") from e
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise UnipassError("UNI-PASS 응답을 읽지 못했습니다.") from e
    if root.tag != "cargCsclPrgsInfoQryRtnVo":
        inner = root.find(".//cargCsclPrgsInfoQryRtnVo")
        if inner is not None:
            root = inner
    return root


def _t(el, name: str) -> str:
    x = el.find(name) if el is not None else None
    return (x.text or "").strip() if x is not None and x.text else ""


def _ymd(v: str) -> str:
    v = (v or "").strip()
    if len(v) >= 8 and v[:8].isdigit():
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}"
    return v[:10]


def _dttm(v: str) -> str:
    """'20261013142000' / '2026-10-13 14:20:00' → '2026-10-13 14:20'"""
    v = (v or "").strip()
    if len(v) >= 12 and v[:12].isdigit():
        return f"{v[:4]}-{v[4:6]}-{v[6:8]} {v[8:10]}:{v[10:12]}"
    return v[:16]


def _check(root) -> int:
    info = _t(root, "ntceInfo")
    try:
        cnt = int(_t(root, "tCnt") or "-1")
    except ValueError:
        cnt = -1
    if cnt < 0:
        if "인증키" in info:
            raise UnipassError("UNI-PASS 인증키가 맞지 않습니다. 설정에서 인증키를 확인하세요.")
        raise UnipassError(info or "UNI-PASS 조회 오류")
    if cnt > 1 and "[N00]" not in info:
        cnt = 1
    return cnt


def _detail(root) -> dict:
    info = root.find("cargCsclPrgsInfoQryVo")
    events = []
    for ev in root.findall("cargCsclPrgsInfoDtlQryVo"):
        events.append({"kind": _t(ev, "cargTrcnRelaBsopTpcd"), "at": _dttm(_t(ev, "prcsDttm")),
                       "shed": _t(ev, "shedNm"), "in_at": _dttm(_t(ev, "rlbrDttm")), "memo": _t(ev, "rlbrCn"),
                       "dclr_no": _t(ev, "dclrNo")})
    events.sort(key=lambda e: e["at"])

    def first(test):
        return next((e for e in events if test(e["kind"].replace(" ", ""))), None)

    carry_in = first(lambda k: "반입" in k)
    cleared = first(lambda k: "수리" in k and ("수입신고" in k or "통관" in k))   # '입항보고 수리'는 제외
    out = first(lambda k: "반출" in k)
    return {
        "found": True,
        "cargo_no": _t(info, "cargMtNo"), "mbl_no": _t(info, "mblNo"), "hbl_no": _t(info, "hblNo"),
        "status": _t(info, "prgsStts"), "clearance": _t(info, "csclPrgsStts"), "item": _t(info, "prnm"),
        "arrived": _ymd(_t(info, "etprDt")), "port": _t(info, "dsprNm"), "ship": _t(info, "shipNm"),
        "weight": f"{_t(info, 'ttwg')} {_t(info, 'wghtUt')}".strip(),
        "packages": f"{_t(info, 'pckGcnt')} {_t(info, 'pckUt')}".strip(),
        "forwarder": _t(info, "frwrEntsConm"), "updated": _dttm(_t(info, "prcsDttm")),
        "in_at": (carry_in["in_at"] or carry_in["at"]) if carry_in else "",
        "shed": carry_in["shed"] if carry_in else "",
        "cleared_at": cleared["at"] if cleared else "",
        "out_at": out["at"] if out else "",
        "events": events,
    }


def lookup(key: str, mbl: str = "", hbl: str = "", year: int = 0) -> dict:
    """B/L 로 조회 → 상세. 못 찾으면 {"found": False}. 연도를 모르면 올해·작년 순으로 찾는다."""
    if not key:
        raise UnipassError("UNI-PASS 인증키가 설정되지 않았습니다.")
    mbl, hbl = (mbl or "").strip().upper(), (hbl or "").strip().upper()
    if not mbl and not hbl:
        raise UnipassError("M B/L 또는 H B/L 번호가 없습니다.")
    this_year = datetime.now().year
    years = [year] if year else [this_year, this_year - 1]
    for y in years:
        params = {"crkyCn": key, "blYy": str(y)}
        if hbl:
            params["hblNo"] = hbl
        if mbl:
            params["mblNo"] = mbl
        root = _get(params)
        cnt = _check(root)
        if cnt == 0 and hbl and mbl:          # 둘 다 넣어 못 찾으면 H B/L 만으로 한 번 더
            root = _get({"crkyCn": key, "blYy": str(y), "hblNo": hbl})
            cnt = _check(root)
        if cnt == 1:
            return _detail(root)
        if cnt > 1:
            # 여러 건(예: M B/L 하나에 H B/L 여러 개) → 입항일이 가장 최근인 화물을 화물관리번호로 다시 조회
            rows = sorted(root.findall("cargCsclPrgsInfoQryVo"), key=lambda r: _t(r, "etprDt"), reverse=True)
            cargo = _t(rows[0], "cargMtNo") if rows else ""
            if cargo:
                d = lookup_cargo(key, cargo)
                d["multiple"] = [{"cargo_no": _t(r, "cargMtNo"), "hbl_no": _t(r, "hblNo"), "mbl_no": _t(r, "mblNo"),
                                  "arrived": _ymd(_t(r, "etprDt"))} for r in rows]
                return d
    return {"found": False}


def lookup_cargo(key: str, cargo_no: str) -> dict:
    root = _get({"crkyCn": key, "cargMtNo": cargo_no})
    return _detail(root) if _check(root) >= 1 else {"found": False}
