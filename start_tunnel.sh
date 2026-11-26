#!/usr/bin/env bash
set -euo pipefail

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "[!] Утилита cloudflared не установлена."
  echo "    Установка на Debian/Ubuntu:"
  echo "      curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb && sudo dpkg -i /tmp/cloudflared.deb"
  exit 1
fi

echo "Запускаю публичный HTTPS-туннель на http://localhost:8000 ..."
echo "Скопируйте выданный адрес https://....trycloudflare.com и откройте на электронной доске!"
echo
exec cloudflared tunnel --url http://localhost:8000
