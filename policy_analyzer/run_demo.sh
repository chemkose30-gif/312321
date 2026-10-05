#!/usr/bin/env bash
# 로컬 테스트용 실행 (Mac / Linux). 샘플 데이터로 UI만 켜집니다.
# 실제 증권 분석(OCR)과 2단계 인증은 꺼져 있습니다. 운영용이 아닙니다.
set -e
cd "$(dirname "$0")"

echo "[1/4] 파이썬 환경 준비..."
python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q fastapi "uvicorn[standard]" python-multipart python-dotenv cryptography qrcode

echo "[2/4] 암호화 키 준비..."
[ -f demo.key ] || python3 -c "from cryptography.fernet import Fernet;open('demo.key','w').write(Fernet.generate_key().decode())"
export DATA_KEY="$(cat demo.key)"
export EXTRACT_BACKEND=mock ANALYZER_MOCK=1 REQUIRE_2FA=0 SECURE_COOKIE=0 DB_PATH=demo.db

echo "[3/4] 데모 계정 준비..."
python3 manage.py seed-demo

echo
echo "[4/4] 서버 시작!  브라우저에서 아래 주소를 여세요:"
echo "        http://localhost:8000"
echo "        아이디: demo   비밀번호: demo-local-1234"
echo "        (끄려면 이 창에서 Ctrl+C)"
echo
python3 -m uvicorn app:app --host 127.0.0.1 --port 8000
