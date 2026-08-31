@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment in .venv...
  python -m venv .venv
  if errorlevel 1 goto :fail
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 goto :fail

echo Installing or updating requirements...
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail

set HOST=0.0.0.0
set PORT=5000

echo.
echo Lunchbreak ELO is starting in LAN mode on http://0.0.0.0:%PORT%
echo Open the in-app Network page after login to see the LAN or VPN URL to share.
echo.

python app.py
goto :eof

:fail
echo.
echo Start failed. Check Python installation and try again.
pause
