#!/usr/bin/env bash
# 개발 서버 실행: ./run.sh [포트]
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-${PORT:-8100}}"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
[ -f .env ] || cp .env.example .env
echo "브라우저에서 http://localhost:$PORT 를 여세요. (종료: Ctrl+C)"
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
