@echo off
chcp 65001 >nul
cd /d "%~dp0"
title TorrServer Lite
echo.
echo  TorrServer Lite
echo  ----------------
echo  Первый запуск может скачать Python и ffmpeg (нужен интернет).
echo.

set "ROOT=%~dp0"
set "PY="

if exist "%ROOT%tools\python\python.exe" set "PY=%ROOT%tools\python\python.exe"

if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%I in ('where python') do (
      set "PY=%%I"
      goto :got_py
    )
  )
)
:got_py

if not defined PY (
  echo [*] Portable Python не найден — скачиваю...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%ipad\bootstrap_python.ps1"
  if exist "%ROOT%tools\python\python.exe" set "PY=%ROOT%tools\python\python.exe"
)

if not defined PY (
  echo [!] Нет Python.
  echo     Установите с https://www.python.org/downloads/
  echo     ^(отметьте "Add python.exe to PATH"^) и запустите start.bat снова.
  pause
  exit /b 1
)

echo [*] Python: %PY%

if not exist "%ROOT%TorrServer-windows-amd64.exe" (
  echo [!] Нет TorrServer-windows-amd64.exe в этой папке.
  pause
  exit /b 1
)

echo [*] Проверяю ffmpeg...
"%PY%" "%ROOT%ipad\ensure_ffmpeg.py"
if errorlevel 1 (
  echo [!] ffmpeg не готов. Нужен интернет или C:\ffmpeg\bin\ffmpeg.exe
  echo     Продолжаю запуск — MatriX может работать, HLS на iPad — нет.
)

echo [*] Запуск...
set "PYW=%PY%"
if /I "%PY:~-10%"=="python.exe" (
  set "PYW=%PY:~0,-10%pythonw.exe"
)
if not exist "%PYW%" set "PYW=%PY%"

start "" "%PYW%" "%ROOT%films.pyw"
echo.
echo  Готово. Иконка в трее. Браузер: http://127.0.0.1:8080/
echo  iPad в той же Wi‑Fi: http://ВАШ_IP:8080/
echo.
timeout /t 4 >nul
exit /b 0
