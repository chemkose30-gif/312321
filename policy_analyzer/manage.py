"""설계사 계정 관리

  python manage.py add-planner <아이디> <이름>     # 비밀번호는 입력창에서
  python manage.py set-password <아이디>
  python manage.py list
  python manage.py reset-2fa <아이디>               # 휴대폰 분실 시 2단계 인증 초기화
  python manage.py audit [건수]                      # 전체 접속·작업 기록 보기
  python manage.py seed-demo                         # (로컬 테스트용) 계정이 없으면 demo 계정 생성
"""

import sys
from getpass import getpass

from dotenv import load_dotenv

load_dotenv()
import store  # noqa: E402


def ask_pw() -> str:
    pw = getpass("비밀번호 (10자 이상): ")
    if len(pw) < 10:
        sys.exit("비밀번호는 10자 이상이어야 합니다.")
    if pw != getpass("비밀번호 확인: "):
        sys.exit("비밀번호가 일치하지 않습니다.")
    return pw


def main() -> None:
    store.init_db()
    args = sys.argv[1:]
    if len(args) == 3 and args[0] == "add-planner":
        store.add_planner(args[1], args[2], ask_pw())
        print(f"설계사 계정 생성: {args[1]}")
    elif len(args) == 2 and args[0] == "set-password":
        print("변경 완료" if store.set_password(args[1], ask_pw()) else "해당 아이디가 없습니다.")
    elif len(args) == 2 and args[0] == "reset-2fa":
        print("초기화 완료. 다음 로그인 때 다시 설정합니다." if store.reset_totp(args[1]) else "해당 아이디가 없습니다.")
    elif args and args[0] == "audit":
        from datetime import datetime
        for r in reversed(store.list_audit(None, int(args[1]) if len(args) > 1 else 100)):
            print(datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S"), r["username"] or "-", r["ip"], r["action"], r["target"] or "")
    elif args == ["seed-demo"]:
        # 로컬 테스트 전용: 계정이 하나도 없을 때만 demo 계정을 만든다.
        if store.list_planners():
            print("이미 계정이 있어 건너뜁니다.")
        else:
            store.add_planner("demo", "데모 설계사", "demo-local-1234")
            print("데모 계정 생성 → 아이디: demo / 비밀번호: demo-local-1234")
    elif args == ["list"]:
        for p in store.list_planners():
            print(p["id"], p["username"], p["name"])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
