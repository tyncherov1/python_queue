@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ==================================================
echo   Публичный HTTPS-туннель к доске (Cloudflare)
echo ==================================================
echo.

where cloudflared >nul 2>nul
if errorlevel 1 (
    echo [!] Утилита cloudflared не установлена.
    echo     Установите её одной командой в PowerShell:
    echo       winget install --id Cloudflare.cloudflared
    echo     После установки перезапустите start_tunnel.bat
    echo.
    pause
    exit /b 1
)

echo Запускаю туннель на http://localhost:8000 ...
echo Скопируйте выданный адрес https://....trycloudflare.com и откройте на электронной доске!
echo.
cloudflared tunnel --url http://localhost:8000
pause
