#!/usr/bin/env bash
# Vultr(또는 다른 우분투 서버)에서 실행하는 설치 스크립트.
# 코드가 이미 이 폴더에 있다고 가정합니다(아래 "코드 올리는 법" 참고).
# 하는 일: Docker 설치 → 이미지 빌드 → 데이터 보관 볼륨과 함께 컨테이너 실행(자동 재시작).
set -e

# ── 설정(필요시 수정) ─────────────────────────────────────────────
BACKEND="${BACKEND:-local}"      # local(서버 내 OCR, 데이터 국내) / mock(샘플) / claude
PORT="${PORT:-8000}"
# ──────────────────────────────────────────────────────────────────

echo "[1/5] Docker 설치 확인..."
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

echo "[2/5] 암호화 키 준비(최초 1회 생성, /root/ins_data/data.key 에 보관)..."
mkdir -p /root/ins_data
if [ ! -f /root/ins_data/data.key ]; then
  docker run --rm python:3.12-slim python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())" 2>/dev/null \
    | tr -d '\r\n' > /root/ins_data/data.key
  echo "    키 생성됨. 이 파일을 반드시 따로 백업하세요: /root/ins_data/data.key"
fi
DATA_KEY="$(cat /root/ins_data/data.key)"

echo "[3/5] 이미지 빌드..."
docker build -t ins-analyzer .

echo "[4/5] 기존 컨테이너 정리 후 실행..."
docker rm -f ins-analyzer 2>/dev/null || true
docker run -d --name ins-analyzer --restart unless-stopped \
  -p "${PORT}:8000" \
  -v /root/ins_data:/data \
  -e DB_PATH=/data/app.db \
  -e DATA_KEY="${DATA_KEY}" \
  -e EXTRACT_BACKEND="${BACKEND}" \
  -e REQUIRE_2FA=1 \
  -e SECURE_COOKIE="${SECURE_COOKIE:-0}" \
  -e TRUST_PROXY="${TRUST_PROXY:-0}" \
  ins-analyzer

echo "[5/5] 첫 설계사 계정 만들기(아이디/이름은 원하는 값으로):"
echo "    docker exec -it ins-analyzer python manage.py add-planner myid \"내이름\""
echo
echo "완료! 접속 주소:  http://<서버IP>:${PORT}"
echo " - 로그: docker logs -f ins-analyzer"
echo " - 재시작: docker restart ins-analyzer"
echo
echo "⚠ 지금은 http(암호화 안 됨)입니다. 실제 고객정보를 쓰려면 도메인+HTTPS가 필요합니다(안내 참고)."
