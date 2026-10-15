@echo off
chcp 65001 > nul
cd /d %~dp0
set PYTHONIOENCODING=utf-8

rem ===== MT5 시장 감시 창에 보이는 금 심볼 이름 그대로 (예: XAUUSD, XAUUSD.GOLD, GOLD) =====
set SYMBOL=XAUUSD.GOLD

rem ===== 설치된 파이썬 찾기: py 런처(3.12 우선) → python → 기본 설치 폴더 =====
set "PY="
for %%V in (3.12 3.13 3.11 3.10) do (
  if not defined PY (py -%%V --version > nul 2>&1 && set "PY=py -%%V")
)
if not defined PY (py --version > nul 2>&1 && set "PY=py")
if not defined PY (python --version > nul 2>&1 && set "PY=python")
if not defined PY (
  for /d %%D in ("%LocalAppData%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
    if exist "%%~D\python.exe" set "PY="%%~D\python.exe""
  )
)
if not defined PY (
  echo [오류] 파이썬을 찾지 못했습니다. python.org 에서 Python 3.12 를 설치하세요.
  echo        설치 첫 화면에서 "Add python.exe to PATH" 를 체크하면 됩니다.
  pause
  exit /b 1
)
echo 사용하는 파이썬:
%PY% --version

echo 필요한 패키지 설치 중... (처음에는 1~2분 걸립니다)
%PY% -m pip install -q --disable-pip-version-check numpy pandas MetaTrader5
%PY% -c "import MetaTrader5, numpy, pandas" > nul 2>&1
if errorlevel 1 (
  echo [오류] 패키지 설치 실패. 파이썬 버전이 너무 최신이면 MetaTrader5 패키지가 설치되지 않습니다.
  echo        python.org 에서 Python 3.12 를 설치한 뒤 다시 실행하세요.
  pause
  exit /b 1
)
if not exist state\last_improve.txt (
  echo [1/2] MT5 과거 데이터로 첫 학습 중 (1·5·15분봉 비교, 수 분 걸릴 수 있음)
  %PY% trader.py improve --mt5 --symbol %SYMBOL%
  if errorlevel 1 (
    echo [오류] 첫 학습 실패. 위 메시지를 확인하세요 (MT5 실행·로그인, 심볼 이름).
    pause
    exit /b 1
  )
)
echo [2/2] 자동매매 시작 - 데모 계좌에서만 주문합니다. 멈추려면 이 창을 닫거나 폴더에 STOP 파일을 만드세요.
%PY% trader.py auto --mt5 --symbol %SYMBOL% --risk 3 --max-positions 5 --leverage 100
pause
