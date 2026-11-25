@echo off
REM Windows: runs the paper trader locally and prints the phone URL.
cd /d "%~dp0papertrader"
if not exist .venv python -m venv .venv
call .venv\Scripts\activate
pip install -q -r requirements.txt
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do set IP=%%a
echo.
echo Open on your phone (same Wi-Fi):  http://%IP: =%:8000
echo.
uvicorn app:app --host 0.0.0.0 --port 8000
