"""
CODEF '내보험다보여(credit4u)' 보험 계약정보 조회 스크립트

흐름
  1) OAuth 토큰 발급 (client_credentials)
  2) 계약정보 조회 요청 -> CF-03002(추가인증 필요) 응답
  3) 휴대폰으로 온 PASS 승인 / SMS 인증번호 입력
  4) twoWayInfo 를 담아 같은 엔드포인트로 재요청 -> CF-00000 이면 성공

사전 준비
  - codef.io 가입 후 client_id / client_secret / public key 발급
  - 내보험다보여(cont.insure.or.kr) 아이디/비밀번호 (회원가입 필요)
  - pip install -r requirements.txt
"""

import base64
import json
import os
import sys
import urllib.parse
from getpass import getpass

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

BASE_URLS = {
    "sandbox": "https://sandbox.codef.io",       # 고정 샘플 응답 (실데이터 X)
    "demo": "https://development.codef.io",      # 실데이터, 호출 횟수 제한
    "production": "https://api.codef.io",
}
OAUTH_URL = "https://oauth.codef.io/oauth/token"
CONTRACT_INFO_PATH = "/v1/kr/insurance/0001/credit4u/contract-info"

CODEF_ENV = os.environ.get("CODEF_ENV", "demo")
CLIENT_ID = os.environ.get("CODEF_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CODEF_CLIENT_SECRET", "")
PUBLIC_KEY = os.environ.get("CODEF_PUBLIC_KEY", "")

# 통신사 코드: 0=SKT, 1=KT, 2=LG U+ (알뜰폰은 망 기준으로 선택)
TELECOM = {"skt": "0", "kt": "1", "lgu": "2"}


def get_token() -> str:
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        OAUTH_URL,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials&scope=read",
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def rsa_encrypt(plain: str) -> str:
    """비밀번호 등 민감정보는 CODEF 공개키로 RSA(PKCS1 v1.5) 암호화해서 보내야 한다."""
    key = RSA.import_key(base64.b64decode(PUBLIC_KEY))
    return base64.b64encode(PKCS1_v1_5.new(key).encrypt(plain.encode())).decode()


def codef_post(token: str, path: str, params: dict) -> dict:
    """CODEF 는 body 를 URL 인코딩된 JSON 으로 받고, 응답도 URL 인코딩된 JSON 으로 준다."""
    resp = requests.post(
        BASE_URLS[CODEF_ENV] + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=urllib.parse.quote(json.dumps(params, ensure_ascii=False)),
        timeout=200,  # 기관 응답 + 인증 대기 시간을 고려
    )
    resp.raise_for_status()
    text = resp.text
    try:
        return json.loads(urllib.parse.unquote_plus(text))
    except json.JSONDecodeError:
        return json.loads(text)


def fetch_contracts(token: str, user: dict) -> dict:
    params = {
        "organization": "0001",
        "id": user["id"],
        "password": rsa_encrypt(user["password"]),
        "type": "0",
        "userName": user["userName"],
        "birthDate": user["birthDate"],  # YYYYMMDD
        "phoneNo": user["phoneNo"],      # 숫자만
        "telecom": user["telecom"],
        "authMethod": user["authMethod"],  # "1"=PASS 앱 인증, "0"=SMS
        "timeOut": "170",
    }

    result = codef_post(token, CONTRACT_INFO_PATH, params)
    code = result.get("result", {}).get("code")

    if code == "CF-03002":  # 추가인증 필요 (2-way)
        data = result.get("data", {})
        two_way_info = {
            "jobIndex": data.get("jobIndex", 0),
            "threadIndex": data.get("threadIndex", 0),
            "jti": data.get("jti", ""),
            "twoWayTimestamp": data.get("twoWayTimestamp"),
        }
        is_pass = user["authMethod"] == "1"
        if is_pass:
            input("휴대폰 PASS 앱에서 인증을 승인한 뒤 Enter 를 누르세요...")
            sms_no = ""
        else:
            sms_no = input("문자로 받은 인증번호를 입력하세요: ").strip()

        result = codef_post(token, CONTRACT_INFO_PATH, {
            **params,
            "smsAuthNo": sms_no,
            "simpleAuth": "1" if is_pass else "0",
            "is2Way": True,
            "twoWayInfo": two_way_info,
        })
        code = result.get("result", {}).get("code")

    if code != "CF-00000":
        msg = result.get("result", {}).get("message")
        if code == "CF-12802":
            msg = "비밀번호 오류 횟수 초과 - 내보험다보여 사이트에서 비밀번호를 재설정하세요."
        raise RuntimeError(f"조회 실패 [{code}] {msg}")

    return result.get("data", {})


def print_summary(data: dict) -> None:
    """응답 필드명은 CODEF 개발가이드의 응답 명세로 꼭 확인하세요. 모르는 구조면 원본 JSON 파일을 참고."""
    contracts = data.get("resContractList") if isinstance(data, dict) else None
    if not contracts:
        print("계약 목록 필드를 찾지 못했습니다. insurance_result.json 원본을 확인하세요.")
        return
    for c in contracts:
        print(f"- [{c.get('resCompanyName', '?')}] {c.get('resInsuranceName', '?')}"
              f" | 상태: {c.get('resContractStatus', '?')}"
              f" | 기간: {c.get('resContractStartDate', '?')}~{c.get('resContractEndDate', '?')}")


def main() -> None:
    if not (CLIENT_ID and CLIENT_SECRET and PUBLIC_KEY):
        sys.exit("CODEF_CLIENT_ID / CODEF_CLIENT_SECRET / CODEF_PUBLIC_KEY 환경변수를 설정하세요.")

    telecom = input("통신사 (skt/kt/lgu): ").strip().lower()
    user = {
        "id": input("내보험다보여 아이디: ").strip(),
        "password": getpass("내보험다보여 비밀번호: "),
        "userName": input("이름: ").strip(),
        "birthDate": input("생년월일 (YYYYMMDD): ").strip(),
        "phoneNo": "".join(ch for ch in input("휴대폰 번호: ") if ch.isdigit()),
        "telecom": TELECOM.get(telecom, "0"),
        "authMethod": "0" if input("인증 방식 (pass/sms) [pass]: ").strip().lower() == "sms" else "1",
    }

    token = get_token()
    data = fetch_contracts(token, user)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "insurance_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n원본 응답 저장: {out}\n")
    print_summary(data)


if __name__ == "__main__":
    main()
