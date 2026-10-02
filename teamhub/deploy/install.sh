#!/usr/bin/env bash
# TeamHub 서버 설치 스크립트 (Ubuntu 24.04, root 로 실행)
#   사용법:  sudo bash install.sh teamhub.우리회사.com   (도메인 + HTTPS, 권장)
#            sudo bash install.sh ip                     (도메인 없이 IP 로 임시 운영, HTTP)
# - /opt/teamhub 에 설치, 부팅 시 자동 시작, 죽으면 자동 재시작 (systemd)
# - Caddy 로 HTTPS 인증서 자동 발급/갱신
# - 매일 새벽 DB 백업 (/var/backups/teamhub, 30일 보관)
set -euo pipefail

DOMAIN="${1:-}"
if [[ -z "$DOMAIN" ]]; then
  echo "사용법: sudo bash install.sh <도메인>   예) sudo bash install.sh teamhub.mycompany.com"
  echo "        도메인이 아직 없으면:  sudo bash install.sh ip"
  exit 1
fi
SRC="$(cd "$(dirname "$0")/.." && pwd)"
IP="$(curl -s -4 --max-time 10 https://ifconfig.me || true)"
if [[ "$DOMAIN" == "ip" ]]; then
  SITE=":80"; URL="http://$IP"
else
  SITE="$DOMAIN"; URL="https://$DOMAIN"
fi

echo "==> 서버 시간대: 한국"
timedatectl set-timezone Asia/Seoul

echo "==> 패키지 설치"
apt-get update -y
apt-get install -y python3 python3-venv sqlite3 caddy ufw

echo "==> 프로그램 복사 (/opt/teamhub)"
id teamhub >/dev/null 2>&1 || useradd --system --home /opt/teamhub --shell /usr/sbin/nologin teamhub
mkdir -p /opt/teamhub /var/lib/teamhub /var/backups/teamhub
cp -r "$SRC/app.py" "$SRC/mailer.py" "$SRC/requirements.txt" "$SRC/static" /opt/teamhub/
python3 -m venv /opt/teamhub/venv
/opt/teamhub/venv/bin/pip install -q --upgrade pip
/opt/teamhub/venv/bin/pip install -q -r /opt/teamhub/requirements.txt
chown -R teamhub:teamhub /opt/teamhub /var/lib/teamhub

echo "==> 설정 파일 (/etc/teamhub.env)"
if [[ ! -f /etc/teamhub.env ]]; then
  sed "s#^TEAMHUB_BASE_URL=.*#TEAMHUB_BASE_URL=$URL#" "$SRC/deploy/teamhub.env.example" > /etc/teamhub.env
else
  sed -i "s#^TEAMHUB_BASE_URL=.*#TEAMHUB_BASE_URL=$URL#" /etc/teamhub.env
fi
chown root:teamhub /etc/teamhub.env
chmod 640 /etc/teamhub.env

echo "==> 자동 실행 서비스 등록"
cp "$SRC/deploy/teamhub.service" /etc/systemd/system/teamhub.service
systemctl daemon-reload
systemctl enable --now teamhub
systemctl restart teamhub

echo "==> 웹 서버 (Caddy)"
cat > /etc/caddy/Caddyfile <<CADDY
$SITE {
    encode gzip
    reverse_proxy 127.0.0.1:8100
}
CADDY
systemctl enable --now caddy
systemctl reload caddy

echo "==> 방화벽 (SSH, HTTP, HTTPS 만 허용)"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "==> 매일 백업"
cat > /etc/cron.daily/teamhub-backup <<'BACKUP'
#!/bin/sh
sqlite3 /var/lib/teamhub/teamhub.db ".backup /var/backups/teamhub/teamhub-$(date +%F).db"
find /var/backups/teamhub -name 'teamhub-*.db' -mtime +30 -delete
BACKUP
chmod +x /etc/cron.daily/teamhub-backup

cat <<DONE

✅ 설치 완료
  - 접속 주소 : $URL
  - 서버 공인 IP: ${IP:-확인 실패}   ← 이카운트 API 허용 IP 로 등록
  - 최초 로그인: admin / admin1234  → 로그인 후 즉시 비밀번호 변경!
  - 메일/이카운트 설정: sudo nano /etc/teamhub.env  → 저장 후 sudo systemctl restart teamhub
  - 상태 확인  : sudo systemctl status teamhub   /  로그: sudo journalctl -u teamhub -f
DONE
if [[ "$DOMAIN" == "ip" ]]; then
  echo "  ⚠ 지금은 암호화되지 않은 HTTP 입니다. 도메인을 연결한 뒤 'sudo bash install.sh 도메인' 을 다시 실행하면 HTTPS 로 바뀝니다."
fi
