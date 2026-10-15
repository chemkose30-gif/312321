@echo off
chcp 65001 > nul
cd /d %~dp0
set PYTHONIOENCODING=utf-8
pip install -q numpy pandas MetaTrader5
if not exist state\champion.json (
  echo [1/2] MT5 과거 데이터로 첫 학습 중...
  python trader.py improve --mt5
)
echo [2/2] 실시간 모의매매 시작 - 기록: state\journal.csv / 주간 리포트: reports\
python trader.py run --mt5
pause
