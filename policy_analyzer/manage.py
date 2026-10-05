"""설계사 계정 관리

  python manage.py add-planner <아이디> <이름>     # 비밀번호는 입력창에서
  python manage.py set-password <아이디>
  python manage.py list
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
    elif args == ["list"]:
        for p in store.list_planners():
            print(p["id"], p["username"], p["name"])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
