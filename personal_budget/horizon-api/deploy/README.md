# Deploy / ops — Тир 1

## Что нужно сделать тебе

**Ничего вручную.** Достаточно влить изменения в `main` (запустится деплой).
При деплое автоматически:

1. применяется `migrations/000_baseline.sql` (baseline-схема ручных объектов;
   идемпотентно — прод не меняет);
2. на сервер копируется новый `forecast_cron.py` + общий `metrics_core.py`;
3. ставится и включается systemd-таймер снапшота на **23:55 ежедневно**
   (интерпретатор python подбирается автоматически из сервиса `horizon`),
   и делается один пробный прогон.

После деплоя ничего настраивать не надо.

---

## Проверка (по желанию, не обязательно)

Если захочешь убедиться, что снапшоты пишутся — на сервере:

```bash
ssh root@109.71.247.58
systemctl list-timers horizon-forecast.timer   # когда следующий запуск
ENV=$(find /opt/horizon -maxdepth 3 -name .env | head -1)
DB_URL=$(grep -E '^DATABASE_URL=' "$ENV" | head -1 | cut -d= -f2-)
psql "$DB_URL" -c "SELECT user_id, snapshot_date, b0, sts_forecast
                   FROM forecast_snapshots
                   ORDER BY snapshot_date DESC LIMIT 10;"
```

---

## Сверка baseline-схемы (опционально, на потом)

`000_baseline.sql` восстановлен из кода. Когда будет время — снять реальный дамп и
сверить типы (расхождения поправить в baseline; на работу прода не влияет):

```bash
pg_dump --schema-only --no-owner --no-privileges "$DB_URL" > /tmp/schema_real.sql
```

---

## Файлы

- `horizon-forecast.service`, `horizon-forecast.timer` — шаблоны юнитов
  (на сервере CI генерирует финальную версию с правильным python).
