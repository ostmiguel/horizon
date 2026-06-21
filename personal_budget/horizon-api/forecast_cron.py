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

load_dotenv()
DB_URL = os.getenv("DATABASE_URL")

# category_ids для авто-генерации плана из графика кредита
# TODO: счёт 'Карта Тбанк' и id 179/144 захардкожены — вынести в настройки/loan-конфиг
CAT_PRINCIPAL = 179  # Кредиты - тело  (→ Обязательства)
CAT_INTEREST  = 144  # Кредиты - процент (→ Расход)


async def active_user_ids(conn) -> list:
    """Все пользователи, у которых есть хоть одна транзакция."""
    rows = await conn.fetch("SELECT DISTINCT user_id FROM transactions")
    return [r["user_id"] for r in rows]


async def generate_loan_plan(conn, user_id, year, month) -> int:
    """В первый день месяца разворачивает loan_schedule в строки plan (per-user)."""
    loan_rows = await conn.fetch("""
        SELECT ls.loan_id, ls.date, ls.principal, ls.interest
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
        if lr["principal"] and float(lr["principal"]) > 0:
            await conn.execute("""
                INSERT INTO plan (user_id, date, amount, account_from, account_to, category_id, source)
                VALUES ($1,$2,$3,'Карта Тбанк','Обязательства',$4,'loan_schedule')
            """, user_id, lr["date"], float(lr["principal"]), CAT_PRINCIPAL)
            created += 1
        if lr["interest"] and float(lr["interest"]) > 0:
            await conn.execute("""
                INSERT INTO plan (user_id, date, amount, account_from, account_to, category_id, source)
                VALUES ($1,$2,$3,'Карта Тбанк','Расход',$4,'loan_schedule')
            """, user_id, lr["date"], float(lr["interest"]), CAT_INTEREST)
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
