#!/usr/bin/env bash
# First installation only. Never operates on another Compose project or vhost.
set -euo pipefail
DOMAIN="${1:-}"
KOMUS_LISTEN_PORT="${2:-8093}"
if [[ $EUID -ne 0 ]]; then echo 'Запустите через sudo.' >&2; exit 1; fi
if [[ ! "$DOMAIN" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]] || [[ "$DOMAIN" != *.* ]]; then echo 'Укажите домен: sudo bash deploy/install.sh komus.example.ru [порт]' >&2; exit 1; fi
if [[ ! "$KOMUS_LISTEN_PORT" =~ ^[0-9]{1,5}$ ]]; then echo 'Недопустимый порт' >&2; exit 1; fi
KOMUS_LISTEN_PORT=$((10#$KOMUS_LISTEN_PORT))
if (( KOMUS_LISTEN_PORT < 1024 || KOMUS_LISTEN_PORT > 65535 )); then echo 'Недопустимый порт' >&2; exit 1; fi
for program in docker nginx curl ss openssl tar awk systemctl mktemp; do command -v "$program" >/dev/null || { echo "Не установлен $program" >&2; exit 1; }; done
docker compose version >/dev/null
docker info >/dev/null
nginx -t
REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
[[ -f compose.yaml ]] || exit 1
CONF="/etc/nginx/sites-available/promet-komus.conf"
LINK="/etc/nginx/sites-enabled/promet-komus.conf"
if [[ ! -d /etc/nginx/sites-available || ! -d /etc/nginx/sites-enabled ]]; then echo 'Нужна структура Nginx sites-available / sites-enabled; см. README для ручного подключения.' >&2; exit 1; fi
if [[ -e "$CONF" || -L "$CONF" || -e "$LINK" || -L "$LINK" ]]; then echo 'Конфигурация promet-komus уже существует. Для обновления используйте deploy/update.sh.' >&2; exit 1; fi
if [[ -e .env || -L .env ]]; then echo '.env уже существует. Повторная установка остановлена; сохраните существующие настройки и данные.' >&2; exit 1; fi
# Consume complete command output. grep -q pipelines can miss a match under pipefail
# when the producer receives SIGPIPE on a large existing Nginx configuration.
KOMUS_LISTENERS="$(ss -H -ltn)"
if printf '%s\n' "$KOMUS_LISTENERS" | awk -v port="$KOMUS_LISTEN_PORT" '$4 ~ (":" port "$") { found=1 } END { exit !found }'; then
    echo "Порт $KOMUS_LISTEN_PORT занят. Укажите другой порт вторым аргументом." >&2; exit 1
fi
KOMUS_NGINX_DUMP="$(nginx -T 2>/dev/null)"
if printf '%s\n' "$KOMUS_NGINX_DUMP" | awk -v host="$DOMAIN" '
    { sub(/#.*/,""); gsub(/[;{}]/," & "); }
    { for(i=1;i<=NF;i++) {
        if($i=="server_name") { names=1; continue }
        if(names && $i==";") { names=0; continue }
        if(names && $i==host) found=1
    } }
    END { exit !found }'; then
    echo 'Домен уже настроен в Nginx. Установка остановлена для проверки конфигурации.' >&2; exit 1
fi
KOMUS_CONTAINERS="$(docker ps -a --filter label=com.docker.compose.project=promet-komus --format '{{.Names}}')"
if [[ -n "$KOMUS_CONTAINERS" ]]; then echo 'Docker-проект promet-komus уже существует. Повторная установка остановлена.' >&2; exit 1; fi
if docker volume inspect promet-komus_komus-data >/dev/null 2>&1; then echo 'Обнаружен существующий volume Комуса. Установка остановлена для сохранения данных.' >&2; exit 1; fi
if docker network inspect promet-komus_default >/dev/null 2>&1; then echo 'Обнаружена существующая Docker-сеть Комуса. Проверьте прошлую установку.' >&2; exit 1; fi
# Private backup of existing routing before creating any application resources.
umask 077
KOMUS_BACKUP_DIR="$(mktemp -d /root/komus-install-backup.XXXXXX)"
tar -czf "$KOMUS_BACKUP_DIR/nginx-before.tar.gz" -C /etc nginx
printf '%s\n' "$KOMUS_NGINX_DUMP" > "$KOMUS_BACKUP_DIR/nginx-before.txt"
docker ps --format '{{.Names}}\t{{.Ports}}\t{{.Status}}' > "$KOMUS_BACKUP_DIR/containers-before.txt"
printf 'Резервная копия конфигурации: %s\n' "$KOMUS_BACKUP_DIR"
unset KOMUS_NGINX_DUMP KOMUS_LISTENERS KOMUS_CONTAINERS
KOMUS_INITIAL_PASSWORD="$(openssl rand -hex 18)"
# Exclusive creation prevents overwriting a concurrently created .env.
(set -o noclobber; printf 'APP_PASSWORD=%s\nKOMUS_PORT=%s\nCOOKIE_SECURE=0\n' "$KOMUS_INITIAL_PASSWORD" "$KOMUS_LISTEN_PORT" > .env)
docker compose -p promet-komus up -d --build
KOMUS_READY=0
for attempt in {1..30}; do if curl -fs "http://127.0.0.1:$KOMUS_LISTEN_PORT/health" >/dev/null; then KOMUS_READY=1; break; fi; sleep 2; done
if [[ "$KOMUS_READY" != 1 ]]; then echo 'Приложение не ответило. Nginx не изменён. Проверьте docker compose -p promet-komus logs.' >&2; exit 1; fi
# Only these newly created paths are ever removed on rollback.
(set -o noclobber; cat > "$CONF" <<EOF
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
)
if ! ln -s "$CONF" "$LINK"; then rm -- "$CONF"; echo 'Не удалось создать новую ссылку Nginx. Установка остановлена.' >&2; exit 1; fi
if ! nginx -t; then rm -- "$LINK" "$CONF"; echo 'Проверка Nginx не пройдена. Только добавленная конфигурация отменена.' >&2; exit 1; fi
if ! systemctl reload nginx; then rm -- "$LINK" "$CONF"; echo 'Перечитывание Nginx не выполнено. Добавленные файлы удалены; резервная копия сохранена.' >&2; exit 1; fi
printf '\nПриложение: http://%s\n' "$DOMAIN"
printf 'Проверка с сервера: curl -f -H "Host: %s" http://127.0.0.1/health\n' "$DOMAIN"
printf 'Перед входом включите HTTPS: certbot --nginx -d %s\n' "$DOMAIN"
printf 'Затем установите COOKIE_SECURE=1 в .env и выполните docker compose -p promet-komus up -d\n'
printf 'Пароль хранится в /opt/komus/.env. Посмотрите его локально в терминале; не присылайте .env или пароль в чат.\n'
unset KOMUS_INITIAL_PASSWORD
