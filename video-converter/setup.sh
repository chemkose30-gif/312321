#!/usr/bin/env bash
# VPS(Ubuntu/Debian)에서 한 번 실행하면 Docker 설치부터 HTTPS 접속까지 자동으로 설정합니다.
#   sudo bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "▶ Docker 설치 중..."
  curl -fsSL https://get.docker.com | sh
fi

if [ ! -f .env ]; then
  echo "▶ 설정 파일(.env) 생성 중..."
  ip=$(curl -fsS https://api.ipify.org)
  password=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 16)
  concurrent=$(( $(nproc) / 2 )); [ "$concurrent" -lt 1 ] && concurrent=1
  cp .env.example .env
  sed -i "s/^APP_PASSWORD=.*/APP_PASSWORD=${password}/" .env
  sed -i "s/^MAX_CONCURRENT=.*/MAX_CONCURRENT=${concurrent}/" .env
  sed -i "s/^DOMAIN=.*/DOMAIN=${ip//./-}.sslip.io/" .env
fi

# 서버 방화벽(ufw)이 켜져 있으면 웹 포트 열기
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
fi

echo "▶ 빌드 및 실행 중... (처음엔 몇 분 걸려요)"
docker compose up -d --build

domain=$(grep '^DOMAIN=' .env | cut -d= -f2)
password=$(grep '^APP_PASSWORD=' .env | cut -d= -f2)
echo
echo "✅ 완료!"
echo "   접속 주소 : https://${domain}"
echo "   비밀번호  : ${password}   (사용자 이름은 아무거나)"
echo "   ※ 처음 접속 시 HTTPS 인증서 발급에 1분 정도 걸릴 수 있어요."
echo "   ※ 비밀번호 변경: .env 수정 후 'docker compose up -d'"
