#!/usr/bin/env bash
# Updates only the existing promet-komus application; preserves its volume and Nginx.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
case "${1:-}" in
  '') KOMUS_RECOVER=false ;;
  --recover) KOMUS_RECOVER=true ;;
  *) echo 'Использование: bash deploy/update.sh [--recover]' >&2; exit 2 ;;
esac
[[ -f .env ]] || { echo 'Не найдена .env. Обновление остановлено.' >&2; exit 1; }
git diff --quiet && git diff --cached --quiet || { echo 'Есть локальные изменения исходников. Сначала сохраните их отдельно.' >&2; exit 1; }
mkdir -p backups
chmod 700 backups
KOMUS_BACKUP_NAME="backups/komus-$(date +%Y%m%d-%H%M%S).db"
KOMUS_BACKUP_TEMP="$(mktemp backups/.komus-backup-XXXXXX)"
trap 'rm -f -- "$KOMUS_BACKUP_TEMP"' EXIT
KOMUS_BACKUP_CODE='
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

with TemporaryDirectory(dir="/tmp") as directory:
    snapshot = Path(directory) / "komus.db"
    with closing(sqlite3.connect("file:/data/komus.db?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                sys.exit("Проверка резервной копии не прошла")
    sys.stdout.buffer.write(snapshot.read_bytes())
'
if "$KOMUS_RECOVER"; then
    # Make the backup in a separate container using the existing data volume.
    git pull --ff-only
    docker compose -p promet-komus build app
    docker compose -p promet-komus run --rm --no-deps -T --entrypoint python app -c "$KOMUS_BACKUP_CODE" > "$KOMUS_BACKUP_TEMP"
else
    docker compose -p promet-komus exec -T app python -c "$KOMUS_BACKUP_CODE" > "$KOMUS_BACKUP_TEMP"
fi
[[ -s "$KOMUS_BACKUP_TEMP" ]] || { echo 'Резервная копия не создана. Обновление остановлено.' >&2; exit 1; }
mv -- "$KOMUS_BACKUP_TEMP" "$KOMUS_BACKUP_NAME"
if ! "$KOMUS_RECOVER"; then
    git pull --ff-only
    docker compose -p promet-komus build app
fi
docker compose -p promet-komus up -d --no-deps app
KOMUS_HEALTH_ERROR=''
for (( attempt=1; attempt<=15; attempt++ )); do
    if KOMUS_HTTP_ADDRESS="$(docker compose -p promet-komus port app 8080 2>&1)"; then
        if KOMUS_HEALTH_RESULT="$(curl -fsS --max-time 2 "http://$KOMUS_HTTP_ADDRESS/health" 2>&1)"; then
            printf '%s\nОбновление проверено. Резервная копия: %s\n' "$KOMUS_HEALTH_RESULT" "$KOMUS_BACKUP_NAME"
            docker compose -p promet-komus ps
            exit 0
        fi
        KOMUS_HEALTH_ERROR="$KOMUS_HEALTH_RESULT"
    else
        KOMUS_HEALTH_ERROR="$KOMUS_HTTP_ADDRESS"
    fi
    if (( attempt < 15 )); then sleep 2; fi
done
printf 'Приложение не ответило после запуска. Последняя ошибка: %s\nРезервная копия: %s\n' "$KOMUS_HEALTH_ERROR" "$KOMUS_BACKUP_NAME" >&2
docker compose -p promet-komus ps -a app >&2 || true
docker compose -p promet-komus logs --no-color --tail=25 app >&2 || true
exit 1
