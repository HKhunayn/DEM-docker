@echo off
setlocal EnableDelayedExpansion
title Docker Volume Export / Import Tool

REM ---- Make sure Docker is running ----
docker info >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not running. Start Docker Desktop and try again.
    pause
    exit /b 1
)

:MENU
cls
echo ==========================================
echo     Docker Volume Export / Import Tool
echo ==========================================
echo.
echo   1. Export a volume
echo   2. Import a volume
echo   3. Exit
echo.
set "ACTION="
set /p "ACTION=Choose an option (1-3): "
if "%ACTION%"=="1" goto EXPORT
if "%ACTION%"=="2" goto IMPORT
if "%ACTION%"=="3" exit /b 0
goto MENU


REM =====================================================
REM                       EXPORT
REM =====================================================
:EXPORT
cls
echo ============ EXPORT A VOLUME ============
echo.
echo Available volumes:
echo.
set count=0
for /f "delims=" %%v in ('docker volume ls -q') do (
    set /a count+=1
    set "vol!count!=%%v"
    echo   !count!. %%v
)
if %count%==0 (
    echo   No volumes found.
    pause
    goto MENU
)
echo.
set "CHOICE="
set /p "CHOICE=Enter the number of the volume to export (0 = back): "
if "%CHOICE%"=="0" goto MENU
set "VOL="
set "VOL=!vol%CHOICE%!"
if not defined VOL (
    echo Invalid choice.
    pause
    goto EXPORT
)

REM ---- Warn if a running container is using the volume ----
set "INUSE="
for /f %%c in ('docker ps -q --filter "volume=%VOL%"') do set "INUSE=1"
if defined INUSE (
    echo.
    echo [WARNING] Volume "!VOL!" is used by a running container:
    docker ps --filter "volume=!VOL!" --format "   - {{.Names}}"
    echo It is safer to stop it first ^(docker compose down^) so the data is consistent.
    set "GO="
    set /p "GO=Continue anyway? (Y/N): "
    if /i not "!GO!"=="Y" goto MENU
)

REM ---- Pick the folder to save the backup in ----
echo.
echo Select the folder where the backup will be saved...
set "OUTDIR="
for /f "delims=" %%f in ('powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='Select where to save the volume backup'; if($d.ShowDialog() -eq 'OK'){$d.SelectedPath}"') do set "OUTDIR=%%f"
if not defined OUTDIR (
    echo No folder selected.
    pause
    goto MENU
)
if "%OUTDIR:~-1%"=="\" set "OUTDIR=%OUTDIR:~0,-1%"

for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%t"
set "FILE=%VOL%_%TS%.tar.gz"

echo.
echo Exporting "%VOL%" to "%OUTDIR%\%FILE%" ...
docker run --rm -v "%VOL%:/source:ro" -v "%OUTDIR%:/backup" alpine tar czf "/backup/%FILE%" -C /source .
if errorlevel 1 (
    echo [ERROR] Export failed.
) else (
    echo [OK] Export complete: !OUTDIR!\!FILE!
)
echo.
pause
goto MENU


REM =====================================================
REM                       IMPORT
REM =====================================================
:IMPORT
cls
echo ============ IMPORT A VOLUME ============
echo.
echo Select the backup file (.tar.gz) to import...
set "INFILE="
for /f "delims=" %%f in ('powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.OpenFileDialog; $d.Title='Select the volume backup to import'; $d.Filter='Volume backups (*.tar.gz)'+[char]124+'*.tar.gz'+[char]124+'All files (*.*)'+[char]124+'*.*'; if($d.ShowDialog() -eq 'OK'){$d.FileName}"') do set "INFILE=%%f"
if not defined INFILE (
    echo No file selected.
    pause
    goto MENU
)

for %%A in ("%INFILE%") do (
    set "INDIR=%%~dpA"
    set "INNAME=%%~nxA"
)
set "INDIR=%INDIR:~0,-1%"

REM ---- Suggest the original volume name (strip _timestamp.tar.gz) ----
set "DEFNAME="
for /f "delims=" %%n in ('powershell -NoProfile -Command "'%INNAME%' -replace '_\d{8}_\d{6}\.tar\.gz$','' -replace '\.tar\.gz$',''"') do set "DEFNAME=%%n"

echo.
echo Selected file: %INFILE%
echo.
echo Existing volumes on this machine:
docker volume ls -q
echo.
echo TIP: keep the original name so docker compose finds the volume.
set "TARGET=%DEFNAME%"
set /p "TARGET=Volume name to restore into [%DEFNAME%]: "
if not defined TARGET (
    echo No volume name given.
    pause
    goto MENU
)

REM ---- Refuse if a running container is using the volume ----
set "INUSE="
for /f %%c in ('docker ps -q --filter "volume=%TARGET%"') do set "INUSE=1"
if defined INUSE (
    echo.
    echo [ERROR] Volume "!TARGET!" is used by a running container.
    echo Stop it first ^(docker compose down^) and run the import again.
    pause
    goto MENU
)

REM ---- Create the volume, or confirm overwrite if it exists ----
docker volume inspect "%TARGET%" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo [WARNING] Volume "!TARGET!" already exists.
    echo Its current contents will be DELETED and replaced by the backup.
    set "GO="
    set /p "GO=Continue? (Y/N): "
    if /i not "!GO!"=="Y" goto MENU
) else (
    docker volume create "!TARGET!" >nul
)

echo.
echo Importing "%INNAME%" into volume "%TARGET%" ...
docker run --rm -v "%TARGET%:/target" -v "%INDIR%:/backup:ro" alpine sh -c "find /target -mindepth 1 -delete && tar xzf '/backup/%INNAME%' -C /target"
if errorlevel 1 (
    echo [ERROR] Import failed.
) else (
    echo [OK] Import complete. Volume "!TARGET!" is ready.
)
echo.
pause
goto MENU
