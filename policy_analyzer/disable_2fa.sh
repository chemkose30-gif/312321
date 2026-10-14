#!/usr/bin/env bash
# 2단계 인증을 완전히 끈다: 앱 스위치(REQUIRE_2FA=0) + 모든 계정의 등록 삭제.
# 테스트용. 실제 고객정보 넣기 전에는 enable_2fa.sh 로 다시 켤 것.
set -e
cd "$(dirname "$0")"
[ -s /root/ins_data/data.key ] || { echo "키 파일이 없습니다"; exit 1; }
DATA_KEY="$(cat /root/ins_data/data.key)"

docker rm -f ins-analyzer 2>/dev/null || true
docker run -d --name ins-analyzer --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v /root/ins_data:/data \
  -e DB_PATH=/data/app.db \
  -e DATA_KEY="$DATA_KEY" \
  -e EXTRACT_BACKEND="${BACKEND:-local}" \
  -e REQUIRE_2FA=0 \
  -e SECURE_COOKIE=1 \
  -e TRUST_PROXY=1 \
  ins-analyzer
sleep 4

echo "모든 계정의 2단계 인증 등록 삭제..."
for u in $(docker exec ins-analyzer python manage.py list | awk '{print $2}'); do
  docker exec ins-analyzer python manage.py reset-2fa "$u" >/dev/null 2>&1 && echo "  - $u 완료" || true
done
echo
echo "완료! 이제 아이디+비밀번호만으로 로그인됩니다. 사이트를 새로고침하세요."
