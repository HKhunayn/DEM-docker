@echo off
setlocal EnableDelayedExpansion
title Docker Volume Export / Import Tool

REM ======================================================================
REM  Interactive:  docker-volume-tool.bat
REM  Scripted:     docker-volume-tool.bat export truth VOLUME OUTDIR
REM                docker-volume-tool.bat export full  VOLUME OUTDIR
REM                docker-volume-tool.bat import FILE VOLUME [keeptiles]
REM
REM  TRUTH = everything in the volume EXCEPT tiles/  (DEM COGs, catalog,
REM          sources, imagery, incoming, jobs). tiles/ is derived data: the
REM          platform rebuilds it from the DEMs by itself on the next start.
REM  FULL  = the whole volume including tiles/ (huge, millions of files).
REM
REM  Archives are plain .tar (no gzip): COGs and PNG tiles are already
REM  compressed, so gzip only burns one CPU core for ~0 percent saving.
REM  Old .tar.gz backups can still be imported.
REM ======================================================================

set "SELF=%~f0"
set "HELPER=%TEMP%\docker-volume-tool-helper.sh"
set "IMG=alpine"
set "BATCH="

REM ---- Make sure Docker is running ----
docker info >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not running. Start Docker Desktop and try again.
    if not "%~1"=="" exit /b 1
    pause
    exit /b 1
)

REM ---- Extract the embedded Linux helper script (end of this file) ----
powershell -NoProfile -Command "$t=[IO.File]::ReadAllText($env:SELF); $m='#__DVT'+'_HELPER__'; $i=$t.LastIndexOf($m); if($i -lt 0){exit 1}; $s=$t.Substring($t.IndexOf([char]10,$i)+1) -replace [string][char]13,''; [IO.File]::WriteAllText($env:HELPER,$s)"
if errorlevel 1 (
    echo [ERROR] Could not extract the helper script to "%HELPER%".
    if not "%~1"=="" exit /b 1
    pause
    exit /b 1
)

REM ---- Scripted mode ----
if /i "%~1"=="export" (
    set "BATCH=1"
    set "SCOPE=%~2"
    set "VOL=%~3"
    set "OUTDIR=%~4"
    goto EXPORT_RUN
)
if /i "%~1"=="import" (
    set "BATCH=1"
    set "INFILE=%~f2"
    set "TARGET=%~3"
    set "KEEPTILES=wipe"
    if /i "%~4"=="keeptiles" set "KEEPTILES=keep"
    goto IMPORT_RUN
)
if not "%~1"=="" (
    echo Usage: %~nx0 [export truth^|full VOLUME OUTDIR] [import FILE VOLUME [keeptiles]]
    exit /b 2
)

:MENU
cls
echo ==========================================
echo     Docker Volume Export / Import Tool
echo ==========================================
echo.
echo   1. Export TRUTH data  - DEM + catalog + jobs, no tiles   [recommended]
echo   2. Export FULL volume - also the derived tiles           [huge + slow]
echo   3. Import a backup
echo   4. Exit
echo.
set "ACTION="
set /p "ACTION=Choose an option (1-4): "
if "%ACTION%"=="1" ( set "SCOPE=truth" & goto EXPORT )
if "%ACTION%"=="2" ( set "SCOPE=full" & goto EXPORT )
if "%ACTION%"=="3" goto IMPORT
if "%ACTION%"=="4" goto QUIT
goto MENU


REM =====================================================
REM                       EXPORT
REM =====================================================
:EXPORT
cls
echo ============ EXPORT A VOLUME (%SCOPE%) ============
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
    echo If a build job is running, files may change during the export.
    echo Safest: stop the stack first ^(docker compose down^).
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

:EXPORT_RUN
if /i not "%SCOPE%"=="truth" if /i not "%SCOPE%"=="full" (
    echo [ERROR] Scope must be "truth" or "full", got "%SCOPE%".
    goto FAIL
)
docker volume inspect "%VOL%" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Volume "%VOL%" does not exist.
    goto FAIL
)
if "%OUTDIR:~-1%"=="\" set "OUTDIR=%OUTDIR:~0,-1%"
if not exist "%OUTDIR%\" (
    echo [ERROR] Folder "%OUTDIR%" does not exist.
    goto FAIL
)

for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%t"
set "FILE=%VOL%_%SCOPE%_%TS%.tar"

echo.
echo Measuring what will be exported (%SCOPE%)...
set "NEED_MB="
for /f %%s in ('docker run --rm -v "%VOL%:/source:ro" -v "%HELPER%:/dvt.sh:ro" %IMG% sh /dvt.sh size %SCOPE%') do set "NEED_MB=%%s"
if not defined NEED_MB (
    echo [ERROR] Could not measure the volume.
    goto FAIL
)
set /a NEED_GB=NEED_MB/1024

REM ---- Free space on the destination drive (MB), checked BEFORE writing ----
set "FREE_MB="
for /f %%s in ('powershell -NoProfile -Command "try{[int64]((New-Object IO.DriveInfo([IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $env:OUTDIR).ProviderPath))).AvailableFreeSpace/1MB)}catch{}"') do set "FREE_MB=%%s"
if defined FREE_MB (
    set /a FREE_GB=FREE_MB/1024
    set /a REQ_MB=NEED_MB+1024
    echo Size to export: !NEED_MB! MB ^(~!NEED_GB! GB^)    Free on destination drive: !FREE_GB! GB
    if !REQ_MB! GTR !FREE_MB! (
        echo.
        echo [ERROR] Not enough free space on the destination drive.
        echo         Need about !NEED_GB! GB + 1 GB margin, only !FREE_GB! GB free.
        echo         Free some space or pick another drive. Nothing was written.
        goto FAIL
    )
) else (
    echo Size to export: !NEED_MB! MB ^(~!NEED_GB! GB^)    [free space could not be checked]
)

echo.
echo Exporting "%VOL%" [%SCOPE%] to "%OUTDIR%\%FILE%" ...
docker run --rm -v "%VOL%:/source:ro" -v "%OUTDIR%:/backup" -v "%HELPER%:/dvt.sh:ro" %IMG% sh /dvt.sh export %SCOPE% "%FILE%" %NEED_MB%
if errorlevel 1 (
    echo [ERROR] Export failed. No partial file was left behind.
    goto FAIL
)
echo [OK] Export complete: %OUTDIR%\%FILE%
if /i "%SCOPE%"=="truth" (
    echo      tiles/ was not included - the platform rebuilds them from the DEMs
    echo      by itself when the stack starts on the target machine.
)
goto DONE


REM =====================================================
REM                       IMPORT
REM =====================================================
:IMPORT
cls
echo ============ IMPORT A VOLUME ============
echo.
echo Select the backup file (.tar or .tar.gz) to import...
set "INFILE="
for /f "delims=" %%f in ('powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.OpenFileDialog; $d.Title='Select the volume backup to import'; $d.Filter='Volume backups (*.tar;*.tar.gz)'+[char]124+'*.tar;*.tar.gz'+[char]124+'All files (*.*)'+[char]124+'*.*'; if($d.ShowDialog() -eq 'OK'){$d.FileName}"') do set "INFILE=%%f"
if not defined INFILE (
    echo No file selected.
    pause
    goto MENU
)
set "TARGET="
set "KEEPTILES=wipe"

:IMPORT_RUN
if not exist "%INFILE%" (
    echo [ERROR] File "%INFILE%" not found.
    goto FAIL
)
for %%A in ("%INFILE%") do (
    set "INDIR=%%~dpA"
    set "INNAME=%%~nxA"
)
set "INDIR=%INDIR:~0,-1%"

set "BSCOPE=full"
echo "%INNAME%" | findstr /i /c:"_truth_" >nul && set "BSCOPE=truth"

if defined BATCH goto IMPORT_TARGET_SET
REM ---- Suggest the original volume name (strip _truth/_full + timestamp) ----
set "DEFNAME="
for /f "delims=" %%n in ('powershell -NoProfile -Command "$env:INNAME -replace '_(?:truth_)?(?:full_)?\d{8}_\d{6}\.tar(?:\.gz)?$','' -replace '\.tar(?:\.gz)?$',''"') do set "DEFNAME=%%n"
echo.
echo Selected file: %INFILE%   [%BSCOPE% backup]
echo.
echo Existing volumes on this machine:
docker volume ls -q
echo.
echo TIP: keep the original name so docker compose finds the volume.
set "TARGET=%DEFNAME%"
set /p "TARGET=Volume name to restore into [%DEFNAME%]: "

:IMPORT_TARGET_SET
if not defined TARGET (
    echo [ERROR] No volume name given.
    goto FAIL
)

REM ---- Refuse if a running container is using the volume ----
set "INUSE="
for /f %%c in ('docker ps -q --filter "volume=%TARGET%"') do set "INUSE=1"
if defined INUSE (
    echo.
    echo [ERROR] Volume "%TARGET%" is used by a running container.
    echo Stop it first ^(docker compose down^) and run the import again.
    goto FAIL
)

REM ---- Create the volume, or confirm overwrite if it exists ----
docker volume inspect "%TARGET%" >nul 2>&1
if errorlevel 1 (
    docker volume create "%TARGET%" >nul
    goto IMPORT_CHECK
)
if defined BATCH goto IMPORT_CHECK
echo.
echo [WARNING] Volume "%TARGET%" already exists.
if /i "%BSCOPE%"=="truth" (
    echo This is a TRUTH backup ^(no tiles^). Everything except tiles/ will be
    echo DELETED and replaced by the backup.
    set "GO="
    set /p "GO=Also delete the existing tiles/ in the volume? (Y/N): "
    if /i not "!GO!"=="Y" set "KEEPTILES=keep"
) else (
    echo Its current contents will be DELETED and replaced by the backup.
)
set "GO="
set /p "GO=Continue? (Y/N): "
if /i not "%GO%"=="Y" goto MENU

:IMPORT_CHECK
set "ARCH_MB="
set "AVAIL_MB="
for /f "tokens=1,2" %%a in ('docker run --rm -v "%TARGET%:/target:ro" -v "%INDIR%:/backup:ro" -v "%HELPER%:/dvt.sh:ro" %IMG% sh /dvt.sh importcheck "/backup/%INNAME%"') do (
    set "ARCH_MB=%%a"
    set "AVAIL_MB=%%b"
)
if defined ARCH_MB if defined AVAIL_MB (
    set /a ARCH_GB=ARCH_MB/1024
    set /a AVAIL_GB=AVAIL_MB/1024
    echo.
    echo Backup size: !ARCH_MB! MB ^(~!ARCH_GB! GB^)    Free space in Docker's disk: !AVAIL_GB! GB
    if !ARCH_MB! GTR !AVAIL_MB! (
        echo [WARNING] The backup is larger than the free space in Docker's disk.
        echo           It may still fit once the old volume contents are deleted.
        if not defined BATCH (
            set "GO="
            set /p "GO=Continue anyway? (Y/N): "
            if /i not "!GO!"=="Y" goto MENU
        )
    )
)

echo.
echo Importing "%INNAME%" into volume "%TARGET%" ...
docker run --rm -v "%TARGET%:/target" -v "%INDIR%:/backup:ro" -v "%HELPER%:/dvt.sh:ro" %IMG% sh /dvt.sh import "/backup/%INNAME%" %KEEPTILES%
if errorlevel 1 (
    echo [ERROR] Import failed.
    goto FAIL
)
echo [OK] Import complete. Volume "%TARGET%" is ready.
echo.
echo NEXT: start the stack ^(docker compose up -d, or Run.bat^). On start-up the
echo       platform checks every saved area and builds the tiles that are
echo       missing or were made by an older engine ^(a TRUTH backup has none^).
echo       The DEMs are in the volume, so nothing is downloaded again.
echo       Progress: the viewer shows a banner; details on the fetch page ^(/fetch/^)
echo       or GET /api/tiles/status. A whole country takes about an hour.
goto DONE


:FAIL
if defined BATCH exit /b 1
echo.
pause
goto MENU

:DONE
if defined BATCH exit /b 0
echo.
pause
goto MENU

:QUIT
del "%HELPER%" >nul 2>&1
exit /b 0


REM ---------------------------------------------------------------------
REM  Everything below the marker is a POSIX sh script. The .bat extracts
REM  it to %TEMP% and mounts it into an alpine container as /dvt.sh.
REM  cmd.exe never runs these lines (every path above exits first).
REM ---------------------------------------------------------------------
#__DVT_HELPER__
# docker-volume-tool helper, runs inside alpine:  sh /dvt.sh CMD ...
#   size        SCOPE                 -> MB that will be archived from /source
#   export      SCOPE FILE TOTAL_MB   -> /source => /backup/FILE (via .partial)
#   importcheck ARCHIVE               -> "ARCHIVE_MB FREE_MB" for /target
#   import      ARCHIVE keep|wipe     -> ARCHIVE => /target
set -u

hms() { printf '%02d:%02d:%02d' $(($1/3600)) $(($1%3600/60)) $(($1%60)); }

# Top-level entries of /source to archive, one "./name" per line.
# truth = everything except tiles/ (derived, rebuildable).
entries() {
    cd /source || exit 1
    for e in * .[!.]* ..?*; do
        [ -e "$e" ] || continue
        if [ "$1" = truth ] && [ "$e" = tiles ]; then continue; fi
        printf './%s\n' "$e"
    done
}

# Progress line every 2 s. kind=file: size of a growing file;
# kind=fd: read offset of fd 0 of a PID (bytes consumed from the archive).
watch() {
    kind=$1 src=$2 total=$3 t0=$(date +%s)
    while true; do
        sleep 2
        if [ "$kind" = file ]; then
            b=$(stat -c %s "$src" 2>/dev/null)
        else
            b=$(sed -n 's/^pos:[[:space:]]*//p' "/proc/$src/fdinfo/0" 2>/dev/null)
        fi
        [ -n "$b" ] || continue
        mb=$((b/1048576))
        el=$(($(date +%s)-t0)); [ "$el" -gt 0 ] || el=1
        rate=$((mb/el)); pct=0; eta='--:--:--'
        if [ "$total" -gt 0 ]; then
            pct=$((mb*100/total)); [ "$pct" -le 99 ] || pct=99
            if [ "$rate" -gt 0 ] && [ "$mb" -lt "$total" ]; then eta=$(hms $(((total-mb)/rate))); fi
        fi
        printf '\r  %s / %s MB  %3d%%  %d MB/s  elapsed %s  ETA %s    ' \
            "$mb" "$total" "$pct" "$rate" "$(hms "$el")" "$eta"
    done
}

do_size() {
    entries "$1" > /tmp/list
    tot=0
    while IFS= read -r e; do
        m=$(du -sm "$e" | cut -f1); tot=$((tot+m))
    done < /tmp/list
    echo "$tot"
}

do_export() {
    scope=$1 file=$2 total=$3
    entries "$scope" > /tmp/list
    [ -s /tmp/list ] || { echo "[ERROR] Nothing to export (volume is empty)."; return 1; }
    echo "  Including: $(tr '\n' ' ' < /tmp/list)"
    out="/backup/$file"; part="$out.partial"; t0=$(date +%s)
    rm -f "$part"
    tar cf "$part" -C /source -T /tmp/list & tpid=$!
    watch file "$part" "$total" & wpid=$!
    trap 'kill $tpid $wpid 2>/dev/null; rm -f "$part"; echo; echo "[ABORTED] partial file removed"; exit 130' INT TERM
    wait $tpid; rc=$?
    kill $wpid 2>/dev/null; wait $wpid 2>/dev/null
    echo
    if [ "$rc" -ne 0 ]; then
        rm -f "$part"
        echo "[ERROR] tar failed (exit $rc). Disk full? The partial file was removed."
        return "$rc"
    fi
    mv -f "$part" "$out" || { rm -f "$part"; return 1; }
    echo "  Wrote $(($(stat -c %s "$out")/1048576)) MB in $(hms $(($(date +%s)-t0)))"
}

do_importcheck() {
    [ -f "$1" ] || { echo "0 0"; return 1; }
    echo "$(($(stat -c %s "$1")/1048576)) $(df -Pm /target | awk 'NR==2{print $4}')"
}

do_import() {
    arch=$1 keep=$2 t0=$(date +%s)
    [ -f "$arch" ] || { echo "[ERROR] Archive not found: $arch"; return 1; }
    total=$(($(stat -c %s "$arch")/1048576))
    if [ "$keep" = keep ]; then
        echo "  Removing old contents (keeping tiles/)..."
        find /target -mindepth 1 -maxdepth 1 ! -name tiles -exec rm -rf {} +
    else
        echo "  Removing old contents..."
        find /target -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    fi
    case "$arch" in
        *.gz|*.tgz) reader='gzip -dc' ;;
        *)          reader='cat' ;;
    esac
    mkfifo /tmp/stream
    $reader < "$arch" > /tmp/stream & rpid=$!
    tar xf /tmp/stream -C /target & tpid=$!
    watch fd "$rpid" "$total" & wpid=$!
    trap 'kill $rpid $tpid $wpid 2>/dev/null; echo; echo "[ABORTED] volume is incomplete - import again"; exit 130' INT TERM
    wait $tpid; trc=$?
    wait $rpid; rrc=$?
    kill $wpid 2>/dev/null; wait $wpid 2>/dev/null
    echo
    if [ "$trc" -ne 0 ] || [ "$rrc" -ne 0 ]; then
        echo "[ERROR] Extract failed (tar=$trc reader=$rrc). The volume is incomplete."
        return 1
    fi
    echo "  Restored $total MB in $(hms $(($(date +%s)-t0)))"
}

cmd=$1; shift
case "$cmd" in
    size)        do_size "$@" ;;
    export)      do_export "$@" ;;
    importcheck) do_importcheck "$@" ;;
    import)      do_import "$@" ;;
    *)           echo "unknown command: $cmd"; exit 2 ;;
esac
