@echo off
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

rem ==== Gold symbol name exactly as shown in MT5 Market Watch (XAUUSD, XAUUSD.GOLD, GOLD ...) ====
set SYMBOL=XAUUSD.GOLD

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
echo Installing packages (first time takes 1-2 minutes)...
%PY% -m pip install -q --disable-pip-version-check numpy pandas MetaTrader5
%PY% -c "import MetaTrader5, numpy, pandas" > nul 2>&1
if errorlevel 1 goto nopackages

if exist state\last_improve.txt goto start
echo [1/2] First learning from MT5 history, this can take a few minutes...
%PY% trader.py improve --mt5 --symbol %SYMBOL%
if errorlevel 1 goto failimprove

:start
echo [2/2] AUTO TRADING (demo account only). Close this window to stop.
%PY% trader.py auto --mt5 --symbol %SYMBOL% --risk 3 --max-positions 5 --leverage 100
echo.
echo Program stopped. See the message above.
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

:nopackages
echo.
echo [ERROR] Package install failed. MetaTrader5 package may not support this Python version.
echo Install Python 3.12 from python.org and run again.
pause
exit /b 1

:failimprove
echo.
echo [ERROR] First learning failed. Check the message above:
echo  - Is MT5 running and logged in?
echo  - Is SYMBOL correct? Edit this file and change the SYMBOL line.
pause
exit /b 1
