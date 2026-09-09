@echo off
rem PURE ASCII. Registers a scheduled task so the uploader starts with Windows
rem and keeps running in the background (no console window).
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
title AllOne Uploader - install autostart

set "TASK=AllOneUploader"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8100"

echo.
echo  ==========================================
echo    AllOne Uploader - 24/7 autostart setup
echo  ==========================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo  [ERROR] Not set up yet.
  call :say needsetup
  goto :fail
)

if not exist "data" mkdir data
> "data\port.txt" echo %PORT%

schtasks /query /tn "%TASK%" >nul 2>&1
if not errorlevel 1 schtasks /delete /tn "%TASK%" /f >nul 2>&1

schtasks /create /f /tn "%TASK%" /sc onlogon /rl highest /tr "wscript.exe \"%~dp0service-run.vbs\""
if errorlevel 1 goto :taskfail

schtasks /run /tn "%TASK%" >nul 2>&1
echo.
echo  [OK] Autostart installed. Port %PORT%.
call :say installed
echo      http://localhost:%PORT%
echo.
pause
exit /b 0

:taskfail
echo  [ERROR] Could not create the scheduled task.
call :say taskfail
goto :fail

:fail
echo.
pause
exit /b 1

:say
if exist "msg\%~1.txt" type "msg\%~1.txt"
echo.
exit /b 0
