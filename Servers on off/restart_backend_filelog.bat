@echo off
REM =====================================================================
REM  LeadHunter Pro - restart Backend ONLY (JAM-PROOF file-log mode)
REM
REM  WHY: the 2026-09-10 freeze root cause was the producer thread blocking
REM  inside logging.StreamHandler(sys.stdout) when the server's cmd console
REM  window stopped draining output (Windows QuickEdit/select mode). A jammed
REM  console makes stdout.write() block FOREVER -> the whole run hangs.
REM
REM  FIX: every log line goes to backend\output\live_server.log instead of
REM  the console. A file never blocks, so a clicked/jammed console window can
REM  no longer freeze a search.
REM =====================================================================

echo Restarting Backend (8000) - JAM-PROOF (logs -> output\live_server.log) ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
timeout /t 1 /nobreak >nul

start "Backend :8000" /D C:\Users\SHAKIR\Desktop\LeadHunterPro\backend cmd /k "C:\Users\SHAKIR\Desktop\LeadHunterPro\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 > output\live_server.log 2>&1"

echo Backend is restarting in its window. Tail output\live_server.log to watch it.
pause