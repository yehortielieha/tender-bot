@echo off
rem One-time setup on a new laptop: creates .venv in this folder and installs what the bot needs.
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python 3 is not installed. Get it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" in the installer, then run this again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo Installing the requirements failed.
  pause
  exit /b 1
)
echo.
echo Setup done. Next: log in to the RIT Client, then run run_bot.bat (it asks for your API key).
pause
