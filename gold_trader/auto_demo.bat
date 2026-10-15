@echo off
chcp 65001 > nul
cd /d %~dp0
set PYTHONIOENCODING=utf-8
pip install -q numpy pandas MetaTrader5
if not exist state\last_improve.txt (
  echo [1/2] MT5 과거 데이터로 첫 학습 중 (1·5·15분봉 비교, 수 분 걸릴 수 있음)
  python trader.py improve --mt5
)
echo [2/2] 자동매매 시작 - 데모 계좌에서만 주문합니다. 멈추려면 이 폴더에 STOP 파일을 만드세요.
python trader.py auto --mt5 --risk 3 --max-positions 5 --leverage 100
pause
