@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ============================================================
rem  GIS map load test
rem
rem  Usage:
rem    RunLoadTest.bat [server_url] [users] [duration_seconds] [extra_options]
rem
rem  Examples:
rem    RunLoadTest.bat http://192.168.1.50:8787 10 60
rem    RunLoadTest.bat http://192.168.1.50:8787 100 300 --min-pause 1 --max-pause 3
rem    RunLoadTest.bat http://192.168.1.50:8787 100 300 --no-imagery
rem ============================================================

rem For LAN testing, always pass the server machine's LAN address.
rem Do not use localhost or 127.0.0.1 from the client machine.

set "BASE_URL=%~1"
set "USERS=%~2"
set "DURATION=%~3"

if not defined BASE_URL (
    echo.
    echo Enter the server machine LAN URL, for example:
    echo   http://192.168.1.50:8787
    set /p "BASE_URL=Server URL: "
)
if not defined BASE_URL set "BASE_URL=http://127.0.0.1:8787"
if not defined USERS set "USERS=10"
if not defined DURATION set "DURATION=60"

rem Forward options after the first three positional arguments.
set "EXTRA_OPTIONS="
if not "%~4"=="" set "EXTRA_OPTIONS=%~4"
if not "%~5"=="" set "EXTRA_OPTIONS=%EXTRA_OPTIONS% %~5"
if not "%~6"=="" set "EXTRA_OPTIONS=%EXTRA_OPTIONS% %~6"
if not "%~7"=="" set "EXTRA_OPTIONS=%EXTRA_OPTIONS% %~7"
if not "%~8"=="" set "EXTRA_OPTIONS=%EXTRA_OPTIONS% %~8"
if not "%~9"=="" set "EXTRA_OPTIONS=%EXTRA_OPTIONS% %~9"

rem Prefer gis-data-platform\.venv, then python on PATH, then the py launcher
rem (on many machines only "py" resolves in cmd).
set "PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
if "%PYTHON%"=="python" python --version >nul 2>&1 || set "PYTHON=py"

"%PYTHON%" --version >nul 2>&1
if errorlevel 1 goto :no_python

if /I "%BASE_URL%"=="http://127.0.0.1:8787" (
    echo [INFO] Localhost selected. For a LAN test, pass the server IPv4 address.
    echo        Example: RunLoadTest.bat http://192.168.1.50:8787 100 300
)

"%PYTHON%" -c "import aiohttp" >nul 2>&1
if errorlevel 1 goto :install_deps

goto :run

:install_deps
echo.
echo [INFO] aiohttp is not installed. Installing load-test dependencies...
"%PYTHON%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :install_failed

:run
echo.
echo GIS map load test
echo   target   : %BASE_URL%
echo   users    : %USERS%
echo   duration : %DURATION% seconds
echo.
echo Press Ctrl+C to stop the test.
echo.

"%PYTHON%" "%~dp0map_load_test.py" --base-url "%BASE_URL%" --users "%USERS%" --duration "%DURATION%" %EXTRA_OPTIONS%
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo [PASS] Load test completed without reported errors.
) else if "%EXIT_CODE%"=="1" (
    echo [WARN] Load test completed, but HTTP or network errors were reported.
) else (
    echo [ERROR] Load test could not start. Exit code: %EXIT_CODE%
)
pause
exit /b %EXIT_CODE%

:no_python
echo [ERROR] Python was not found.
echo Install Python 3.11 or newer and ensure it is available as "python".
pause
exit /b 2

:install_failed
echo [ERROR] Could not install aiohttp.
echo Try manually: "%PYTHON%" -m pip install -r "%~dp0requirements.txt"
pause
exit /b 2
