@echo off
rem Shows what the bot sees through the RIT API. Places no orders.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" lt3_bot.py --check
pause
