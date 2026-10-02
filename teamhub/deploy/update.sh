#!/usr/bin/env bash
# 새 버전 반영:  cd TeamHub소스/teamhub && git pull && sudo bash deploy/update.sh
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
sqlite3 /var/lib/teamhub/teamhub.db ".backup /var/backups/teamhub/teamhub-before-update-$(date +%F-%H%M).db"
cp -r "$SRC"/*.py "$SRC/requirements.txt" "$SRC/static" /opt/teamhub/
/opt/teamhub/venv/bin/pip install -q -r /opt/teamhub/requirements.txt
chown -R teamhub:teamhub /opt/teamhub
systemctl restart teamhub
echo "✅ 업데이트 완료 (업데이트 전 DB 는 /var/backups/teamhub 에 백업됨)"
