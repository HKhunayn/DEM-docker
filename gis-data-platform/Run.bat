@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
call Run.bat %*
exit /b %errorlevel%
