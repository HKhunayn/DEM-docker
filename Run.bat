@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ============================================================
rem  DEM GIS stack - three apps sharing one data volume:
rem    engine         builds + serves tiles   (GIS_ENGINE_PORT, default 5000)
rem    data-platform  downloads only          (GIS_DATA_PORT,   default 5100)
rem    viewer         optional web viewer     (GIS_VIEWER_PORT, default 3100)
rem
rem    Docker (preferred) : Run.bat [-y] [up|down|logs|build|restart|status] [service ...]
rem        Run.bat up                    all three
rem        Run.bat up engine             serve + build only (no downloads)
rem        Run.bat up data-platform      downloads only
rem        Run.bat up viewer             viewer (starts the engine too)
rem        -y = don't ask when a job is running
rem    No Docker installed: Run.bat falls back to a local dev run
rem                         (API via gis-data-platform\.venv + Vite dev server)
rem  NOTE: keep this file CRLF (see .gitattributes) or cmd.exe cannot parse it.
rem ============================================================

set "SELF=%~nx0"
set "ASSUME_YES=0"
set "ACTION="
set "SERVICES="
:parse_flags
if /i "%~1"=="-y" (
  set "ASSUME_YES=1"
  shift
  goto :parse_flags
)
if /i "%~1"=="--yes" (
  set "ASSUME_YES=1"
  shift
  goto :parse_flags
)
set "ACTION=%~1"
if not "%~1"=="" shift
:parse_services
if "%~1"=="" goto :args_done
set "SERVICES=%SERVICES% %~1"
shift
goto :parse_services
:args_done
if "%ACTION%"=="" set "ACTION=up"
for %%s in (%SERVICES%) do (
  if /i not "%%s"=="engine" if /i not "%%s"=="data-platform" if /i not "%%s"=="viewer" (
    echo [ERROR] unknown service "%%s" ^(engine, data-platform, viewer^)
    exit /b 2
  )
)

if not exist ".env" if exist ".env.example" copy /y ".env.example" ".env" >nul && echo Created .env from .env.example

rem --- host ports from .env (compose reads the same file). Resolved here, at
rem     top level: %VAR% set inside a ( ) block is not visible in that block.
rem     GIS_PLATFORM_PORT is the old name of the engine port (still honoured).
set "ENGINE_PORT=5000"
set "DATA_PORT=5100"
set "VIEWER_PORT=3100"
if exist ".env" for /f "tokens=2 delims== " %%p in ('findstr /b /c:"GIS_PLATFORM_PORT=" .env 2^>nul') do set "ENGINE_PORT=%%p"
if exist ".env" for /f "tokens=2 delims== " %%p in ('findstr /b /c:"GIS_ENGINE_PORT=" .env 2^>nul') do set "ENGINE_PORT=%%p"
if exist ".env" for /f "tokens=2 delims== " %%p in ('findstr /b /c:"GIS_DATA_PORT=" .env 2^>nul') do set "DATA_PORT=%%p"
if exist ".env" for /f "tokens=2 delims== " %%p in ('findstr /b /c:"GIS_VIEWER_PORT=" .env 2^>nul') do set "VIEWER_PORT=%%p"
rem Variables set in the shell win over .env (same rule as docker compose).
if defined GIS_PLATFORM_PORT if not defined GIS_ENGINE_PORT set "ENGINE_PORT=%GIS_PLATFORM_PORT%"
if defined GIS_ENGINE_PORT set "ENGINE_PORT=%GIS_ENGINE_PORT%"
if defined GIS_DATA_PORT set "DATA_PORT=%GIS_DATA_PORT%"
if defined GIS_VIEWER_PORT set "VIEWER_PORT=%GIS_VIEWER_PORT%"

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
if /i "%ACTION%"=="status" goto :status
echo Usage: %SELF% [-y] [up^|down^|logs^|build^|restart^|status] [engine^|data-platform^|viewer ...]
exit /b 2

:up
call :require_daemon || goto :fail
call :check_running_job || goto :aborted
if defined SERVICES (echo Starting:%SERVICES% ...) else (echo Starting engine, data-platform and viewer ...)
rem --remove-orphans: drops the old "gateway" container (the engine replaced it)
"%DOCKER%" compose up -d --build --remove-orphans %SERVICES%
if errorlevel 1 (
  set "FAIL_MSG=docker compose up failed - see the messages above."
  goto :fail
)
call :print_urls
exit /b 0

:down
call :require_daemon || goto :fail
call :check_running_job || goto :aborted
if defined SERVICES (
  "%DOCKER%" compose stop %SERVICES%
) else (
  "%DOCKER%" compose down
)
if errorlevel 1 goto :fail
exit /b 0

:logs
call :require_daemon || goto :fail
"%DOCKER%" compose logs -f %SERVICES%
exit /b %errorlevel%

:build
call :require_daemon || goto :fail
"%DOCKER%" compose build %SERVICES%
if errorlevel 1 goto :fail
exit /b 0

:restart
call :require_daemon || goto :fail
call :check_running_job || goto :aborted
"%DOCKER%" compose restart %SERVICES%
if errorlevel 1 goto :fail
call :print_urls
exit /b 0

:status
call :require_daemon || goto :fail
"%DOCKER%" compose ps
exit /b %errorlevel%

rem ---------------------------------------------------------------------------
:require_daemon
"%DOCKER%" info >nul 2>&1
if not errorlevel 1 exit /b 0
set "FAIL_MSG=Docker is installed but the Docker engine is not reachable. Start Docker Desktop, wait until it reports 'Engine running', then run this file again."
exit /b 1

rem :affects NAME -> AFFECTS=1 when NAME is part of this command (no service
rem list = all services).
:affects
set "AFFECTS=0"
if not defined SERVICES (
  set "AFFECTS=1"
  exit /b 0
)
echo  %SERVICES% | findstr /i /c:" %~1" >nul 2>&1 && set "AFFECTS=1"
exit /b 0

rem :job_running URL -> JOB_RUNNING=1 when that app reports a running job.
:job_running
set "JOB_RUNNING=0"
where curl.exe >nul 2>&1 || exit /b 0
set "JOB_TMP=%TEMP%\gis-current-job-%RANDOM%.json"
curl.exe -s -m 3 -o "%JOB_TMP%" "%~1" >nul 2>&1
if exist "%JOB_TMP%" findstr /c:"\"state\":\"running\"" "%JOB_TMP%" >nul 2>&1 && set "JOB_RUNNING=1"
if exist "%JOB_TMP%" del /q "%JOB_TMP%" >nul 2>&1
exit /b 0

rem Stopping/recreating the engine interrupts a running BUILD, the data-platform
rem a running DOWNLOAD (both resume later). Warn and ask unless -y. Returns 1
rem when the user declines.
:check_running_job
set "RUNNING_ON="
call :affects engine
if "%AFFECTS%"=="1" call :job_running "http://127.0.0.1:%ENGINE_PORT%/api/jobs/current"
if "%AFFECTS%"=="1" if "%JOB_RUNNING%"=="1" set "RUNNING_ON=%RUNNING_ON% engine(build, port %ENGINE_PORT%)"
call :affects data-platform
if "%AFFECTS%"=="1" call :job_running "http://127.0.0.1:%DATA_PORT%/api/jobs/current"
if "%AFFECTS%"=="1" if "%JOB_RUNNING%"=="1" set "RUNNING_ON=%RUNNING_ON% data-platform(download, port %DATA_PORT%)"
if not defined RUNNING_ON exit /b 0
echo.
echo [WARN] A job is RUNNING on:%RUNNING_ON%
echo        "%ACTION%" restarts that app and interrupts the job (it resumes later).
if "%ASSUME_YES%"=="1" echo        -y given, continuing.
if "%ASSUME_YES%"=="1" exit /b 0
choice /c YN /n /m "Continue anyway? [Y/N] "
if errorlevel 2 exit /b 1
exit /b 0

rem :wait_health NAME URL -> waits up to ~2 min for /health, prints the result
:wait_health
where curl.exe >nul 2>&1 || exit /b 0
set /a "WAIT_N=0"
:wait_health_loop
curl.exe -s -f -m 3 -o nul "%~2" >nul 2>&1
if not errorlevel 1 (
  echo   %~1: up
  exit /b 0
)
set /a "WAIT_N+=1"
if %WAIT_N% geq 60 (
  echo   %~1: NOT answering on %~2 yet - check "docker compose logs %~1"
  exit /b 0
)
timeout /t 2 /nobreak >nul
goto :wait_health_loop

:print_urls
echo.
set "SHOW_ENGINE=0"
call :affects engine
if "%AFFECTS%"=="1" set "SHOW_ENGINE=1"
call :affects viewer
if "%AFFECTS%"=="1" set "SHOW_ENGINE=1"
if "%SHOW_ENGINE%"=="1" (
  call :wait_health engine "http://127.0.0.1:%ENGINE_PORT%/health"
  echo Engine ^(apps connect here^):
  echo   ArcGIS:  http://^<this-host^>:%ENGINE_PORT%/arcgis/rest/services/^<area^>/ImageServer
  echo   Terrain: http://127.0.0.1:%ENGINE_PORT%/tiles/terrain.json
  echo   API:     http://127.0.0.1:%ENGINE_PORT%/docs
)
call :affects data-platform
if "%AFFECTS%"=="1" (
  call :wait_health data-platform "http://127.0.0.1:%DATA_PORT%/health"
  echo Data-platform ^(downloads^): http://127.0.0.1:%DATA_PORT%/fetch/
)
call :affects viewer
if "%AFFECTS%"=="1" echo Viewer:  http://127.0.0.1:%VIEWER_PORT%/
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
