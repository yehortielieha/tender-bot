@echo off
rem Runs the bot with the PRACTICE-SERVER settings (fair price off, trend rules on).
rem For a live room with real traders, and on competition day, use run_bot.bat instead.
cd /d "%~dp0"
title LT3 bot - PRACTICE SERVER
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" lt3_bot.py --practice
pause
