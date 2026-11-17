#!/usr/bin/env bash
# Updates only the existing promet-komus application; preserves its volume and Nginx.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ -f .env ]] || { echo 'Не найдена .env. Обновление остановлено.' >&2; exit 1; }
git diff --quiet && git diff --cached --quiet || { echo 'Есть локальные изменения исходников. Сначала сохраните их отдельно.' >&2; exit 1; }
mkdir -p backups
chmod 700 backups
KOMUS_BACKUP_NAME="backups/komus-$(date +%Y%m%d-%H%M%S).db"
umask 077
docker compose -p promet-komus exec -T app python -c "import sqlite3,sys; a=sqlite3.connect('/data/komus.db'); b=sqlite3.connect('/tmp/komus-backup.db'); a.backup(b); b.close(); sys.stdout.buffer.write(open('/tmp/komus-backup.db','rb').read())" > "$KOMUS_BACKUP_NAME"
[[ -s "$KOMUS_BACKUP_NAME" ]] || { echo 'Резервная копия не создана. Обновление остановлено.' >&2; exit 1; }
git pull --ff-only
# The previous container keeps serving while this application's new image builds.
docker compose -p promet-komus build app
docker compose -p promet-komus up -d --no-deps app
KOMUS_HTTP_ADDRESS="$(docker compose -p promet-komus port app 8080)"
curl -fsS --retry 12 --retry-delay 2 --retry-connrefused "http://$KOMUS_HTTP_ADDRESS/health"
printf '\nОбновление проверено. Резервная копия: %s\n' "$KOMUS_BACKUP_NAME"
docker compose -p promet-komus ps
