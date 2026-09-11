@echo off
REM =====================================================================
REM  LeadHunter Pro - restart ALL servers (SearXNG + Backend + Frontend)
REM  For restarting a single server use:
REM    restart_searxng.bat / restart_backend.bat / restart_frontend.bat
REM =====================================================================

echo Stopping all three servers ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8080 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5173 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
timeout /t 1 /nobreak >nul

echo Starting SearXNG (8080) ...
start "SearXNG :8080" /D C:\Users\SHAKIR\searxng-master cmd /k "set SEARXNG_SETTINGS_PATH=C:\Users\SHAKIR\searxng-master\searx\settings.yml && C:\Users\SHAKIR\searxng-venv\Scripts\python.exe -m searx.webapp"

echo Starting Backend (8000) ...
start "Backend :8000" /D C:\Users\SHAKIR\Desktop\LeadHunterPro\backend cmd /k "C:\Users\SHAKIR\Desktop\LeadHunterPro\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

echo Starting Frontend (5173) ...
start "Frontend :5173" /D C:\Users\SHAKIR\Desktop\LeadHunterPro\frontend cmd /k "npm run dev"

echo.
echo All three servers are restarting in separate windows.
pause