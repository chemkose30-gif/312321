#!/usr/bin/env bash
# 새 버전 반영:  cd /root/312321 && git pull && bash teamhub/deploy/update.sh
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> [1/4] DB 백업"
if ! timeout 60 sqlite3 /var/lib/teamhub/teamhub.db ".timeout 10000" \
     ".backup /var/backups/teamhub/teamhub-before-update-$(date +%F-%H%M).db"; then
  echo "    ⚠ 백업 실패 (계속 진행합니다. 매일 자동 백업은 /var/backups/teamhub 에 있습니다)"
fi

echo "==> [2/4] 프로그램 복사"
cp -r "$SRC"/*.py "$SRC/requirements.txt" "$SRC/static" /opt/teamhub/
cp "$SRC/deploy/teamhub.service" /etc/systemd/system/teamhub.service
# 화면 오른쪽 위 메뉴에 보이는 버전 (커밋 날짜·번호)
git -C "$SRC" log -1 --format="%cd · %h" --date=format:"%Y-%m-%d %H:%M" > /opt/teamhub/VERSION 2>/dev/null || true
systemctl daemon-reload

echo "==> [3/4] 필요한 패키지 확인 (처음엔 1~2분 걸릴 수 있어요)"
if ! command -v readpst >/dev/null 2>&1; then   # Outlook 메일함(.pst) 가져오기용
  timeout 300 apt-get install -y -qq pst-utils >/dev/null 2>&1 || \
    { timeout 120 apt-get update -qq >/dev/null 2>&1; timeout 300 apt-get install -y -qq pst-utils >/dev/null 2>&1; } || \
    echo "    ⚠ pst-utils 설치 실패 (Outlook .pst 가져오기만 안 됩니다)"
fi
timeout 300 /opt/teamhub/venv/bin/pip install -q --disable-pip-version-check -r /opt/teamhub/requirements.txt
chown -R teamhub:teamhub /opt/teamhub

echo "==> [4/4] 서버 재시작"
timeout 60 systemctl restart teamhub || true
sleep 2
if systemctl is-active --quiet teamhub; then
  echo "✅ 업데이트 완료 (업데이트 전 DB 는 /var/backups/teamhub 에 백업됨)"
else
  echo "❌ 서버가 시작되지 않았습니다. 아래 로그를 캡처해서 보내주세요."
  journalctl -u teamhub -n 30 --no-pager
  exit 1
fi
