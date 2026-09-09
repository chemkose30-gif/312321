@echo off
rem PURE ASCII. Removes the autostart task and stops the background server.
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title AllOne Uploader - remove autostart

schtasks /end /tn "AllOneUploader" >nul 2>&1
schtasks /delete /tn "AllOneUploader" /f >nul 2>&1
taskkill /f /im wscript.exe /fi "WINDOWTITLE eq service-run*" >nul 2>&1
for /f "tokens=2 delims==; " %%P in ('wmic process where "commandline like '%%uvicorn app.main%%'" get processid /value 2^>nul ^| findstr "="') do taskkill /f /pid %%P >nul 2>&1

echo.
echo  [OK] Autostart removed and server stopped.
if exist "msg\uninstalled.txt" type "msg\uninstalled.txt"
echo.
pause
