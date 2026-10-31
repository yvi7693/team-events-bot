#!/usr/bin/env bash
# Временный HTTPS-туннель через localhost.run для проверки Mini App без сервера.
# Бесплатный localhost.run меняет домен на ходу — скрипт записывает актуальный адрес
# в data/public_url.txt, а бот (с PUBLIC_URL_FILE=data/public_url.txt) подхватывает его сам.
# Если соединение рвётся — переподключается.
set -u
cd "$(dirname "$0")/.."
mkdir -p data
PORT="${WEB_PORT:-8080}"
while true; do
  ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
      -o ExitOnForwardFailure=yes -R "80:localhost:${PORT}" nokey@localhost.run 2>&1 |
  while IFS= read -r line; do
    if url=$(grep -oE 'https://[a-z0-9-]+\.lhr\.life' <<<"$line"); then
      echo "$url" > data/public_url.txt
      echo "$(date '+%F %T') tunnel: $url"
    fi
  done
  echo "$(date '+%F %T') tunnel closed, reconnecting in 5s"
  sleep 5
done
