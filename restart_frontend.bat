@echo off
REM =====================================================================
REM  LeadHunter Pro - restart Frontend ONLY
REM =====================================================================

echo Restarting Frontend (5173) ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5173 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
timeout /t 1 /nobreak >nul

start "Frontend :5173" /D C:\Users\SHAKIR\Desktop\LeadHunterPro\frontend cmd /k "npm run dev"

echo Frontend is restarting in its window.
pause