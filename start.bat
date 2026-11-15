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

if exist "%ENV%\Scripts\python.exe" goto run

echo Первый запуск: скачиваю компоненты. Это займёт 5-10 минут, не закрывай окно.
python -m venv "%ENV%"
if errorlevel 1 goto fail
"%ENV%\Scripts\python.exe" -m pip install --upgrade pip -q
"%ENV%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail

:run
"%ENV%\Scripts\python.exe" repost_app.py
if errorlevel 1 pause
exit /b 0

:fail
echo.
echo Установка не удалась. Проверь интернет и запусти start.bat ещё раз.
rmdir /s /q "%ENV%" 2>nul
pause
exit /b 1
