@echo off
REM ============================================================
REM  LeadHunter Pro Backend - canonical launcher.
REM  ALWAYS runs on port 8000 (the port the frontend proxies to).
REM  Run from anywhere: double-click or:  start_backend.bat
REM
REM  GUARANTEE: ONE backend, port 8000, current code.
REM  On every start this KILLS every previous instance of THIS
REM  backend, wherever it is running - any port, any host, parent
REM  or worker, and no matter HOW it was started (this script, a
REM  VS Code task, a terminal, an old launcher). Two servers can
REM  never stack and a frozen one is always replaced.
REM
REM  Why this matters (2026-09-09): a stale instance (started from
REM  an old script, not this one) was sitting on 8000 in a frozen
REM  state - it accepted TCP but answered no HTTP request, so the
REM  frontend showed "loading live data" forever. The fix: kill
REM  THIS project's backend by command line, not just the 8000
REM  listener, so even a wedged twin that never bound a port gets
REM  swept before the fresh server starts.
REM
REM  Gotcha: if the server ever stops answering again (frontend
REM  stuck on "loading live data"), the usual cause is the Windows
REM  console QuickEdit freeze - clicking inside the terminal window
REM  PAUSES the whole process (the port stays bound, but nothing
REM  is served). Just re-run this script: it kills the frozen one
REM  and starts a fresh copy. To avoid the freeze entirely, start
REM  the backend in its own window and do not click it.
REM ============================================================
cd /d "%~dp0"

REM --- 1. Kill EVERY previous instance of THIS backend ---------
REM Matches only python.exe processes whose command line runs
REM uvicorn on app.main (the LeadHunterPro backend), on ANY
REM port/host. Deliberately python.exe-only so this PowerShell
REM command can never kill itself, and SearXNG (which runs as
REM python -m searx.webapp) is left untouched.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'uvicorn' -and $_.CommandLine -match 'app.main' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

REM --- 2. Belt-and-suspenders: free port 8000 directly ---------
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":8000 .*LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)

echo [LeadHunter] Starting backend on http://127.0.0.1:8000 ...
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000