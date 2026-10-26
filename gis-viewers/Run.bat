@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ============================================================
rem  GIS viewers - MapLibre, ArcGIS, CesiumJS
rem  Usage: Run.bat [platform URL]
rem          Run.bat http://127.0.0.1:8787
rem ============================================================

set "PLATFORM_URL=http://127.0.0.1:8787"
if not "%~1"=="" set "PLATFORM_URL=%~1"
set "URL=http://127.0.0.1:5173/"
rem Port must be resolved OUTSIDE the if-block below: %PLATFORM_PORT% inside a
rem parenthesized block is expanded when the block is parsed, before the for /f
rem assignment runs, which silently launched `python -m app --port` with no value.
set "PLATFORM_PORT=8787"
for /f "tokens=3 delims=:/ " %%P in ("%PLATFORM_URL%") do set "PLATFORM_PORT=%%P"
if not defined PLATFORM_PORT set "PLATFORM_PORT=8787"
if "%PLATFORM_PORT%"=="" set "PLATFORM_PORT=8787"

where npm >nul 2>&1
if errorlevel 1 goto :no_node
if not exist "node_modules" goto :missing_modules

if not exist ".env.local" (
  >.env.local echo VITE_PLATFORM_URL=%PLATFORM_URL%
) else (
echo Using existing .env.local
)

rem --- Make sure the data platform API is running (viewers need it) ---
set "PLATFORM_DIR=%~dp0..\gis-data-platform"
curl -s -o nul -m 2 "%PLATFORM_URL%/health"
if errorlevel 1 (
  if exist "%PLATFORM_DIR%\.venv\Scripts\python.exe" (
    echo Platform not responding at %PLATFORM_URL% - starting gis-data-platform...
    start "GIS data platform - API" /D "%PLATFORM_DIR%" cmd /c ".venv\Scripts\python.exe -m app --host 127.0.0.1 --port %PLATFORM_PORT% || pause"
    %SystemRoot%\System32\ping.exe -n 4 127.0.0.1 >nul
  ) else (
    echo [WARN] Platform not responding at %PLATFORM_URL% and %PLATFORM_DIR% has no .venv.
    echo        Start gis-data-platform\Run.bat manually, or the map will be empty.
  )
) else (
  echo Platform already running at %PLATFORM_URL%
)

echo.
echo   GIS viewers
echo   platform  : %PLATFORM_URL%
echo   viewer     : %URL%
echo   tabs       : MapLibre / ArcGIS / CesiumJS
echo.
echo   Close this window or press Ctrl+C to stop.
echo.

start "GIS viewers - Vite" cmd /c "npm run dev -- --host 127.0.0.1 || pause"
%SystemRoot%\System32\ping.exe -n 5 127.0.0.1 >nul
start "" "%URL%"
exit /b 0

:no_node
echo [ERROR] Node.js/npm was not found.
echo Install Node.js 18+ and run this file again.
pause
exit /b 1

:missing_modules
echo [ERROR] node_modules was not found.
echo.
echo Install dependencies with:
echo   npm install
pause
exit /b 1
