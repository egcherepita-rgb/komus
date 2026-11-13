#!/usr/bin/env bash
# Run from the cloned repository. Adds only this project's container and vhost.
set -euo pipefail
DOMAIN="${1:-}"
KOMUS_LISTEN_PORT="${2:-8093}"
if [[ $EUID -ne 0 ]]; then echo 'Запустите через sudo.' >&2; exit 1; fi
if [[ ! "$DOMAIN" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]] || [[ "$DOMAIN" != *.* ]]; then echo 'Укажите домен: sudo bash deploy/install.sh komus.example.ru [порт]' >&2; exit 1; fi
if [[ ! "$KOMUS_LISTEN_PORT" =~ ^[0-9]+$ ]] || (( KOMUS_LISTEN_PORT < 1024 || KOMUS_LISTEN_PORT > 65535 )); then echo 'Недопустимый порт' >&2; exit 1; fi
for program in docker nginx curl ss openssl; do command -v "$program" >/dev/null || { echo "Не установлен $program" >&2; exit 1; }; done
docker compose version >/dev/null
nginx -t
REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
[[ -f compose.yaml ]] || exit 1
CONF="/etc/nginx/sites-available/promet-komus.conf"
LINK="/etc/nginx/sites-enabled/promet-komus.conf"
if [[ ! -d /etc/nginx/sites-available || ! -d /etc/nginx/sites-enabled ]]; then echo 'Нужна структура Nginx sites-available / sites-enabled; см. README для ручного подключения.' >&2; exit 1; fi
if [[ -e "$CONF" || -e "$LINK" || -L "$LINK" ]]; then echo 'Конфигурация promet-komus уже существует. Для обновления используйте deploy/update.sh.' >&2; exit 1; fi
if ss -H -ltn | awk '{print $4}' | grep -Eq ":${KOMUS_LISTEN_PORT}$"; then echo "Порт $KOMUS_LISTEN_PORT занят. Укажите другой порт вторым аргументом." >&2; exit 1; fi
if nginx -T 2>&1 | grep -Fq "server_name $DOMAIN"; then echo 'Домен уже встречается в Nginx. Проверьте конфигурацию вручную.' >&2; exit 1; fi
if [[ -e .env ]]; then echo '.env уже существует. Проверьте его и запустите Docker / Nginx вручную по README.' >&2; exit 1; fi
umask 077
KOMUS_INITIAL_PASSWORD="$(openssl rand -hex 18)"
printf 'APP_PASSWORD=%s\nKOMUS_PORT=%s\nCOOKIE_SECURE=0\n' "$KOMUS_INITIAL_PASSWORD" "$KOMUS_LISTEN_PORT" > .env
docker compose up -d --build
KOMUS_READY=0
for attempt in {1..30}; do if curl -fsS "http://127.0.0.1:$KOMUS_LISTEN_PORT/health" >/dev/null; then KOMUS_READY=1; break; fi; sleep 2; done
if [[ "$KOMUS_READY" != 1 ]]; then echo 'Приложение не ответило. Nginx не изменён. Проверьте docker compose logs.' >&2; exit 1; fi
cat > "$CONF" <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 16m;
    location / {
        proxy_pass http://127.0.0.1:$KOMUS_LISTEN_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
EOF
ln -s "$CONF" "$LINK"
if ! nginx -t; then rm "$LINK" "$CONF"; echo 'Проверка Nginx не пройдена. Добавленная конфигурация отменена.' >&2; exit 1; fi
systemctl reload nginx
printf '\nПриложение: http://%s\nПароль: %s\n' "$DOMAIN" "$KOMUS_INITIAL_PASSWORD"
printf 'После настройки DNS включите HTTPS: certbot --nginx -d %s\n' "$DOMAIN"
printf 'Затем установите COOKIE_SECURE=1 в .env и выполните docker compose up -d\n'
unset KOMUS_INITIAL_PASSWORD
