@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ============================================================
rem  GIS platform + viewers
rem    Docker (preferred) : Run.bat [up|down|logs|build|restart]
rem    No Docker installed: Run.bat falls back to a local dev run
rem                         (API via gis-data-platform\.venv + Vite dev server)
rem ============================================================

if not exist ".env" if exist ".env.example" (
  copy /y ".env.example" ".env" >nul
  echo Created .env from .env.example
)

where docker >nul 2>&1
if errorlevel 1 goto :local

if /i "%~1"=="down" goto :down
if /i "%~1"=="logs" goto :logs
if /i "%~1"=="build" goto :build
if /i "%~1"=="restart" goto :restart

echo Starting GIS data platform and viewer containers...
docker compose up -d --build
if errorlevel 1 exit /b 1

rem --- read the actual host ports from .env (compose reads the same file) ---
set "API_PORT=8787"
set "VIEWER_PORT=5173"
if exist ".env" (
  for /f "tokens=2 delims==" %%p in ('findstr /b "GIS_PLATFORM_PORT=" .env 2^>nul') do set "API_PORT=%%p"
  for /f "tokens=2 delims==" %%p in ('findstr /b "GIS_VIEWER_PORT=" .env 2^>nul') do set "VIEWER_PORT=%%p"
)

echo.
echo Viewer:  http://127.0.0.1:%VIEWER_PORT%/
echo API:     http://127.0.0.1:%API_PORT%/docs
echo Fetch:   http://127.0.0.1:%VIEWER_PORT%/fetch/
exit /b 0

:down
docker compose down
exit /b %errorlevel%

:logs
docker compose logs -f
exit /b %errorlevel%

:build
docker compose build
exit /b %errorlevel%

:restart
docker compose down
if errorlevel 1 exit /b 1
docker compose up -d
exit /b %errorlevel%

:local
if /i "%~1"=="down" goto :local_down
echo [INFO] Docker was not found - running locally instead (API + Vite dev server).
echo        Install Docker Desktop to use the containerized deployment.

set "PLATFORM_DIR=%~dp0gis-data-platform"
set "VIEWER_DIR=%~dp0gis-viewers"

rem --- 1. Python dependencies for the data platform (venv) ---
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo [ERROR] Python 3 was not found. Install Python 3.11+ and run this file again.
  exit /b 1
)
if not exist "%PLATFORM_DIR%\.venv\Scripts\python.exe" (
  echo Creating gis-data-platform\.venv and installing Python dependencies...
  %PY% -m venv "%PLATFORM_DIR%\.venv"
  if errorlevel 1 exit /b 1
  "%PLATFORM_DIR%\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%PLATFORM_DIR%\requirements.txt"
  if errorlevel 1 exit /b 1
)

rem --- 2. Node dependencies for the viewers ---
where npm >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Node.js/npm was not found. Install Node.js 18+ and run this file again.
  exit /b 1
)
if not exist "%VIEWER_DIR%\node_modules" (
  echo Installing viewer dependencies...
  pushd "%VIEWER_DIR%"
  call npm install
  if errorlevel 1 (
    popd
    exit /b 1
  )
  popd
)

rem --- 3. Start API + viewer ---
call "%VIEWER_DIR%\Run.bat"
exit /b %errorlevel%

:local_down
echo [INFO] Local mode runs no daemon - close the API/viewer windows instead.
exit /b 0
