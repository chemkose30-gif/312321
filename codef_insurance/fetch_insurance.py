"""
CODEF '내보험다보여(credit4u)' 보험 계약정보 조회 - 본인 1명 조회용 CLI
(여러 고객 조회는 app.py 웹 서비스를 사용)

흐름
  1) 계약정보 조회 요청 -> CF-03002(추가인증 필요) 응답
  2) 휴대폰으로 온 PASS 승인 / SMS 인증번호 입력
  3) twoWayInfo 를 담아 같은 엔드포인트로 재요청 -> CF-00000 이면 성공
"""

import json
import os
import sys
from getpass import getpass

import codef


def fetch_contracts(user: dict) -> dict:
    params = codef.contract_params(user)
    result = codef.post(codef.CONTRACT_INFO_PATH, params)

    if codef.code_of(result) == codef.NEED_2WAY:
        if user["authMethod"] == "1":
            input("휴대폰 PASS 앱에서 인증을 승인한 뒤 Enter 를 누르세요...")
            sms_no = ""
        else:
            sms_no = input("문자로 받은 인증번호를 입력하세요: ").strip()
        result = codef.post(codef.CONTRACT_INFO_PATH,
                            codef.confirm_params(params, codef.two_way_info(result), sms_no))

    if codef.code_of(result) != codef.OK:
        raise RuntimeError(f"조회 실패 [{codef.code_of(result)}] {codef.message_of(result)}")
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
    if codef.env() != "mock" and not os.environ.get("CODEF_PUBLIC_KEY"):
        sys.exit("CODEF_CLIENT_ID / CODEF_CLIENT_SECRET / CODEF_PUBLIC_KEY 환경변수를 설정하세요.")

    telecom = input("통신사 (skt/kt/lgu): ").strip().lower()
    user = {
        "id": input("내보험다보여 아이디: ").strip(),
        "password": getpass("내보험다보여 비밀번호: "),
        "userName": input("이름: ").strip(),
        "birthDate": input("생년월일 (YYYYMMDD): ").strip(),
        "phoneNo": "".join(ch for ch in input("휴대폰 번호: ") if ch.isdigit()),
        "telecom": codef.TELECOM.get(telecom, "0"),
        "authMethod": "0" if input("인증 방식 (pass/sms) [pass]: ").strip().lower() == "sms" else "1",
    }

    data = fetch_contracts(user)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "insurance_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n원본 응답 저장: {out}\n")
    print_summary(data)


if __name__ == "__main__":
    main()
