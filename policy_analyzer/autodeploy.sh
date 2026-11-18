#!/usr/bin/env bash
# 자동 배포: GitHub에 새 코드가 올라오면 알아서 받아서 다시 띄운다.
#   - 2~3분마다 실행(cron)되도록 아래 "설치" 명령으로 등록한다.
#   - 새 커밋이 없으면 아무것도 안 하고 끝난다(서버 부담 없음).
#   - 현재 돌아가는 분석 방식(claude/local)과 2FA 등 보안설정은 switch_backend.sh가 그대로 유지한다.
#
# [최초 설치 — 서버에서 딱 한 번만]
#   cd ~/312321/policy_analyzer && chmod +x autodeploy.sh && \
#   ( crontab -l 2>/dev/null | grep -v autodeploy.sh ; \
#     echo "*/2 * * * * /bin/bash $PWD/autodeploy.sh >> $PWD/autodeploy.log 2>&1" ) | crontab -
#
# [끄기]   crontab -l | grep -v autodeploy.sh | crontab -
# [기록보기] tail -f ~/312321/policy_analyzer/autodeploy.log

set -e
cd "$(dirname "$0")"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git fetch --quiet origin "$BRANCH" || { echo "[$(date '+%F %T')] git fetch 실패"; exit 0; }

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0   # 새 코드 없음 — 조용히 끝
fi

echo "[$(date '+%F %T')] 새 코드 발견: ${LOCAL:0:7} → ${REMOTE:0:7}  ($BRANCH)"
git reset --hard "origin/$BRANCH"
echo "[$(date '+%F %T')] 재배포 시작..."
bash switch_backend.sh
echo "[$(date '+%F %T')] 재배포 완료"
