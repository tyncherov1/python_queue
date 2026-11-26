@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ==================================================
echo   Очередь на семинар — запуск бота и веб-доски
echo ==================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Создаю виртуальное окружение .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo [ОШИБКА] Python не найден. Установите Python 3.10+ с python.org
        pause
        exit /b 1
    )
    echo [2/3] Устанавливаю зависимости из requirements.txt...
    .venv\Scripts\pip.exe install -r requirements.txt
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось установить зависимости. Проверьте интернет.
        pause
        exit /b 1
    )
)

if not exist ".env" (
    echo [!] Файл .env не найден — создаю шаблон из .env.example...
    copy /Y ".env.example" ".env" >nul
    echo     Откройте .env и впишите BOT_TOKEN и ADMIN_IDS, затем перезапустите start.bat!
    echo     (Сейчас сайт всё равно запустится в демо-режиме на http://localhost:8000)
    echo.
)

echo [3/3] Запускаю сервер...
echo       Локальная доска: http://localhost:8000
echo       Для остановки нажмите Ctrl+C
echo.
.venv\Scripts\python.exe run.py
pause
