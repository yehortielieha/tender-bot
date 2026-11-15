@echo off
rem Runs the bot live. Ctrl+C stops it and cancels its open orders (positions stay open).
cd /d "%~dp0"
title LT3 bot - LIVE
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" lt3_bot.py
pause
