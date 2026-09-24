@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title TorrServer Lite
echo.
echo  TorrServer Lite
echo  ----------------
echo  First run may download Python and ffmpeg.
echo  Internet required for that.
echo.

set "ROOT=%~dp0"
set "PY="

if exist "%ROOT%tools\python\python.exe" set "PY=%ROOT%tools\python\python.exe"

if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%I in ('where python') do (
      set "PY=%%I"
      goto got_py
    )
  )
)
:got_py

if not defined PY (
  echo [*] Portable Python not found - downloading...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%ipad\bootstrap_python.ps1"
  if exist "%ROOT%tools\python\python.exe" set "PY=%ROOT%tools\python\python.exe"
)

if not defined PY (
  echo [!] Python not found.
  echo     Install from https://www.python.org/downloads/
  echo     Enable "Add python.exe to PATH", then run start.bat again.
  pause
  exit /b 1
)

echo [*] Python: %PY%

if not exist "%ROOT%TorrServer-windows-amd64.exe" (
  echo [!] Missing TorrServer-windows-amd64.exe in this folder.
  pause
  exit /b 1
)

echo [*] Checking ffmpeg...
"%PY%" "%ROOT%ipad\ensure_ffmpeg.py"
if errorlevel 1 (
  echo [!] ffmpeg not ready. Need internet or C:\ffmpeg\bin\ffmpeg.exe
  echo     Continuing anyway. MatriX may work; iPad HLS may not.
)

echo [*] Starting...
set "PYW=%PY%"
if /I "%PY:~-10%"=="python.exe" (
  set "PYW=%PY:~0,-10%pythonw.exe"
)
if not exist "%PYW%" set "PYW=%PY%"

start "" "%PYW%" "%ROOT%films.pyw"
echo.
echo  Done. Tray icon. Browser: http://127.0.0.1:8080/
echo  iPad same Wi-Fi: http://PC_IP:8080/
echo.
timeout /t 4 >nul
exit /b 0
