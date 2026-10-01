@echo off
chcp 65001 > nul
cd /d "%~dp0"
title MT5 텔레그램 봇
python telegram_bot.py
pause
