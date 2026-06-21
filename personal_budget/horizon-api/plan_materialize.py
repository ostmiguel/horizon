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
        # снести прежнюю материализацию правил за этот месяц (incl. инактив/удалённые)
        await db.execute("""
            DELETE FROM plan
            WHERE user_id=$1 AND source_rule_id IS NOT NULL
              AND EXTRACT(YEAR  FROM date)=$2
              AND EXTRACT(MONTH FROM date)=$3
        """, user_id, year, month)

        created = 0
        for r in rules:
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


def current_and_next_month(today: date = None):
    """[(год, месяц)] текущего и следующего месяца — для прогона при правке правила."""
    today = today or date.today()
    y, m = today.year, today.month
    nxt = (y + 1, 1) if m == 12 else (y, m + 1)
    return [(y, m), nxt]
