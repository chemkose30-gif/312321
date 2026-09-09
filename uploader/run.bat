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

rem --- ZIP 안에서 바로 실행한 경우 (압축을 풀지 않음) ---
echo "%~dp0" | findstr /i /c:"\Temp\" >nul
if not errorlevel 1 (
  echo  [문제] 압축을 풀지 않고 ZIP 안에서 실행한 것 같습니다.
  echo.
  echo  ZIP 파일에서 마우스 오른쪽 클릭 - "압축 풀기"를 먼저 하고,
  echo  풀린 폴더 안의 run.bat 을 실행하세요.
  echo.
  goto :fail
)

rem --- Python 찾기 (py 런처 - python - python3 순서) ---
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python -c "import sys" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  python3 -c "import sys" >nul 2>&1 && set "PY=python3"
)

if not defined PY (
  echo  [문제] Python 을 찾을 수 없습니다.
  echo.
  echo  1^) 브라우저에서 python.org 설치 페이지를 열어드립니다.
  echo  2^) 설치 파일 첫 화면 맨 아래 "Add python.exe to PATH" 를 반드시 체크하세요.
  echo  3^) 설치가 끝나면 이 창을 닫고 run.bat 을 다시 실행하세요.
  echo.
  echo  ※ Microsoft Store 창이 열렸다면 그건 진짜 Python 이 아닙니다.
  echo     아래 주소에서 직접 받으세요: https://www.python.org/downloads/
  echo.
  start "" "https://www.python.org/downloads/"
  goto :fail
)

echo  Python 확인 완료 ^(%PY%^)
echo.

rem --- 가상환경 ---
if not exist ".venv\Scripts\python.exe" (
  echo  [1/3] 실행 환경을 만드는 중...
  %PY% -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo  [문제] 실행 환경^(.venv^) 을 만들지 못했습니다.
  echo  이 폴더를 바탕화면이나 문서 폴더로 옮긴 뒤 다시 시도해 보세요.
  echo  ^(OneDrive/네트워크 드라이브에서는 실패할 수 있습니다^)
  goto :fail
)

rem --- 패키지 설치 (이미 돼 있으면 건너뜀) ---
".venv\Scripts\python.exe" -c "import uvicorn, fastapi, httpx, cryptography" >nul 2>&1
if errorlevel 1 (
  echo  [2/3] 필요한 프로그램을 설치하는 중... ^(처음 한 번만, 1~2분 걸립니다^)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo  [문제] 설치에 실패했습니다. 인터넷 연결을 확인한 뒤 다시 실행해 보세요.
    goto :fail
  )
)

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

:fail
echo.
echo  --------------------------------------------------
echo  창이 닫히지 않습니다. 위 내용을 확인하세요.
echo  --------------------------------------------------
pause
exit /b 1
