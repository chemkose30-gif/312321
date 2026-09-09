@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8100"

if not exist ".venv" (
  echo [1/3] 가상환경을 만드는 중...
  python -m venv .venv
  if errorlevel 1 (
    echo.
    echo Python을 찾을 수 없습니다. https://www.python.org/downloads/ 에서 설치할 때
    echo "Add python.exe to PATH" 를 체크한 뒤 이 파일을 다시 실행하세요.
    pause
    exit /b 1
  )
  echo [2/3] 필요한 패키지를 설치하는 중... ^(처음 한 번만, 1~2분^)
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
)

if not exist ".env" copy ".env.example" ".env" >nul

echo [3/3] 서버 시작 — 브라우저에서 http://localhost:%PORT% 를 여세요.
echo 종료하려면 이 창에서 Ctrl+C 를 누르거나 창을 닫으세요.
start "" "http://localhost:%PORT%"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port %PORT%
pause
