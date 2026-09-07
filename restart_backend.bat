@echo off
REM =====================================================================
REM  LeadHunter Pro - restart Backend ONLY
REM =====================================================================

echo Restarting Backend (8000) ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
timeout /t 1 /nobreak >nul

start "Backend :8000" /D C:\Users\SHAKIR\Desktop\LeadHunterPro\backend cmd /k "C:\Users\SHAKIR\Desktop\LeadHunterPro\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

echo Backend is restarting in its window.
pause