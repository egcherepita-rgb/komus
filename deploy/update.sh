#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ -f .env ]] || { echo 'Сначала настройте .env' >&2; exit 1; }
# Backup includes SQLite WAL-consistent contents; never committed to Git.
mkdir -p backups
chmod 700 backups
KOMUS_BACKUP_NAME="backups/komus-$(date +%Y%m%d-%H%M%S).db"
umask 077
docker compose exec -T app python -c "import sqlite3,sys; a=sqlite3.connect('/data/komus.db'); b=sqlite3.connect('/tmp/komus-backup.db'); a.backup(b); b.close(); sys.stdout.buffer.write(open('/tmp/komus-backup.db','rb').read())" > "$KOMUS_BACKUP_NAME"
git pull --ff-only
docker compose up -d --build
printf 'Обновление запущено. Резервная копия: %s\n' "$KOMUS_BACKUP_NAME"
docker compose ps
