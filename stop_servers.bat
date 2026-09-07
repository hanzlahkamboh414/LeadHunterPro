@echo off
REM =====================================================================
REM  LeadHunter Pro - STOP all servers (by listening port)
REM
REM  Finds each server's PID via netstat (LISTENING on its port) and
REM  force-kills it. Established connections never get touched - only the
REM  LISTENING socket owner is killed.
REM =====================================================================

echo Stopping SearXNG  :8080 ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8080 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1

echo Stopping Backend  :8000 ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1

echo Stopping Frontend :5173 ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5173 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1

echo.
echo Done. All servers stopped.
pause