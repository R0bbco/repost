@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "ENV=%LOCALAPPDATA%\RepostApp\venv"

where python >nul 2>nul
if errorlevel 1 (
  echo Python не найден. Установи его с python.org и поставь галочку "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist "%ENV%\Scripts\python.exe" (
  echo Первый запуск: скачиваю компоненты. Это займёт 5-10 минут, не закрывай окно.
  python -m venv "%ENV%"
  if errorlevel 1 goto fail
  "%ENV%\Scripts\python.exe" -m pip install --upgrade pip -q
)

rem Ставим компоненты заново, только если список изменился (после обновления программы)
fc /b requirements.txt "%ENV%\requirements.installed" >nul 2>nul
if errorlevel 1 (
  echo Устанавливаю компоненты...
  "%ENV%\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto fail
  copy /y requirements.txt "%ENV%\requirements.installed" >nul
)

rem Запускаем программу без чёрного окна; этот файл сразу завершается,
rem поэтому его можно спокойно обновлять
start "" "%ENV%\Scripts\pythonw.exe" repost_app.py
exit /b 0

:fail
echo.
echo Установка не удалась. Проверь интернет и запусти start.bat ещё раз.
pause
exit /b 1
