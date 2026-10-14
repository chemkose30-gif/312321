#!/usr/bin/env bash
# 증권 분석 방식 전환.  BACKEND=claude  또는  BACKEND=local  (기본 local)
#   claude 모드는 /root/ins_data/api.key 에 Claude API 키(sk-ant-...)가 있어야 함.
# 현재 켜져 있는 보안 설정(2FA/쿠키/프록시)은 그대로 유지.
set -e
cd "$(dirname "$0")"
BACKEND="${BACKEND:-local}"

getenv() { docker inspect ins-analyzer --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep "^$1=" | head -1 | cut -d= -f2-; }
R2FA="$(getenv REQUIRE_2FA)";  [ -z "$R2FA" ]  && R2FA=1
SECURE="$(getenv SECURE_COOKIE)"; [ -z "$SECURE" ] && SECURE=1
TRUSTP="$(getenv TRUST_PROXY)"; [ -z "$TRUSTP" ] && TRUSTP=1

API_ARGS=()
if [ "$BACKEND" = "claude" ]; then
  if [ ! -s /root/ins_data/api.key ]; then
    echo "오류: /root/ins_data/api.key 에 Claude API 키가 없습니다."
    echo "먼저:  echo \"sk-ant-...키...\" > /root/ins_data/api.key"
    exit 1
  fi
  API_ARGS=(-e "ANTHROPIC_API_KEY=$(tr -d '\r\n' < /root/ins_data/api.key)")
  echo "→ claude 모드 (Claude API, 해외 전송)"
else
  echo "→ local 모드 (서버 내 OCR, 국내 처리)"
fi

echo "[1/2] 이미지 빌드(anthropic 포함)..."
docker build -q -t ins-analyzer . >/dev/null

echo "[2/2] 컨테이너 재실행..."
DATA_KEY="$(cat /root/ins_data/data.key)"
docker rm -f ins-analyzer 2>/dev/null || true
docker run -d --name ins-analyzer --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v /root/ins_data:/data \
  -e DB_PATH=/data/app.db \
  -e DATA_KEY="$DATA_KEY" \
  -e EXTRACT_BACKEND="$BACKEND" \
  -e REQUIRE_2FA="$R2FA" \
  -e SECURE_COOKIE="$SECURE" \
  -e TRUST_PROXY="$TRUSTP" \
  "${API_ARGS[@]}" \
  ins-analyzer
echo
echo "완료! 현재 분석 방식: $BACKEND  (2FA=$R2FA)"
echo "사이트에서 증권을 올려 확인하세요. 바꾸려면 BACKEND=local bash switch_backend.sh"
