@echo off
setlocal
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "ENTRY=%ROOT%deploy\local\serve.py"
set "ENVFILE=%ROOT%deploy\local\.env"

echo ==========================================================
echo   workbuddy2api  -  local admin console
echo ==========================================================
echo.

rem --- 1. virtual environment ---
if not exist "%PY%" (
  echo [ERROR] Virtual environment not found:
  echo         %PY%
  echo.
  echo Create it first:
  echo         uv venv
  echo         uv pip install -r requirements.lock.txt
  echo.
  goto :fail
)

rem --- 2. config file ---
if not exist "%ENVFILE%" (
  echo [ERROR] Missing config file:
  echo         %ENVFILE%
  echo.
  echo Copy deploy\local\.env and fill in ADMIN_KEY / CODEBUDDY_AUTH_DIR.
  echo.
  goto :fail
)

rem --- 3. port already in use? (warn only) ---
netstat -ano | findstr /C:"LISTENING" | findstr /C:":8787" >nul 2>&1
if not errorlevel 1 (
  echo [WARN] Port 8787 is already in use - another instance may be running.
  echo        Starting anyway; a bind error means you should close the other one.
  echo.
)

echo Starting server ...  (Ctrl+C to stop)
echo   Admin console : http://127.0.0.1:8787/admin/
echo   API base      : http://127.0.0.1:8787/v1
echo.

"%PY%" "%ENTRY%"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo [ERROR] Server exited with code %RC%.
echo Server stopped.

:fail
echo.
pause
endlocal
