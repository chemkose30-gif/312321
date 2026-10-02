"""이카운트 ERP Open API 클라이언트 (표준 라이브러리만 사용).

확인된 사항
- Zone 조회 : POST https://{oapi|sboapi}.ecount.com/OAPI/V2/Zone            {"COM_CODE"}
- 로그인    : POST https://{oapi|sboapi}{ZONE}.ecount.com/OAPI/V2/OAPILogin {COM_CODE, USER_ID, API_CERT_KEY, LAN_TYPE, ZONE}
- 이후 호출 : POST https://{oapi|sboapi}{ZONE}.ecount.com/OAPI/V2/{Domain}/{Method}?SESSION_ID=...
- 품목 조회 : InventoryBasic/GetBasicProductsList (응답 PROD_CD, PROD_DES, SIZE_DES, UNIT ...)
- 판매 입력 : Sale/SaveSale  {"SaleList": [{"BulkDatas": {...}}]}
- 테스트 인증키는 sboapi(테스트 서버), 실서비스 키는 oapi 에서만 동작하며 서로 호환되지 않는다.
- 등록된 IP 에서만 동작한다.
견적서 입력 경로/목록 키는 관리자 설정에서 바꿀 수 있게 했다 (API 매뉴얼과 다르면 화면에서 수정).
"""
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request

DEFAULT_PATHS = {
    "products": "InventoryBasic/GetBasicProductsList",
    "quotation": "Quotation/SaveQuotation",
    "quotation_list_key": "QuotationList",
    "sale": "Sale/SaveSale",
    "sale_list_key": "SaleList",
}


class EcountError(RuntimeError):
    pass


def mask(text: str, *secrets) -> str:
    text = re.sub(r"(SESSION_ID=)[^&\s\"']+", r"\1***", str(text))
    for s in secrets:
        if s and len(str(s)) >= 4:
            text = text.replace(str(s), "***")
    return text


def _find(obj, key):
    """중첩된 응답에서 key 를 처음 찾아 반환."""
    if isinstance(obj, dict):
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
        for v in obj.values():
            r = _find(v, key)
            if r not in (None, ""):
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, key)
            if r not in (None, ""):
                return r
    return None


def _maybe_json(v):
    """이카운트는 ResultDetails/SlipNos 를 JSON '문자열'로 주기도 한다 → 실제 목록으로."""
    if isinstance(v, str) and v.strip()[:1] in ("[", "{"):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def error_messages(resp) -> list:
    """이카운트 응답에서 사람이 읽을 오류 메시지를 모은다."""
    msgs = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("Message", "MESSAGE", "ErrorMessage", "TotalError", "RESULT_MSG") and v:
                    msgs.append(str(v))
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    if isinstance(resp, dict):
        walk(resp.get("Error"))
        walk(resp.get("Errors"))
        data = resp.get("Data")
        if isinstance(data, dict):
            details = _maybe_json(data.get("ResultDetails"))
            if isinstance(details, list):   # 성공한 묶음의 "OK" 문구는 오류가 아니므로 실패한 것만
                details = [d for d in details if not (isinstance(d, dict) and d.get("IsSuccess"))]
            walk(details)
    return list(dict.fromkeys(m.strip() for m in msgs if m.strip()))


class Client:
    def __init__(self, cfg: dict):
        self.com_code = (cfg.get("com_code") or "").strip()
        self.user_id = (cfg.get("user_id") or "").strip()
        self.key = (cfg.get("api_key") or "").strip()
        self.is_test = str(cfg.get("is_test", "1")) in ("1", "true", "True")
        self.paths = {k: (cfg.get("path_" + k) or v).strip() for k, v in DEFAULT_PATHS.items()}
        self.zone = ""
        self.session = ""
        self.session_at = 0.0
        self._lock = threading.Lock()
        self._ctx = ssl.create_default_context()

    # ------------------------------------------------------------ 기본
    def _sub(self):
        return "sboapi" if self.is_test else "oapi"

    def _safe(self, text):
        return mask(text, self.key, self.session)

    def _post(self, url, body, timeout=30):
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     method="POST", headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as r:
                raw = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise EcountError(self._safe(f"HTTP {e.code}: {detail}")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise EcountError(self._safe(f"이카운트 서버 연결 실패: {e}")) from None
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise EcountError(self._safe("이카운트 응답을 해석할 수 없습니다: " + raw[:300])) from None

    # ------------------------------------------------------------ 인증
    def login(self, force=False):
        with self._lock:
            if self.session and not force and time.time() - self.session_at < 20 * 60:
                return self.session
            missing = [n for n, v in (("회사코드", self.com_code), ("사용자 ID", self.user_id),
                                      ("API 인증키", self.key)) if not v]
            if missing:
                raise EcountError("이카운트 설정이 비어 있습니다: " + ", ".join(missing))
            if not self.zone:
                resp = self._post(f"https://{self._sub()}.ecount.com/OAPI/V2/Zone", {"COM_CODE": self.com_code})
                zone = _find(resp, "ZONE")
                if not zone:
                    msg = "; ".join(error_messages(resp)) or json.dumps(resp, ensure_ascii=False)[:300]
                    raise EcountError(self._safe("Zone 조회 실패 (회사코드 확인): " + msg))
                self.zone = str(zone).strip()
            resp = self._post(f"https://{self._sub()}{self.zone}.ecount.com/OAPI/V2/OAPILogin", {
                "COM_CODE": self.com_code, "USER_ID": self.user_id, "API_CERT_KEY": self.key,
                "LAN_TYPE": "ko-KR", "ZONE": self.zone,
            })
            sid = _find(resp, "SESSION_ID")
            if not sid:
                msg = "; ".join(error_messages(resp)) or json.dumps(resp, ensure_ascii=False)[:300]
                hint = (" (테스트 인증키는 '테스트 서버' 모드에서만, 실서비스 키는 '실서비스' 모드에서만 동작합니다."
                        " 이카운트에 서버 IP가 등록되어 있는지도 확인하세요.)")
                raise EcountError(self._safe("로그인 실패: " + msg + hint))
            self.session, self.session_at = str(sid), time.time()
            return self.session

    def call(self, path: str, body: dict):
        """세션으로 API 호출. 세션 만료로 보이면 한 번 재로그인 후 재시도."""
        for attempt in range(2):
            sid = self.login(force=attempt > 0)
            url = f"https://{self._sub()}{self.zone}.ecount.com/OAPI/V2/{path}?SESSION_ID={sid}"
            try:
                resp = self._post(url, body)
            except EcountError as e:
                if attempt == 0 and ("401" in str(e) or "세션" in str(e)):
                    continue
                raise
            msgs = " ".join(error_messages(resp))
            if attempt == 0 and ("세션" in msgs or "SESSION" in msgs.upper()) and not _find(resp, "SuccessCnt"):
                continue
            return resp
        return resp

    # ------------------------------------------------------------ 기능
    def products(self) -> list:
        resp = self.call(self.paths["products"], {})
        data = resp.get("Data") if isinstance(resp, dict) else None
        rows = None
        if isinstance(data, dict):
            for k in ("Result", "Datas"):
                if isinstance(data.get(k), list):
                    rows = data[k]
                    break
        elif isinstance(data, list):
            rows = data
        if rows is None:
            msg = "; ".join(error_messages(resp)) or json.dumps(resp, ensure_ascii=False)[:300]
            raise EcountError(self._safe("품목 조회 실패: " + msg))
        out = []
        for r in rows:
            code = str(r.get("PROD_CD") or "").strip()
            if not code:
                continue
            price = r.get("OUT_PRICE") or r.get("PRICE") or 0
            try:
                price = float(str(price).replace(",", "") or 0)
            except ValueError:
                price = 0
            out.append({"code": code, "name": str(r.get("PROD_DES") or "").strip(),
                        "spec": str(r.get("SIZE_DES") or "").strip(), "unit": str(r.get("UNIT") or "").strip(),
                        "price": price})
        return out

    def save_slip(self, kind: str, lines: list) -> dict:
        """kind='quotation'|'sale'. lines 는 BulkDatas dict 목록(같은 UPLOAD_SER_NO = 한 전표).
        반환: {"ok": bool, "slip_nos": [...], "messages": [...], "raw": resp}"""
        path, list_key = self.paths[kind], self.paths[kind + "_list_key"]
        resp = self.call(path, {list_key: [{"BulkDatas": ln} for ln in lines]})
        data = resp.get("Data") if isinstance(resp, dict) else {}
        data = data if isinstance(data, dict) else {}
        try:
            fail = int(str(data.get("FailCnt", "0") or 0))
            success = int(str(data.get("SuccessCnt", "0") or 0))
        except ValueError:
            fail, success = 1, 0
        slips = _maybe_json(data.get("SlipNos")) or []
        if isinstance(slips, str):
            slips = [s for s in re.split(r"[,\s]+", slips.strip("[]\"' ")) if s.strip("\"'")]
        slips = [str(x).strip("\"' ") for x in slips if str(x).strip("\"' ")]
        messages = error_messages(resp)
        ok = success > 0 and fail == 0 and not (isinstance(resp, dict) and resp.get("Error"))
        return {"ok": ok, "slip_nos": slips, "messages": messages, "quota": str(data.get("QUANTITY_INFO") or ""),
                "raw": json.loads(self._safe(json.dumps(resp, ensure_ascii=False)))}


_client_cache = {"sig": None, "client": None}


def get_client(cfg: dict) -> Client:
    """설정이 바뀌지 않았으면 같은 클라이언트(세션)를 재사용."""
    sig = json.dumps(cfg, sort_keys=True)
    if _client_cache["sig"] != sig:
        _client_cache["sig"], _client_cache["client"] = sig, Client(cfg)
    return _client_cache["client"]


def build_lines(q: dict, items: list, cust_cd: str, wh_cd: str, io_date: str, emp_cd: str = "",
                kind: str = "sale") -> list:
    """TeamHub 견적서 → 이카운트 BulkDatas 목록 (한 전표)."""
    head = {}
    if kind == "quotation":   # 견적서입력 상단 항목 (입력화면에 있는 항목만 반영됨)
        head = {"TTL_CTT": (q.get("title") or "")[:200], "REF_DES": (q.get("customer_contact") or "")[:200],
                "COLL_TERM": (q.get("payment_terms") or "")[:200], "AGREE_TERM": (q.get("valid_until") or "")[:200]}
        head = {k: v for k, v in head.items() if v}
    lines = []
    for it in items:
        ln = {
            "UPLOAD_SER_NO": "1",
            "IO_DATE": io_date.replace("-", ""),
            "CUST": cust_cd,
            "CUST_DES": q["customer_name"],
            "PROD_CD": it["prod_cd"],
            "PROD_DES": it["name"],
            "SIZE_DES": it["spec"],
            "QTY": _num(it["qty"]),
            "PRICE": _num(it["unit_price"]),
            "SUPPLY_AMT": str(it["supply"]),
            "VAT_AMT": str(it["vat"]),
            "REMARKS": it["note"],
        }
        ln.update(head)
        if wh_cd:
            ln["WH_CD"] = wh_cd
        if emp_cd:
            ln["EMP_CD"] = emp_cd
        lines.append(ln)
    return lines


def _num(v) -> str:
    v = float(v or 0)
    return str(int(v)) if v == int(v) else str(v)
