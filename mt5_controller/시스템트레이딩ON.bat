@echo off
chcp 65001 > nul
cd /d "%~dp0"
python mt5_control.py on
pause
