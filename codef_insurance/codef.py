"""CODEF 내보험다보여(credit4u) 클라이언트 - CLI 스크립트와 웹앱이 같이 사용"""

import base64
import json
import os
import time
import urllib.parse

import httpx
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

BASE_URLS = {
    "sandbox": "https://sandbox.codef.io",       # 고정 샘플 응답 (실데이터 X)
    "demo": "https://development.codef.io",      # 실데이터, 호출 횟수 제한
    "production": "https://api.codef.io",
}
OAUTH_URL = "https://oauth.codef.io/oauth/token"
CONTRACT_INFO_PATH = "/v1/kr/insurance/0001/credit4u/contract-info"

# 통신사 코드: 0=SKT, 1=KT, 2=LG U+ (알뜰폰은 망 기준으로 선택)
TELECOM = {"skt": "0", "kt": "1", "lgu": "2"}

OK = "CF-00000"
NEED_2WAY = "CF-03002"
PW_LOCKED = "CF-12802"


def env() -> str:
    return os.environ.get("CODEF_ENV", "demo")


_token: str | None = None
_token_exp = 0.0


def get_token() -> str:
    global _token, _token_exp
    if _token and time.time() < _token_exp:
        return _token
    cid = os.environ.get("CODEF_CLIENT_ID", "")
    secret = os.environ.get("CODEF_CLIENT_SECRET", "")
    if not (cid and secret):
        raise RuntimeError("CODEF_CLIENT_ID / CODEF_CLIENT_SECRET 환경변수를 설정하세요.")
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    resp = httpx.post(
        OAUTH_URL,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        content="grant_type=client_credentials&scope=read",
        timeout=30,
    )
    resp.raise_for_status()
    _token = resp.json()["access_token"]
    _token_exp = time.time() + 60 * 60  # 토큰은 1주일 유효하지만 1시간마다 갱신
    return _token


def rsa_encrypt(plain: str) -> str:
    """비밀번호 등 민감정보는 CODEF 공개키로 RSA(PKCS1 v1.5) 암호화해서 보내야 한다."""
    if env() == "mock":
        return "mock-encrypted"
    pub = os.environ.get("CODEF_PUBLIC_KEY", "")
    if not pub:
        raise RuntimeError("CODEF_PUBLIC_KEY 환경변수를 설정하세요.")
    key = RSA.import_key(base64.b64decode(pub))
    return base64.b64encode(PKCS1_v1_5.new(key).encrypt(plain.encode())).decode()


def post(path: str, params: dict) -> dict:
    """CODEF 는 body 를 URL 인코딩된 JSON 으로 받고, 응답도 URL 인코딩된 JSON 으로 준다."""
    if env() == "mock":
        return _mock(params)
    resp = httpx.post(
        BASE_URLS[env()] + path,
        headers={"Authorization": f"Bearer {get_token()}", "Content-Type": "application/x-www-form-urlencoded"},
        content=urllib.parse.quote(json.dumps(params, ensure_ascii=False)),
        timeout=200,  # 기관 응답 + 인증 대기 시간을 고려
    )
    resp.raise_for_status()
    try:
        return json.loads(urllib.parse.unquote_plus(resp.text))
    except json.JSONDecodeError:
        return json.loads(resp.text)


def contract_params(user: dict) -> dict:
    """1단계 요청 파라미터. user: id, password(평문), userName, birthDate, phoneNo, telecom, authMethod"""
    return {
        "organization": "0001",
        "id": user["id"],
        "password": rsa_encrypt(user["password"]),
        "type": "0",
        "userName": user["userName"],
        "birthDate": user["birthDate"],      # YYYYMMDD
        "phoneNo": user["phoneNo"],          # 숫자만
        "telecom": user["telecom"],
        "authMethod": user["authMethod"],    # "1"=PASS 앱 인증, "0"=SMS
        "timeOut": "170",
    }


def two_way_info(result: dict) -> dict:
    data = result.get("data", {})
    return {
        "jobIndex": data.get("jobIndex", 0),
        "threadIndex": data.get("threadIndex", 0),
        "jti": data.get("jti", ""),
        "twoWayTimestamp": data.get("twoWayTimestamp"),
    }


def confirm_params(params: dict, two_way: dict, sms_no: str = "") -> dict:
    """2단계(추가인증 완료) 요청 파라미터"""
    is_pass = params["authMethod"] == "1"
    return {
        **params,
        "smsAuthNo": "" if is_pass else sms_no,
        "simpleAuth": "1" if is_pass else "0",
        "is2Way": True,
        "twoWayInfo": two_way,
    }


def code_of(result: dict) -> str | None:
    return result.get("result", {}).get("code")


def message_of(result: dict) -> str:
    if code_of(result) == PW_LOCKED:
        return "비밀번호 오류 횟수 초과 - 내보험다보여 사이트에서 비밀번호를 재설정하세요."
    return result.get("result", {}).get("message") or "알 수 없는 오류"


def _mock(params: dict) -> dict:
    """CODEF_ENV=mock: 키 없이 화면 흐름을 시험하기 위한 가짜 응답 (실제 데이터 아님)"""
    if not params.get("is2Way"):
        return {"result": {"code": NEED_2WAY, "message": "추가인증 필요"},
                "data": {"jobIndex": 0, "threadIndex": 0, "jti": "mock", "twoWayTimestamp": 0}}
    return {"result": {"code": OK, "message": "성공"}, "data": {"resContractList": [
        {"resCompanyName": "가상생명", "resInsuranceName": "[샘플] 종합건강보험", "resContractStatus": "정상",
         "resContractStartDate": "20200101", "resContractEndDate": "20500101", "resMonthlyPremium": 52000},
        {"resCompanyName": "가상화재", "resInsuranceName": "[샘플] 실손의료보험", "resContractStatus": "정상",
         "resContractStartDate": "20230301", "resContractEndDate": "20330301", "resMonthlyPremium": 31000},
    ]}}
