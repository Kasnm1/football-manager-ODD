@echo off
cd /d "%~dp0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do set "FM_ODDS_WEB_PID=%%p"
if defined FM_ODDS_WEB_PID goto open_browser
start "FM Odds 2.0" /b pythonw fm_odds_web.py --no-browser
timeout /t 2 /nobreak >nul
:open_browser
start "" http://127.0.0.1:8765
