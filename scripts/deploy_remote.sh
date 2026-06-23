#!/bin/bash
# Серверная часть деплоя: распаковать тарбол, разложить файлы, прогнать миграции,
# поставить таймер, рестартнуть API. Идемпотентно (безопасно при ретраях).
set -e
APP=/opt/horizon/personal_budget/horizon-api
T=/tmp/hzunpack
rm -rf "$T"; mkdir -p "$T"
tar xzf /tmp/hzbuild.tgz -C "$T"
SRC="$T/personal_budget/horizon-api"

# ── frontend ──
mv -f "$T/releases/budget-v2.html" "$APP/static/index.html"
cp -f "$T/chart.js" "$APP/static/"

# ── backend ──
cp -f "$SRC/forecast_cron.py" "$SRC/metrics_core.py" "$SRC/plan_materialize.py" \
      "$SRC/main.py" "$SRC/middleware.py" "$APP/"
cp -f "$SRC/routers/"*.py "$APP/routers/"
mkdir -p "$APP/migrations" "$APP/deploy"
cp -f "$SRC/migrations/"*.sql "$APP/migrations/"
cp -f "$SRC/deploy/"* "$APP/deploy/" 2>/dev/null || true

# ── DB-миграции (ошибка помечает деплой красным, но НЕ блокирует рестарт) ──
MIGFAIL=0
ENV_FILE=$(find /opt/horizon -maxdepth 3 -name '.env' | head -1)
if [ -n "$ENV_FILE" ]; then
  DB_URL=$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
  if [ -n "$DB_URL" ]; then
    for f in "$APP/migrations/"*.sql; do
      echo "migrate: $f"
      if ! psql "$DB_URL" -v ON_ERROR_STOP=1 -f "$f"; then echo "!!! MIGRATION FAILED: $f"; MIGFAIL=1; fi
    done
    echo "migrations done (fail=$MIGFAIL)"
  else echo "DATABASE_URL not set; skip migrations"; fi
else echo "no .env; skip migrations"; fi

# ── systemd-таймер снапшота ──
EXEC=$(systemctl show -p ExecStart --value horizon | sed -nE 's/.*path=([^ ;]+).*/\1/p')
BINDIR=$(dirname "$EXEC")
PY="$BINDIR/python"; [ -x "$PY" ] || PY="$BINDIR/python3"; [ -x "$PY" ] || PY=$(command -v python3)
sed "s#^ExecStart=.*#ExecStart=$PY $APP/forecast_cron.py#" \
  "$APP/deploy/horizon-forecast.service" > /etc/systemd/system/horizon-forecast.service
cp -f "$APP/deploy/horizon-forecast.timer" /etc/systemd/system/horizon-forecast.timer
systemctl daemon-reload
systemctl enable --now horizon-forecast.timer

# ── рестарт API ──
systemctl restart horizon
rm -rf "$T" /tmp/hzbuild.tgz
echo "deploy done"
[ "$MIGFAIL" = 1 ] && { echo "deploy completed, but a migration failed (see above)"; exit 1; } || true
