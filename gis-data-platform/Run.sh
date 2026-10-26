@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ============================================================
rem  GIS data platform - local API and data services
rem  Usage: Run.bat [port] [host]
rem          Run.bat 8787 0.0.0.0
rem  Use 0.0.0.0 to make the API reachable over the LAN.
rem ============================================================

set "PORT=8787"
set "HOST=0.0.0.0"
if not "%~1"=="" set "PORT=%~1"
if not "%~2"=="" set "HOST=%~2"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "LOCAL_URL=http://127.0.0.1:%PORT%/docs"

if not exist "%PYTHON%" goto :no_venv

"%PYTHON%" -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 goto :missing_deps

for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /R /C:"IPv4 Address" /C:"IPv4-Adresse"') do set "LAN_IP=%%A"
set "LAN_IP=%LAN_IP: =%"
if not defined LAN_IP set "LAN_IP=YOUR_SERVER_LAN_IP"

echo.
echo   GIS data platform
echo   bind     : %HOST%:%PORT%
echo   local    : %LOCAL_URL%
echo   LAN      : http://%LAN_IP%:%PORT%
echo   health   : http://%LAN_IP%:%PORT%/health
echo   terrain  : http://%LAN_IP%:%PORT%/tiles/terrain.json
echo.
echo   Close this window or press Ctrl+C to stop.
echo.

start "GIS data platform - API" /D "%~dp0" "%PYTHON%" -m app --host %HOST% --port %PORT%
%SystemRoot%\System32\ping.exe -n 3 127.0.0.1 >nul
start "" "%LOCAL_URL%"
exit /b 0

:no_venv
echo [ERROR] Project virtual environment was not found:
echo   %PYTHON%
echo.
echo Create it with:
echo   uv venv --python 3.13 .venv
echo   uv pip install --python .venv\Scripts\python.exe -r requirements.txt
pause
exit /b 1

:missing_deps
echo [ERROR] Dependencies are missing from the project virtual environment.
echo.
echo Install them with:
echo   "%PYTHON%" -m pip install -r requirements.txt
pause
exit /b 1
