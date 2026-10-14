#!/usr/bin/env bash
# 2단계 인증을 다시 켠다(실제 운영 시). 다음 로그인 때 각자 QR 재등록.
set -e
cd "$(dirname "$0")"
DATA_KEY="$(cat /root/ins_data/data.key)"
docker rm -f ins-analyzer 2>/dev/null || true
docker run -d --name ins-analyzer --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v /root/ins_data:/data \
  -e DB_PATH=/data/app.db \
  -e DATA_KEY="$DATA_KEY" \
  -e EXTRACT_BACKEND="${BACKEND:-local}" \
  -e REQUIRE_2FA=1 \
  -e SECURE_COOKIE=1 \
  -e TRUST_PROXY=1 \
  ins-analyzer
echo "완료! 2단계 인증이 다시 켜졌습니다."
