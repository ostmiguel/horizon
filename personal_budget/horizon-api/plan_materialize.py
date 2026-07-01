"""
Материализация plan_rules → plan.

Активные правила (is_active=true) разворачиваются в конкретные плановые строки
таблицы plan на заданный месяц. Метрики/прогноз читают plan, поэтому после
материализации правила автоматически учитываются в /api/metrics.

Идемпотентность: полная пере-материализация за месяц — сносим прежние строки,
порождённые правилами (source_rule_id IS NOT NULL), и пересоздаём из активных.
Повторный прогон не плодит дубли; деактивация/удаление/правка правила
отражаются на следующем прогоне. Строки помечаются source='plan_rule' и
source_rule_id=<id правила> — по ним фронт группирует план под правилами.
"""
from datetime import date
from calendar import monthrange


async def materialize_rules(db, user_id, year: int, month: int) -> int:
    """Пере-материализует активные plan_rules пользователя в plan на год+месяц.
    Возвращает число созданных строк."""
    days_in_month = monthrange(year, month)[1]
    rules = await db.fetch("""
        SELECT id, amount, account_from, account_to, category_id, day_of_month
        FROM plan_rules
        WHERE user_id=$1 AND is_active=true
        ORDER BY day_of_month NULLS LAST, id
    """, user_id)

    async with db.transaction():
        # снести прежнюю материализацию правил за месяц, КРОМЕ pinned (ручные правки
        # «только этот месяц» сохраняем — иначе они слетали бы при перештамповке).
        await db.execute("""
            DELETE FROM plan
            WHERE user_id=$1 AND source_rule_id IS NOT NULL
              AND EXTRACT(YEAR  FROM date)=$2
              AND EXTRACT(MONTH FROM date)=$3
              AND pinned = false
        """, user_id, year, month)

        # правила, которые НЕ материализуем в этом месяце:
        #  • есть pinned-строка (ручная правка) — оставляем её, новую не создаём;
        #  • есть запись в plan_rule_skips — «удалено только в этом месяце».
        pinned_ids = {row["source_rule_id"] for row in await db.fetch("""
            SELECT DISTINCT source_rule_id FROM plan
            WHERE user_id=$1 AND source_rule_id IS NOT NULL
              AND EXTRACT(YEAR FROM date)=$2 AND EXTRACT(MONTH FROM date)=$3
              AND pinned = true
        """, user_id, year, month)}
        skip_ids = {row["rule_id"] for row in await db.fetch("""
            SELECT rule_id FROM plan_rule_skips WHERE user_id=$1 AND year=$2 AND month=$3
        """, user_id, year, month)}

        created = 0
        for r in rules:
            if r["id"] in pinned_ids or r["id"] in skip_ids:
                continue
            dom = int(r["day_of_month"]) if r["day_of_month"] else 1
            day = max(1, min(dom, days_in_month))   # клампим (напр. 31 в феврале → 28/29)
            d = date(year, month, day)
            await db.execute("""
                INSERT INTO plan
                  (user_id, date, amount, account_from, account_to, category_id, source, source_rule_id)
                VALUES ($1, $2, $3, $4, $5, $6, 'plan_rule', $7)
            """, user_id, d, float(r["amount"]),
                 r["account_from"], r["account_to"], r["category_id"], r["id"])
            created += 1

    return created


async def ensure_materialized(db, user_id, year: int, month: int) -> int:
    """Ленивая материализация: если за (год, месяц) НЕТ строк, порождённых
    правилами, а активные правила есть — штампуем. Закрывает разрыв «будущий
    месяц ещё ни разу не материализовался» (кроном план текущего месяца
    штампуется только в день-1, а окно прогноза/пилюль читает plan напрямую).

    Дёшево: два EXISTS. Материализация правил всегда атомарна по ВСЕМ активным
    правилам месяца (materialize_rules сносит и пересоздаёт целиком), поэтому
    достаточно проверить наличие любой rule-строки — частичного расхождения
    по отдельным правилам быть не может. Свежесть уже материализованного месяца
    поддерживают правки правил (_materialize_current_next) и день-1 крон."""
    has_rows = await db.fetchval("""
        SELECT EXISTS(
            SELECT 1 FROM plan
            WHERE user_id=$1 AND source_rule_id IS NOT NULL
              AND EXTRACT(YEAR FROM date)=$2 AND EXTRACT(MONTH FROM date)=$3
        )
    """, user_id, year, month)
    if has_rows:
        return 0
    has_rules = await db.fetchval(
        "SELECT EXISTS(SELECT 1 FROM plan_rules WHERE user_id=$1 AND is_active=true)",
        user_id,
    )
    if not has_rules:
        return 0
    return await materialize_rules(db, user_id, year, month)


def current_and_next_month(today: date = None):
    """[(год, месяц)] текущего и следующего месяца — для прогона при правке правила."""
    today = today or date.today()
    y, m = today.year, today.month
    nxt = (y + 1, 1) if m == 12 else (y, m + 1)
    return [(y, m), nxt]
