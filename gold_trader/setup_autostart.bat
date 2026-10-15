@echo off
cd /d "%~dp0"
echo ============================================================
echo  Gold AI Trader - auto start setup
echo ============================================================
echo.
echo 1) Run auto_demo.bat automatically 1 minute after you log in to Windows
schtasks /Create /TN "GoldTraderAI" /TR "\"%~dp0auto_demo.bat\"" /SC ONLOGON /DELAY 0001:00 /F > nul
if errorlevel 1 (
  echo    FAILED. Right-click this file and choose "Run as administrator", then try again.
) else (
  echo    OK
)
echo.
echo 2) Never sleep / hibernate while plugged in
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
echo    OK
echo.
echo Done. MT5 also needs to start: the program opens MT5 by itself when it connects.
echo Keep "Save password" ticked in MT5 login so it logs in automatically.
echo To undo, run remove_autostart.bat
echo.
pause
