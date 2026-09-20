@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title JEV Disk Health Check

echo ============================================================
echo   JEV Disk Health Check  (read-only, deletes nothing)
echo   This window IS the service. Close it = service stops.
echo ============================================================
echo.

set "PY="
where python >nul 2>nul
if %errorlevel%==0 set "PY=python"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "C:\Python313\python.exe" set "PY=C:\Python313\python.exe"
if not defined PY if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"
if not defined PY (
  echo [X] Python not found. Please install Python 3.9+ and try again.
  echo.
  pause
  exit /b 1
)
echo [OK] Python: %PY%

if not defined TYPESAFE_API_KEY if exist "key.txt" set /p TYPESAFE_API_KEY=<key.txt
if defined TYPESAFE_API_KEY goto haskey

echo.
echo No API key found. Two ways to set it:
echo   1^) create a file named  key.txt  here, containing only the key
echo   2^) paste it below - it will be saved to key.txt for next time
echo.
set /p TYPESAFE_API_KEY=Paste API key and press Enter: 
if not defined TYPESAFE_API_KEY (
  echo [X] No key entered. Cannot call the judging API.
  echo.
  pause
  exit /b 1
)
> "key.txt" echo %TYPESAFE_API_KEY%
echo [OK] saved to key.txt

:haskey
echo [OK] API key ready
echo.
echo Starting... browser will open automatically.
echo Press Ctrl+C or close this window to stop. Idle shutdown is on by default.
echo ------------------------------------------------------------
set PYTHONIOENCODING=utf-8
"%PY%" server.py %*
set "RC=%ERRORLEVEL%"
echo ------------------------------------------------------------
echo Service exited. You can close this window now.
echo.
pause
