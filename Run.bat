@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ============================================================
rem  GIS platform + viewers
rem    Docker (preferred) : Run.bat [-y] [up|down|logs|build|restart]
rem                         -y = don't ask when a DEM job is running
rem    No Docker installed: Run.bat falls back to a local dev run
rem                         (API via gis-data-platform\.venv + Vite dev server)
rem  NOTE: keep this file CRLF (see .gitattributes) or cmd.exe cannot parse it.
rem ============================================================

set "ASSUME_YES=0"
set "ACTION=%~1"
if /i "%ACTION%"=="-y" goto :flag_yes
if /i "%ACTION%"=="--yes" goto :flag_yes
goto :flags_done
:flag_yes
set "ASSUME_YES=1"
set "ACTION=%~2"
:flags_done
if "%ACTION%"=="" set "ACTION=up"

if not exist ".env" if exist ".env.example" copy /y ".env.example" ".env" >nul && echo Created .env from .env.example

rem --- host ports from .env (compose reads the same file). Resolved here, at
rem     top level: %VAR% set inside a ( ) block is not visible in that block.
set "API_PORT=5000"
set "VIEWER_PORT=3100"
if exist ".env" for /f "tokens=2 delims== " %%p in ('findstr /b /c:"GIS_PLATFORM_PORT=" .env 2^>nul') do set "API_PORT=%%p"
if exist ".env" for /f "tokens=2 delims== " %%p in ('findstr /b /c:"GIS_VIEWER_PORT=" .env 2^>nul') do set "VIEWER_PORT=%%p"

rem --- find docker: PATH first, then the Docker Desktop install locations
rem     (Docker Desktop can be installed without putting docker on PATH).
set "DOCKER="
where docker >nul 2>&1 && set "DOCKER=docker"
if not defined DOCKER if exist "%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin\docker.exe" set "DOCKER=%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin\docker.exe"
if not defined DOCKER if exist "%ProgramFiles%\Docker\Docker\resources\bin\docker.exe" set "DOCKER=%ProgramFiles%\Docker\Docker\resources\bin\docker.exe"
if not defined DOCKER goto :local
rem The credential helpers next to docker.exe must be on PATH too.
if not "%DOCKER%"=="docker" for %%D in ("%DOCKER%") do set "PATH=%%~dpD;%PATH%"

if /i "%ACTION%"=="up" goto :up
if /i "%ACTION%"=="down" goto :down
if /i "%ACTION%"=="logs" goto :logs
if /i "%ACTION%"=="build" goto :build
if /i "%ACTION%"=="restart" goto :restart
echo Usage: %~nx0 [-y] [up^|down^|logs^|build^|restart]
exit /b 2

:up
call :require_daemon || goto :fail
call :check_running_job || goto :aborted
echo Starting GIS data platform and viewer containers...
"%DOCKER%" compose up -d --build
if errorlevel 1 (
  set "FAIL_MSG=docker compose up failed - see the messages above."
  goto :fail
)
echo.
echo Viewer:  http://127.0.0.1:%VIEWER_PORT%/
echo API:     http://127.0.0.1:%API_PORT%/docs
echo Fetch:   http://127.0.0.1:%VIEWER_PORT%/fetch/
exit /b 0

:down
call :require_daemon || goto :fail
call :check_running_job || goto :aborted
"%DOCKER%" compose down
if errorlevel 1 goto :fail
exit /b 0

:logs
call :require_daemon || goto :fail
"%DOCKER%" compose logs -f
exit /b %errorlevel%

:build
call :require_daemon || goto :fail
"%DOCKER%" compose build
if errorlevel 1 goto :fail
exit /b 0

:restart
call :require_daemon || goto :fail
call :check_running_job || goto :aborted
"%DOCKER%" compose down
if errorlevel 1 goto :fail
"%DOCKER%" compose up -d
if errorlevel 1 goto :fail
exit /b 0

rem ---------------------------------------------------------------------------
:require_daemon
"%DOCKER%" info >nul 2>&1
if not errorlevel 1 exit /b 0
set "FAIL_MSG=Docker is installed but the Docker engine is not reachable. Start Docker Desktop, wait until it reports 'Engine running', then run this file again."
exit /b 1

rem Recreating/stopping data-platform kills a running fetch/build job (job state
rem is in memory). Warn and ask unless -y. Returns 1 when the user declines.
:check_running_job
where curl.exe >nul 2>&1 || exit /b 0
set "JOB_TMP=%TEMP%\gis-current-job-%RANDOM%.json"
curl.exe -s -m 3 -o "%JOB_TMP%" "http://127.0.0.1:%API_PORT%/api/jobs/current" >nul 2>&1
set "JOB_RUNNING=0"
if exist "%JOB_TMP%" findstr /c:"\"state\":\"running\"" "%JOB_TMP%" >nul 2>&1 && set "JOB_RUNNING=1"
if exist "%JOB_TMP%" del /q "%JOB_TMP%" >nul 2>&1
if "%JOB_RUNNING%"=="0" exit /b 0
echo.
echo [WARN] A DEM job is RUNNING on http://127.0.0.1:%API_PORT% .
echo        "%ACTION%" recreates/stops the API container and will KILL that job.
if "%ASSUME_YES%"=="1" echo        -y given, continuing.
if "%ASSUME_YES%"=="1" exit /b 0
choice /c YN /n /m "Continue anyway? [Y/N] "
if errorlevel 2 exit /b 1
exit /b 0

:aborted
echo Aborted - nothing was changed.
exit /b 1

:fail
echo.
if defined FAIL_MSG (echo [ERROR] %FAIL_MSG%) else (echo [ERROR] The last command failed - see the messages above.)
echo        Hint: is Docker Desktop running? Try "docker compose ps" / "docker compose logs".
pause
exit /b 1

rem ---------------------------------------------------------------------------
:local
if /i "%ACTION%"=="down" goto :local_down
echo [INFO] Docker was not found - running locally instead (API + Vite dev server).
echo        Install Docker Desktop to use the containerized deployment.

set "PLATFORM_DIR=%~dp0gis-data-platform"
set "VIEWER_DIR=%~dp0gis-viewers"

rem --- 1. Python dependencies for the data platform (venv) ---
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  set "FAIL_MSG=Python 3 was not found. Install Python 3.11+ and run this file again."
  goto :local_fail
)
if exist "%PLATFORM_DIR%\.venv\Scripts\python.exe" goto :local_node
echo Creating gis-data-platform\.venv and installing Python dependencies...
%PY% -m venv "%PLATFORM_DIR%\.venv"
if errorlevel 1 goto :local_fail
"%PLATFORM_DIR%\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%PLATFORM_DIR%\requirements.txt"
if errorlevel 1 goto :local_fail

rem --- 2. Node dependencies for the viewers ---
:local_node
where npm >nul 2>&1
if errorlevel 1 (
  set "FAIL_MSG=Node.js/npm was not found. Install Node.js 20.19+ or 22.12+ and run this file again."
  goto :local_fail
)
if exist "%VIEWER_DIR%\node_modules" goto :local_start
echo Installing viewer dependencies...
pushd "%VIEWER_DIR%"
call npm install
set "NPM_RC=%errorlevel%"
popd
if not "%NPM_RC%"=="0" goto :local_fail

rem --- 3. Start API + viewer ---
:local_start
call "%VIEWER_DIR%\Run.bat"
exit /b %errorlevel%

:local_fail
echo.
if defined FAIL_MSG (echo [ERROR] %FAIL_MSG%) else (echo [ERROR] Local setup failed - see the messages above.)
pause
exit /b 1

:local_down
echo [INFO] Local mode runs no daemon - close the API/viewer windows instead.
exit /b 0
