@echo off
chcp 65001 > nul
cd /d %~dp0
set PYTHONIOENCODING=utf-8

rem ===== MT5 시장 감시 창에 보이는 금 심볼 이름 그대로 (예: XAUUSD, XAUUSD.GOLD, GOLD) =====
set SYMBOL=XAUUSD.GOLD

python --version > nul 2>&1
if errorlevel 1 (
  echo [오류] 파이썬이 설치되지 않았거나 PATH에 없습니다. python.org 에서 설치할 때 "Add python.exe to PATH"를 체크하세요.
  pause
  exit /b 1
)
echo 필요한 패키지 설치 중...
python -m pip install -q numpy pandas MetaTrader5
if not exist state\last_improve.txt (
  echo [1/2] MT5 과거 데이터로 첫 학습 중 (1·5·15분봉 비교, 수 분 걸릴 수 있음)
  python trader.py improve --mt5 --symbol %SYMBOL%
  if errorlevel 1 (
    echo [오류] 첫 학습 실패. 위 메시지를 확인하세요 (MT5 실행·로그인, 심볼 이름).
    pause
    exit /b 1
  )
)
echo [2/2] 실시간 모의매매 시작 - 주문은 넣지 않고 기록만 합니다.
python trader.py run --mt5 --symbol %SYMBOL% --risk 3 --max-positions 5 --leverage 100
pause
