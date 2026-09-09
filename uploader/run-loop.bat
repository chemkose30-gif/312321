@echo off
rem PURE ASCII. Supervisor loop: keeps the server running 24/7 and
rem restarts it a few seconds after any crash. Started by service-run.vbs.
cd /d "%~dp0"
if not exist "data" mkdir data

set "PORT=8100"
if exist "data\port.txt" set /p PORT=<data\port.txt

:loop
echo [%DATE% %TIME%] starting server on port %PORT% >> "data\server.log"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% >> "data\server.log" 2>&1
echo [%DATE% %TIME%] server stopped, restarting in 5s >> "data\server.log"
timeout /t 5 /nobreak >nul
goto loop
