@echo off
rem =====================================================================
rem  This file must stay PURE ASCII.
rem  cmd.exe parses batch files using the system ANSI codepage (949 on
rem  Korean Windows). Non-ASCII bytes here break the parser and the
rem  garbled fragments get executed as commands. Korean text is kept in
rem  msg\*.txt (UTF-8) and printed with "type" after chcp 65001.
rem =====================================================================
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
title AllOne Uploader

echo.
echo  ==============================
echo    AllOne Uploader
echo  ==============================
echo.

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8100"

rem --- running from inside the zip (not extracted) ---
echo "%~dp0" | findstr /i /c:"\Temp\" >nul
if not errorlevel 1 goto :notextracted

rem --- find python ---
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if defined PY goto :pyfound
python -c "import sys" >nul 2>&1 && set "PY=python"
if defined PY goto :pyfound
python3 -c "import sys" >nul 2>&1 && set "PY=python3"
if defined PY goto :pyfound

rem --- try automatic install with winget (Windows 10/11) ---
where winget >nul 2>&1
if errorlevel 1 goto :nopython
call :say installing
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
echo  [OK] Python found.
echo.

if not exist ".venv\Scripts\python.exe" (
  echo  [1/3] Preparing environment...
  %PY% -m venv .venv
)
if not exist ".venv\Scripts\python.exe" goto :novenv

".venv\Scripts\python.exe" -c "import uvicorn, fastapi, httpx, cryptography" >nul 2>&1
if not errorlevel 1 goto :ready
echo  [2/3] Installing packages... (first run only, 1-2 min)
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :pipfail

:ready
if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul
echo.
echo  [3/3] Server started.
echo.
echo      http://localhost:%PORT%
echo.
call :say started
start "" "http://localhost:%PORT%"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
echo.
echo  Server stopped.
pause
exit /b 0

:notextracted
echo  [ERROR] Running from inside the ZIP file.
call :say notextracted
goto :fail

:nopython
echo  [ERROR] Python not found.
call :say nopython
start "" "https://www.python.org/downloads/"
goto :fail

:novenv
echo  [ERROR] Could not create the .venv environment.
call :say novenv
goto :fail

:pipfail
echo  [ERROR] Package installation failed.
call :say pipfail
goto :fail

:fail
echo.
echo  --------------------------------------------------
pause
exit /b 1

rem --- print a Korean message file if present ---
:say
if exist "msg\%~1.txt" type "msg\%~1.txt"
echo.
exit /b 0
