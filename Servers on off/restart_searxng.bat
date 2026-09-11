@echo off
REM =====================================================================
REM  LeadHunter Pro - restart SearXNG ONLY
REM =====================================================================

echo Restarting SearXNG (8080) ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8080 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
timeout /t 1 /nobreak >nul

start "SearXNG :8080" /D C:\Users\SHAKIR\searxng-master cmd /k "set SEARXNG_SETTINGS_PATH=C:\Users\SHAKIR\searxng-master\searx\settings.yml && C:\Users\SHAKIR\searxng-venv\Scripts\python.exe -m searx.webapp"

echo SearXNG is restarting in its window.
pause