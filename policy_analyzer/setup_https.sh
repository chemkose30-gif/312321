#!/usr/bin/env bash
# 도메인 없이 HTTPS + 방화벽 설정.
#  - 서버 IP로 만든 주소(<IP>.sslip.io)에 Let's Encrypt 진짜 인증서를 자동 발급(Caddy)
#  - 앱은 외부에 직접 노출하지 않고(127.0.0.1) Caddy 뒤에서만 동작
#  - 방화벽으로 22(SSH)/80/443 만 허용
set -e
cd "$(dirname "$0")"

IP="$(curl -4 -s https://api.ipify.org || true)"
[ -z "$IP" ] && IP="$(hostname -I | awk '{print $1}')"
HOST="${IP//./-}.sslip.io"
echo "=== 사용할 주소: https://$HOST ==="

echo "[1/4] 방화벽 설정(22/80/443만 허용)..."
apt-get install -y -q ufw >/dev/null
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable

echo "[2/4] 앱을 내부 전용(127.0.0.1)으로, 보안옵션 켜서 재실행..."
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

echo "[3/4] Caddy(자동 HTTPS) 설치..."
if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https curl gnupg >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q >/dev/null
  apt-get install -y -q caddy >/dev/null
fi

echo "[4/4] Caddy 설정 및 인증서 발급..."
cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
    reverse_proxy 127.0.0.1:8000
    encode gzip
}
CADDY
systemctl restart caddy
sleep 3

echo
echo "================================================================"
echo " 완료! 이제 아래 주소로 접속하세요 (자물쇠 표시, 외부에서도 OK):"
echo "     https://$HOST"
echo
echo " 인증서 발급에 10~30초 걸릴 수 있어요. 처음엔 안 되면 1분 뒤 새로고침."
echo " 로그: journalctl -u caddy -n 30 --no-pager"
echo "================================================================"
