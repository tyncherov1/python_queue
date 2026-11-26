#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "=================================================="
echo "  Очередь на семинар — запуск бота и веб-доски"
echo "=================================================="
echo

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] Создаю виртуальное окружение .venv..."
  python3 -m venv .venv
  echo "[2/3] Устанавливаю зависимости из requirements.txt..."
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi

if [ ! -f ".env" ]; then
  echo "[!] Файл .env не найден — создаю шаблон из .env.example..."
  cp .env.example .env
  echo "    Откройте .env (например: nano .env) и впишите BOT_TOKEN и ADMIN_IDS."
  echo "    (Сейчас сайт всё равно запустится в демо-режиме на http://localhost:8000)"
  echo
fi

echo "[3/3] Запускаю сервер (http://0.0.0.0:8000)..."
echo "      Для остановки нажмите Ctrl+C"
echo
exec .venv/bin/python run.py
