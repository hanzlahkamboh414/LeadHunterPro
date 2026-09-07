@echo off
REM =====================================================================
REM  LeadHunter Pro - START all servers (SearXNG + Backend + Frontend)
REM
REM  Each server opens in its OWN titled window showing live logs.
REM  Close a window to stop just that server.
REM
REM    SearXNG  -> http://127.0.0.1:8080   free search lane (tried first)
REM    Backend  -> http://127.0.0.1:8000   FastAPI /api/v1/leads
REM    Frontend -> http://127.0.0.1:5173   Vite dev (proxies /api to 8000)
REM
REM  Stop everything with:  stop_servers.bat
REM =====================================================================

echo Starting SearXNG (8080) ...
start "SearXNG :8080" /D C:\Users\SHAKIR\searxng-master cmd /k "set SEARXNG_SETTINGS_PATH=C:\Users\SHAKIR\searxng-master\searx\settings.yml && C:\Users\SHAKIR\searxng-venv\Scripts\python.exe -m searx.webapp"

echo Starting Backend (8000) ...
start "Backend :8000" /D C:\Users\SHAKIR\Desktop\LeadHunterPro\backend cmd /k "C:\Users\SHAKIR\Desktop\LeadHunterPro\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

echo Starting Frontend (5173) ...
start "Frontend :5173" /D C:\Users\SHAKIR\Desktop\LeadHunterPro\frontend cmd /k "npm run dev"

echo.
echo All three servers are launching in separate windows.
echo Open http://127.0.0.1:5173 in the browser.
pause