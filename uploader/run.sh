#!/usr/bin/env bash
# 실행: ./run.sh [포트]
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-${PORT:-8100}}"
HOST="${HOST:-127.0.0.1}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python3 를 찾을 수 없습니다. https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행하세요."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "[1/3] 실행 환경을 만드는 중..."
  python3 -m venv .venv
fi
if ! .venv/bin/python -c "import uvicorn, fastapi, httpx, cryptography" >/dev/null 2>&1; then
  echo "[2/3] 필요한 프로그램을 설치하는 중... (처음 한 번만, 1~2분)"
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
fi
[ -f .env ] || { [ -f .env.example ] && cp .env.example .env; } || true

echo
echo "[3/3] 서버를 켰습니다."
echo "   브라우저에서 열기 : http://localhost:$PORT"
echo "   끄기             : Ctrl+C"
echo
command -v open >/dev/null 2>&1 && (sleep 2; open "http://localhost:$PORT") &
exec .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
