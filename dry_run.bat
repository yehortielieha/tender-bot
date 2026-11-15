@echo off
rem Prints the bot's tender decisions without accepting, declining or trading anything.
cd /d "%~dp0"
title LT3 bot - DRY RUN
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" lt3_bot.py --dry-run
pause
