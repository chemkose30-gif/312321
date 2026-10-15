@echo off
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

rem ==== Gold symbol: auto = find it automatically, or type the exact Market Watch name (e.g. XAUUSD) ====
set SYMBOL=auto

rem ==== Find installed Python: py launcher 3.12 first, then python, then default install folders ====
set "PY="
call :try "py -3.12"
call :try "py -3.13"
call :try "py -3.11"
call :try "py"
call :try "python"
for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do if exist "%%~D\python.exe" call :setpy "%%~D\python.exe"
for /d %%D in ("%ProgramFiles%\Python3*") do if exist "%%~D\python.exe" call :setpy "%%~D\python.exe"
if not defined PY goto nopython

echo Python:
%PY% --version
%PY% trader.py loosen --mt5 --symbol %SYMBOL%
echo.
pause
exit /b 0

:try
if defined PY goto :eof
%~1 --version > nul 2>&1
if errorlevel 1 goto :eof
set "PY=%~1"
goto :eof

:setpy
if defined PY goto :eof
set PY="%~1"
goto :eof

:nopython
echo.
echo [ERROR] Python not found.
echo Install Python 3.12 from python.org and tick "Add python.exe to PATH" on the first screen.
pause
exit /b 1

