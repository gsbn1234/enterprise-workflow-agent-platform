@echo off
REM ---------------------------------------------------------------------------
REM  Phase 6 one-command demo launcher.
REM
REM    demo.bat            start (default)
REM    demo.bat start      start
REM    demo.bat stop       stop only the server this script started
REM    demo.bat status     report whether it is up
REM
REM  FastAPI serves both the API and the built UI, so one process is enough.
REM  Docker is deliberately not involved: this path must work on a machine that
REM  has none, and the container path is documented separately.
REM
REM  Runtime files (pid, log) live under data\, which is already gitignored.
REM ---------------------------------------------------------------------------
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"
cd /d "%ROOT%"

set "RUNTIME_DIR=%ROOT%data\demo"
set "PID_FILE=%RUNTIME_DIR%\server.pid"
set "LOG_FILE=%RUNTIME_DIR%\server.log"
set "PORT=8010"
set "BASE_URL=http://127.0.0.1:%PORT%"
set "SHOWCASE_URL=%BASE_URL%/showcase"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"

REM The demo's configuration, stated here because the showcase makes claims that
REM are only true under it. app/config.py loads .env without overwriting what is
REM already in the environment, so these win.
REM
REM   AGENT_AUTH_REQUIRED=true  Role checks on the approval endpoints. With the
REM     default (false) an employee can approve a production change over HTTP and
REM     the showcase's RBAC card would be describing a disabled button instead of
REM     a boundary. With it on, the API answers 403 -- which is what the card
REM     claims, and what the demo then shows happening.
set "AGENT_AUTH_REQUIRED=true"
REM   AGENT_TOOL_MODE=mock  Change-making tools are simulated. A demo must never
REM     be able to touch real infrastructure, so this is pinned rather than left
REM     to whatever the ambient shell happens to have.
set "AGENT_TOOL_MODE=mock"

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

if /i "%ACTION%"=="start"  goto :start
if /i "%ACTION%"=="stop"   goto :stop
if /i "%ACTION%"=="status" goto :status
echo Usage: demo.bat [start ^| stop ^| status]
exit /b 2

REM ---------------------------------------------------------------- start ----
:start
if not exist "%PYTHON%" (
  echo [demo] Python virtualenv not found at "%PYTHON%".
  echo [demo] Create it first, e.g.:
  echo [demo]     python -m venv .venv
  echo [demo]     .venv\Scripts\python.exe -m pip install -r requirements.txt
  exit /b 1
)

if not exist "%RUNTIME_DIR%" mkdir "%RUNTIME_DIR%" >nul 2>&1

REM Refuse to start a second copy: a stale pid file is cleared, a live one is
REM respected. Nothing is killed here -- that is what stop is for.
if exist "%PID_FILE%" (
  set /p OLD_PID=<"%PID_FILE%"
  call :is_running !OLD_PID!
  if "!RUNNING!"=="1" (
    echo [demo] Already running ^(pid !OLD_PID!^).
    echo [demo] %SHOWCASE_URL%
    start "" "%SHOWCASE_URL%"
    exit /b 0
  )
  echo [demo] Stale pid file ^(pid !OLD_PID! is gone^); starting fresh.
  del "%PID_FILE%" >nul 2>&1
)

echo [demo] Starting uvicorn on port %PORT% ...
echo [demo] Log: %LOG_FILE%
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p = Start-Process -FilePath '%PYTHON%' -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','%PORT%' -WorkingDirectory '%ROOT%' -RedirectStandardOutput '%LOG_FILE%' -RedirectStandardError '%RUNTIME_DIR%\server.err.log' -PassThru -WindowStyle Hidden; $p.Id | Out-File -Encoding ascii '%PID_FILE%'"
if errorlevel 1 (
  echo [demo] Failed to launch the server.
  exit /b 1
)

set /p SERVER_PID=<"%PID_FILE%"
echo [demo] pid %SERVER_PID%; waiting for %BASE_URL%/api/health ...

set "READY=0"
for /l %%i in (1,1,40) do (
  if "!READY!"=="0" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%BASE_URL%/api/health'; if ($r.StatusCode -eq 200) { exit 0 } } catch { }; exit 1" >nul 2>&1
    if !errorlevel! equ 0 (
      set "READY=1"
    ) else (
      powershell -NoProfile -Command "Start-Sleep -Milliseconds 500" >nul 2>&1
    )
  )
)

if "!READY!"=="0" (
  echo [demo] Server did not become healthy. Last lines of the log:
  powershell -NoProfile -Command "if (Test-Path '%LOG_FILE%') { Get-Content '%LOG_FILE%' -Tail 20 }" 2>nul
  echo [demo] Run "demo.bat stop" to clean up pid %SERVER_PID%.
  exit /b 1
)

echo [demo] Ready. Showing the showcase page.
start "" "%SHOWCASE_URL%"
exit /b 0

REM ----------------------------------------------------------------- stop ----
:stop
REM Stops one process: the one whose pid this script wrote. There is no
REM taskkill by image name anywhere in this file -- a demo must never be able to
REM take down every python or node process on the machine.
if not exist "%PID_FILE%" (
  echo [demo] No pid file at "%PID_FILE%"; nothing started by this script is running.
  exit /b 0
)

set /p SERVER_PID=<"%PID_FILE%"
call :is_running %SERVER_PID%
if "%RUNNING%"=="0" (
  echo [demo] pid %SERVER_PID% is not running; removing stale pid file.
  del "%PID_FILE%" >nul 2>&1
  exit /b 0
)

echo [demo] Stopping pid %SERVER_PID% ...
taskkill /PID %SERVER_PID% /T /F >nul 2>&1
del "%PID_FILE%" >nul 2>&1

powershell -NoProfile -Command "Start-Sleep -Milliseconds 400" >nul 2>&1
call :is_running %SERVER_PID%
if "%RUNNING%"=="1" (
  echo [demo] pid %SERVER_PID% is still alive; stop it manually if that is unexpected.
  exit /b 1
)
echo [demo] Stopped.
exit /b 0

REM --------------------------------------------------------------- status ----
:status
if not exist "%PID_FILE%" (
  echo [demo] not running ^(no pid file^)
  exit /b 1
)
set /p SERVER_PID=<"%PID_FILE%"
call :is_running %SERVER_PID%
if "%RUNNING%"=="1" (
  echo [demo] running ^(pid %SERVER_PID%^) on %BASE_URL%
  exit /b 0
)
echo [demo] not running ^(stale pid %SERVER_PID%^)
exit /b 1

REM ------------------------------------------------------------ is_running ----
REM Sets RUNNING=1 when %1 names a live process this script started, else 0.
:is_running
set "RUNNING=0"
if "%~1"=="" exit /b 0
REM The filter is an exact pid match, so only that one row can be printed.
tasklist /FI "PID eq %~1" /NH 2>nul | find "%~1" >nul 2>&1
if not errorlevel 1 set "RUNNING=1"
exit /b 0
