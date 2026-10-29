@echo off
setlocal EnableDelayedExpansion
title Docker Compose Tool

REM ---- Work in the folder where this .bat file is located ----
cd /d "%~dp0"

REM ---- Make sure Docker is running ----
docker info >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not running. Start Docker Desktop and try again.
    pause
    exit /b 1
)

REM ---- Pick "docker compose" (v2) or "docker-compose" (v1) ----
set "DC=docker compose"
docker compose version >nul 2>&1
if errorlevel 1 (
    docker-compose version >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Docker Compose is not installed.
        pause
        exit /b 1
    )
    set "DC=docker-compose"
)

REM ---- Make sure a compose file exists here ----
set "CFILE="
for %%f in (compose.yaml compose.yml docker-compose.yaml docker-compose.yml) do (
    if not defined CFILE if exist "%%f" set "CFILE=%%f"
)
if not defined CFILE (
    echo [ERROR] No docker-compose.yml found in:
    echo   %CD%
    echo Put this .bat file in the same folder as your docker-compose.yml.
    pause
    exit /b 1
)

:MENU
cls
echo ==================================================
echo                Docker Compose Tool
echo ==================================================
echo  Folder : %CD%
echo  File   : %CFILE%
echo ==================================================
echo.
echo   1. Build images
echo   2. Build images (no cache, clean rebuild)
echo   3. Start containers (up -d)
echo   4. Build + Start (up -d --build)
echo   5. Stop and remove containers (down)
echo   6. Restart containers
echo   7. Show status (ps)
echo   8. Show logs (follow)
echo   9. Pull latest images
echo  10. Open a shell inside a container
echo  11. Full reset: down + clean rebuild + up
echo   0. Exit
echo.
set "ACTION="
set /p "ACTION=Choose an option: "

if "%ACTION%"=="1"  goto BUILD
if "%ACTION%"=="2"  goto BUILD_NOCACHE
if "%ACTION%"=="3"  goto UP
if "%ACTION%"=="4"  goto UP_BUILD
if "%ACTION%"=="5"  goto DOWN
if "%ACTION%"=="6"  goto RESTART
if "%ACTION%"=="7"  goto STATUS
if "%ACTION%"=="8"  goto LOGS
if "%ACTION%"=="9"  goto PULL
if "%ACTION%"=="10" goto SHELL
if "%ACTION%"=="11" goto RESET
if "%ACTION%"=="0"  exit /b 0
goto MENU


REM =====================================================
REM   Helper: choose a service (blank = all services)
REM =====================================================
:PICK_SERVICE
set "SVC="
set scount=0
echo.
echo Services in %CFILE%:
for /f "delims=" %%s in ('%DC% config --services 2^>nul') do (
    set /a scount+=1
    set "svc!scount!=%%s"
    echo   !scount!. %%s
)
echo.
set "SCHOICE="
set /p "SCHOICE=Service number (press Enter for ALL): "
if not defined SCHOICE goto :eof
set "SVC=!svc%SCHOICE%!"
if not defined SVC echo Invalid choice, using ALL services.
goto :eof


:BUILD
call :PICK_SERVICE
echo.
echo Building !SVC! ...
%DC% build !SVC!
goto DONE

:BUILD_NOCACHE
call :PICK_SERVICE
echo.
echo Clean rebuild (no cache) !SVC! ...
%DC% build --no-cache --pull !SVC!
goto DONE

:UP
call :PICK_SERVICE
echo.
%DC% up -d !SVC!
goto DONE

:UP_BUILD
call :PICK_SERVICE
echo.
%DC% up -d --build !SVC!
goto DONE

:DOWN
echo.
echo Stopping and removing containers (volumes are KEPT)...
%DC% down
goto DONE

:RESTART
call :PICK_SERVICE
echo.
%DC% restart !SVC!
goto DONE

:STATUS
echo.
%DC% ps
goto DONE

:LOGS
call :PICK_SERVICE
echo.
echo Showing logs. Press Ctrl+C to stop, then answer N to return to the menu.
echo.
%DC% logs -f --tail 200 !SVC!
goto DONE

:PULL
echo.
%DC% pull
goto DONE

:SHELL
call :PICK_SERVICE
if not defined SVC (
    echo You must choose one service.
    pause
    goto MENU
)
echo.
echo Opening shell in "!SVC!" (type exit to leave)...
%DC% exec !SVC! bash 2>nul || %DC% exec !SVC! sh
goto DONE

:RESET
echo.
echo This will stop containers, rebuild all images without cache, and start again.
echo Your volumes (data) are NOT deleted.
set "GO="
set /p "GO=Continue? (Y/N): "
if /i not "%GO%"=="Y" goto MENU
%DC% down
%DC% build --no-cache --pull
%DC% up -d
goto DONE


:DONE
echo.
if errorlevel 1 (
    echo [ERROR] The command finished with an error. Check the messages above.
) else (
    echo [OK] Done.
)
echo.
pause
goto MENU
