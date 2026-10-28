@echo off
rem Build the Windows `lerc1enc.exe` (Lerc1 "CntZImage" encoder) with MSVC.
rem Source: GDAL frmts/mrf/LERCV1 (Esri Lerc1, Apache-2.0) - see lercv1\NOTICE
rem Run from a "x64 Native Tools Command Prompt for VS" (cl.exe on PATH), or
rem this script tries to locate one with vswhere. POSIX: build_lerc1enc.sh
setlocal
cd /d "%~dp0"

where cl >nul 2>nul
if not errorlevel 1 goto build

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" goto nocl
set "VSDIR="
for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSDIR=%%i"
if not defined VSDIR goto nocl
if not exist "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" goto nocl
call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul
where cl >nul 2>nul
if errorlevel 1 goto nocl

:build
cl /nologo /O2 /EHsc /std:c++17 /I lercv1 lerc1enc.cpp lercv1\Lerc1Image.cpp /Fe:lerc1enc.exe
if errorlevel 1 (
    echo lerc1enc build FAILED 1>&2
    exit /b 1
)
del /q lerc1enc.obj Lerc1Image.obj 2>nul
echo built %CD%\lerc1enc.exe
exit /b 0

:nocl
echo cl.exe not found - open a "x64 Native Tools Command Prompt for VS" or install the Visual Studio C++ build tools. 1>&2
exit /b 1
