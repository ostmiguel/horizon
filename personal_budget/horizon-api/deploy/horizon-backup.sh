#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  Horizon — локальный бэкап БД: pg_dump (сжатый) + sha256 + проверка целостности
#  + ротация. Запускается systemd-таймером раз в сутки.
#
#  ⚠️  ЛОКАЛЬНЫЙ бэкап НЕ защищает от потери самого VPS, ransomware или
#      повреждения диска. Это временная мера (экономия на облаке). Как появится
#      бюджет — выгружать копии OFF-BOX (другой сервер / объектное хранилище),
#      желательно immutable. Минимум — периодически скачивать дамп к себе.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BACKUP_DIR="${HORIZON_BACKUP_DIR:-/opt/horizon/backups}"
KEEP_DAYS="${HORIZON_BACKUP_KEEP:-14}"

ENV_FILE=$(find /opt/horizon -maxdepth 3 -name .env | head -1)
[ -z "${ENV_FILE:-}" ] && { echo "no .env found"; exit 1; }
DB_URL=$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
[ -z "${DB_URL:-}" ] && { echo "DATABASE_URL not set"; exit 1; }

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"               # каталог с финданными — только владельцу

TS=$(date +%Y%m%d-%H%M%S)
FILE="$BACKUP_DIR/horizon-$TS.dump"

# custom-format дамп (сжатый, восстанавливается через pg_restore)
pg_dump "$DB_URL" --format=custom --no-owner --file="$FILE"
chmod 600 "$FILE"

# контрольная сумма (обнаружение порчи/подмены)
sha256sum "$FILE" > "$FILE.sha256"
chmod 600 "$FILE.sha256"

# проверка восстанавливаемости: pg_restore --list читает оглавление дампа;
# падение здесь = битый бэкап (по скиллу — verify recoverability).
if ! pg_restore --list "$FILE" > /dev/null 2>&1; then
  echo "!!! BACKUP CORRUPT (pg_restore --list failed): $FILE"
  exit 1
fi

# ротация по возрасту
find "$BACKUP_DIR" -maxdepth 1 -name 'horizon-*.dump'        -mtime +"$KEEP_DAYS" -delete
find "$BACKUP_DIR" -maxdepth 1 -name 'horizon-*.dump.sha256' -mtime +"$KEEP_DAYS" -delete

echo "backup ok: $FILE ($(du -h "$FILE" | cut -f1)) · keep=${KEEP_DAYS}d · total=$(ls -1 "$BACKUP_DIR"/horizon-*.dump 2>/dev/null | wc -l)"
