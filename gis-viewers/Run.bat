@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ============================================================
rem  GIS viewers - MapLibre, ArcGIS, CesiumJS (Vite dev server)
rem  Usage: Run.bat [platform URL]
rem          Run.bat http://127.0.0.1:8787
rem  A platform URL argument always (re)writes .env.local.
rem  NOTE: keep this file CRLF (see .gitattributes) or cmd.exe cannot parse it.
rem ============================================================

set "PLATFORM_URL=http://127.0.0.1:8787"
if not "%~1"=="" set "PLATFORM_URL=%~1"
set "URL=http://127.0.0.1:5173/"
rem Port must be resolved OUTSIDE any ( ) block: %VAR% inside a parenthesized
rem block is expanded when the block is parsed, before a for /f assignment in
rem it runs (that once launched `python -m app --port` with no value).
set "PLATFORM_PORT="
for /f "tokens=3 delims=:/ " %%P in ("%PLATFORM_URL%") do set "PLATFORM_PORT=%%P"
if not defined PLATFORM_PORT set "PLATFORM_PORT=8787"

where npm >nul 2>&1
if errorlevel 1 goto :no_node
if not exist "node_modules" goto :missing_modules

rem Vite 7 needs Node.js ^20.19 or >=22.12 - warn early instead of a cryptic crash.
node -e "const [a,b]=process.versions.node.split('.').map(Number);process.exit((a===20&&b>=19)||(a===22&&b>=12)||a>22?0:1)" >nul 2>&1
if errorlevel 1 echo [WARN] Node.js 20.19+ or 22.12+ is required by Vite; found: & node --version

if not "%~1"=="" goto :write_env
if exist ".env.local" goto :env_existing
:write_env
>".env.local" echo VITE_PLATFORM_URL=%PLATFORM_URL%
echo Wrote .env.local (VITE_PLATFORM_URL=%PLATFORM_URL%)
goto :env_done
:env_existing
echo Using existing .env.local
:env_done

set "HAVE_CURL=0"
where curl.exe >nul 2>&1 && set "HAVE_CURL=1"

rem --- Make sure the data platform API is running (viewers need it) ---
set "PLATFORM_DIR=%~dp0..\gis-data-platform"
if "%HAVE_CURL%"=="0" goto :start_platform
curl.exe -fs -o nul -m 2 "%PLATFORM_URL%/health" >nul 2>&1
if errorlevel 1 goto :start_platform
echo Platform already running at %PLATFORM_URL%
goto :platform_done

:start_platform
if not exist "%PLATFORM_DIR%\.venv\Scripts\python.exe" goto :no_platform_venv
echo Platform not responding at %PLATFORM_URL% - starting gis-data-platform...
start "GIS data platform - API" /D "%PLATFORM_DIR%" cmd /c ".venv\Scripts\python.exe -m app --host 127.0.0.1 --port %PLATFORM_PORT% || pause"
if "%HAVE_CURL%"=="0" (
  %SystemRoot%\System32\ping.exe -n 4 127.0.0.1 >nul
  goto :platform_done
)
call :wait_url "%PLATFORM_URL%/health" API 30
if errorlevel 1 echo [WARN] API not ready after ~30 s - check the "GIS data platform - API" window.
goto :platform_done

:no_platform_venv
echo [WARN] Platform not responding at %PLATFORM_URL% and %PLATFORM_DIR% has no .venv.
echo        Start gis-data-platform\Run.bat manually, or the map will be empty.

:platform_done
echo.
echo   GIS viewers
echo   platform  : %PLATFORM_URL%
echo   viewer    : %URL%
echo   tabs      : MapLibre / ArcGIS / CesiumJS
echo.
echo   Close the Vite window or press Ctrl+C there to stop.
echo.

start "GIS viewers - Vite" cmd /c "npm run dev -- --host 127.0.0.1 || pause"
if "%HAVE_CURL%"=="0" (
  %SystemRoot%\System32\ping.exe -n 5 127.0.0.1 >nul
) else (
  call :wait_url "%URL%" viewer 30
)
start "" "%URL%"
exit /b 0

rem call :wait_url URL LABEL MAX_SECONDS  -> errorlevel 0 when URL answers 2xx
:wait_url
set "W_URL=%~1"
set "W_LABEL=%~2"
set /a W_MAX=%~3
set /a W_N=0
:wait_url_loop
curl.exe -fs -o nul -m 2 "%W_URL%" >nul 2>&1 && exit /b 0
if %W_N% GEQ %W_MAX% exit /b 1
echo   waiting for %W_LABEL%... %W_N%s
%SystemRoot%\System32\ping.exe -n 2 127.0.0.1 >nul
set /a W_N+=1
goto :wait_url_loop

:no_node
echo [ERROR] Node.js/npm was not found.
echo Install Node.js 20.19+ or 22.12+ and run this file again.
pause
exit /b 1

:missing_modules
echo [ERROR] node_modules was not found.
echo.
echo Install dependencies with:
echo   npm install
pause
exit /b 1
