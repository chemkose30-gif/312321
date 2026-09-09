@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
title 올원업로드

echo.
echo  ==============================
echo    올원업로드 시작
echo  ==============================
echo.

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8100"

rem ── 압축을 풀지 않고 ZIP 안에서 실행한 경우 ──────────────
echo "%~dp0" | findstr /i /c:"\Temp\" >nul
if not errorlevel 1 goto :notextracted

rem ── Python 찾기 ─────────────────────────────────────────
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if defined PY goto :pyfound
python -c "import sys" >nul 2>&1 && set "PY=python"
if defined PY goto :pyfound
python3 -c "import sys" >nul 2>&1 && set "PY=python3"
if defined PY goto :pyfound

rem ── 없으면 winget 으로 자동 설치 (윈도우 10/11) ──────────
where winget >nul 2>&1
if errorlevel 1 goto :nopython
echo  Python 이 없어서 자동으로 설치합니다.
echo  ^(설치 창이 뜨면 "예"를 눌러주세요. 3~5분 걸릴 수 있습니다^)
echo.
winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
echo.
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto :pyfound
if exist "%ProgramFiles%\Python312\python.exe" set PY="%ProgramFiles%\Python312\python.exe"
if defined PY goto :pyfound
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if defined PY goto :pyfound
goto :nopython

:pyfound
echo  Python 확인 완료.
echo.

rem ── 실행 환경 만들기 ────────────────────────────────────
if not exist ".venv\Scripts\python.exe" (
  echo  [1/3] 실행 환경을 만드는 중...
  %PY% -m venv .venv
)
if not exist ".venv\Scripts\python.exe" goto :novenv

rem ── 필요한 프로그램 설치 ────────────────────────────────
".venv\Scripts\python.exe" -c "import uvicorn, fastapi, httpx, cryptography" >nul 2>&1
if not errorlevel 1 goto :ready
echo  [2/3] 필요한 프로그램을 설치하는 중... ^(처음 한 번만, 1~2분^)
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :pipfail

:ready
if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul
echo.
echo  [3/3] 서버를 켰습니다.
echo.
echo     브라우저에서 열기 :  http://localhost:%PORT%
echo     끄기              :  이 창을 닫으세요
echo.
start "" "http://localhost:%PORT%"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
echo.
echo  서버가 종료되었습니다.
pause
exit /b 0

:notextracted
echo  [문제] 압축을 풀지 않고 ZIP 안에서 실행한 것 같습니다.
echo.
echo  ZIP 파일에 마우스 오른쪽 클릭 - [압축 풀기] 를 먼저 하고,
echo  풀린 폴더 안의 run.bat 을 실행하세요.
goto :fail

:nopython
echo  [문제] Python 을 찾을 수 없습니다.
echo.
echo  방법 1 : 이 창을 닫고 run.bat 을 한 번 더 실행해 보세요.
echo           방금 설치가 끝났다면 그때는 인식됩니다.
echo.
echo  방법 2 : python.org 설치 페이지를 열어드립니다.
echo           - 설치 첫 화면 맨 아래 "Add python.exe to PATH" 를 반드시 체크
echo           - 설치 후 이 창을 닫고 run.bat 을 다시 실행
echo.
echo  ※ Microsoft Store 창이 열렸다면 그건 진짜 Python 이 아닙니다.
start "" "https://www.python.org/downloads/"
goto :fail

:novenv
echo.
echo  [문제] 실행 환경^(.venv^) 을 만들지 못했습니다.
echo  이 폴더를 바탕화면이나 문서 폴더로 옮긴 뒤 다시 실행해 보세요.
echo  ^(OneDrive - 네트워크 드라이브에서는 실패할 수 있습니다^)
goto :fail

:pipfail
echo.
echo  [문제] 필요한 프로그램 설치에 실패했습니다.
echo  인터넷 연결을 확인한 뒤 다시 실행해 보세요.
echo  회사 네트워크라면 방화벽 때문일 수 있습니다.
goto :fail

:fail
echo.
echo  --------------------------------------------------
echo   위 내용을 확인하세요. ^(이 창은 자동으로 닫히지 않습니다^)
echo  --------------------------------------------------
pause
exit /b 1
