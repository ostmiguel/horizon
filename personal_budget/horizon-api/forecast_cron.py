#!/usr/bin/env python3
"""
Horizon — ежедневный снапшот STS-прогноза (23:55 каждый день).
Пишет компоненты Safe-to-Spend и их сумму в forecast_snapshots для КАЖДОГО
активного пользователя. В последний день месяца actual_b0 = финальный
оперативный баланс.

Математика STS/дневной ставки — из общего модуля metrics_core (тот же код, что
в /api/metrics). Никаких дублирующих формул и захардкоженных UUID.
"""
import asyncio
import asyncpg
import os
import sys
from datetime import date
from calendar import monthrange
from dotenv import load_dotenv

# гарантируем импорт metrics_core независимо от рабочей директории запуска
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from metrics_core import safe_to_spend  # noqa: E402
from plan_materialize import materialize_rules, ensure_materialized, current_and_next_month  # noqa: E402

load_dotenv()
DB_URL = os.getenv("DATABASE_URL")

async def active_user_ids(conn) -> list:
    """Все пользователи, у которых есть хоть одна транзакция."""
    rows = await conn.fetch("SELECT DISTINCT user_id FROM transactions")
    return [r["user_id"] for r in rows]


async def generate_loan_plan(conn, user_id, year, month) -> int:
    """В первый день месяца разворачивает loan_schedule в строки plan (per-user).
    Категории — по системной метке role, счёт списания — из самого кредита
    (account_from), без захардкоженных id и имён."""
    cat_principal = await conn.fetchval(
        "SELECT id FROM categories WHERE user_id=$1 AND role='loan_principal'", user_id)
    cat_interest = await conn.fetchval(
        "SELECT id FROM categories WHERE user_id=$1 AND role='loan_interest'", user_id)
    fallback_acc = await conn.fetchval("""
        SELECT name FROM accounts
        WHERE user_id=$1 AND account_type='Актив' AND is_active=true
        ORDER BY id LIMIT 1
    """, user_id)

    loan_rows = await conn.fetch("""
        SELECT ls.loan_id, ls.date, ls.principal, ls.interest, l.account_from, l.account_name
        FROM loan_schedule ls
        JOIN loans l ON ls.loan_id = l.id
        WHERE l.user_id = $1
          AND EXTRACT(YEAR  FROM ls.date) = $2
          AND EXTRACT(MONTH FROM ls.date) = $3
          AND ls.is_paid = false
          AND l.is_active = true
          AND ls.principal IS NOT NULL
    """, user_id, year, month)
    if not loan_rows:
        return 0

    await conn.execute("""
        DELETE FROM plan
        WHERE user_id=$1 AND source='loan_schedule'
          AND EXTRACT(YEAR FROM date)=$2 AND EXTRACT(MONTH FROM date)=$3
    """, user_id, year, month)

    created = 0
    for lr in loan_rows:
        acc = lr["account_from"] or fallback_acc
        liab_acc = lr["account_name"] or "Обязательства"   # тело → счёт кредита (fallback: пул)
        if cat_principal and lr["principal"] and float(lr["principal"]) > 0:
            await conn.execute("""
                INSERT INTO plan (user_id, date, amount, account_from, account_to, category_id, source)
                VALUES ($1,$2,$3,$4,$5,$6,'loan_schedule')
            """, user_id, lr["date"], float(lr["principal"]), acc, liab_acc, cat_principal)
            created += 1
        if cat_interest and lr["interest"] and float(lr["interest"]) > 0:
            await conn.execute("""
                INSERT INTO plan (user_id, date, amount, account_from, account_to, category_id, source)
                VALUES ($1,$2,$3,$4,'Расход',$5,'loan_schedule')
            """, user_id, lr["date"], float(lr["interest"]), acc, cat_interest)
            created += 1
    return created


async def snapshot_user(conn, user_id, today, month_end) -> dict:
    """Считает STS через общий safe_to_spend и пишет снапшот."""
    m = await safe_to_spend(conn, user_id, today)
    b0           = m["B0"]
    i_remain     = m["I_remain"]
    f_remain     = m["F_remain"]
    v_remain     = m["V_remain"]
    sts_forecast = m["sts"]          # = B0 + I − F − V − R_topup (как в /api/metrics)
    actual_b0    = b0 if today == month_end else None

    await conn.execute("""
        INSERT INTO forecast_snapshots
          (user_id, snapshot_date, month_end,
           b0, i_remain, f_remain, v_remain, sts_forecast, actual_b0)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (user_id, snapshot_date) DO UPDATE SET
          b0           = EXCLUDED.b0,
          i_remain     = EXCLUDED.i_remain,
          f_remain     = EXCLUDED.f_remain,
          v_remain     = EXCLUDED.v_remain,
          sts_forecast = EXCLUDED.sts_forecast,
          actual_b0    = COALESCE(EXCLUDED.actual_b0, forecast_snapshots.actual_b0)
    """, user_id, today, month_end,
         b0, i_remain, f_remain, v_remain, sts_forecast, actual_b0)

    return {"b0": b0, "i_remain": i_remain, "f_remain": f_remain,
            "v_remain": v_remain, "sts": sts_forecast, "actual_b0": actual_b0}


async def run():
    conn = await asyncpg.connect(DB_URL)
    today = date.today()
    year, month = today.year, today.month
    days_in_month = monthrange(year, month)[1]
    month_end = date(year, month, days_in_month)

    try:
        users = await active_user_ids(conn)
        print(f"[{today}] snapshot run: {len(users)} active users")
        for uid in users:
            try:
                if today.day == 1:
                    n = await generate_loan_plan(conn, uid, year, month)
                    if n:
                        print(f"  [{uid}] loan plan generated: {n} rows")
                    nr = await materialize_rules(conn, uid, year, month)
                    if nr:
                        print(f"  [{uid}] plan rules materialized: {nr} rows")
                # Проактивно штампуем следующий месяц (окно прогноза/пилюль до ~75
                # дней читает plan напрямую). ensure дёшев и не трогает уже готовый.
                for y, mo in current_and_next_month(today):
                    await ensure_materialized(conn, uid, y, mo)
                r = await snapshot_user(conn, uid, today, month_end)
                print(
                    f"  [{uid}] ok | b0={r['b0']:.0f} i={r['i_remain']:.0f} "
                    f"f={r['f_remain']:.0f} v={r['v_remain']:.0f} sts={r['sts']:.0f}"
                    + (f" | MONTH_END actual_b0={r['actual_b0']:.0f}"
                       if r["actual_b0"] is not None else "")
                )
            except Exception as e:
                print(f"  [{uid}] ERROR: {e}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())
