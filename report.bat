@echo off
rem Shows how each heat and each tender did, from the bot's logs. Safe to run while the bot trades.
rem   report.bat            the most recent day
rem   report.bat 20261110   one day
rem   report.bat all        everything
cd /d "%~dp0"
title LT3 bot - report
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" report.py %*
echo.
pause
