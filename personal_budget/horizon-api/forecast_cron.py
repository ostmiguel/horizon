#!/usr/bin/env python3
"""
Horizon — ежедневный снапшот STS-прогноза (23:55 каждый день).
Записывает компоненты Safe-to-Spend и их сумму в forecast_snapshots.
В последний день месяца actual_b0 = финальный оперативный баланс.
"""
import asyncio
import asyncpg
import os
import statistics
from datetime import date, timedelta
from calendar import monthrange
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL")
OWNER_ID = "084f18c0-927d-444e-a9d0-7313042d7163"


async def run():
    conn = await asyncpg.connect(DB_URL)
    today = date.today()
    year, month = today.year, today.month
    days_in_month = monthrange(year, month)[1]
    d_left = days_in_month - today.day
    month_end = date(year, month, days_in_month)

    try:
        # ── B0: оперативный баланс (Актив + include_in_balance=True, без подушки) ──
        acc_rows = await conn.fetch("""
            SELECT
                a.initial_balance
                + COALESCE(SUM(CASE WHEN t.account_to   = a.name THEN t.amount ELSE 0 END), 0)
                - COALESCE(SUM(CASE WHEN t.account_from = a.name THEN t.amount ELSE 0 END), 0)
                AS balance
            FROM accounts a
            LEFT JOIN transactions t
                ON (t.account_from = a.name OR t.account_to = a.name)
                AND t.user_id = $1
            WHERE a.user_id = $1
              AND a.is_active = true
              AND a.account_type = 'Актив'
              AND a.include_in_balance = true
              AND (a.is_cushion IS NULL OR a.is_cushion = false)
            GROUP BY a.id, a.initial_balance
        """, OWNER_ID)
        b0 = sum(float(r["balance"]) for r in acc_rows)

        # ── I_remain: запланированные доходы после сегодня ──
        i_remain = float(await conn.fetchval("""
            SELECT COALESCE(SUM(amount), 0) FROM plan
            WHERE user_id = $1
              AND EXTRACT(YEAR  FROM date) = $2
              AND EXTRACT(MONTH FROM date) = $3
              AND date > $4
              AND account_from = 'Доход'
        """, OWNER_ID, year, month, today))

        # ── F_remain: фиксированные + эпизодические + обязательства из плана ──
        plan_rows = await conn.fetch("""
            SELECT p.amount, p.account_to,
                   c.character    AS cat_character,
                   c.expense_type AS cat_expense_type
            FROM plan p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.user_id = $1
              AND EXTRACT(YEAR  FROM p.date) = $2
              AND EXTRACT(MONTH FROM p.date) = $3
              AND p.date > $4
              AND p.account_to IN ('Расход', 'Обязательства')
        """, OWNER_ID, year, month, today)

        f_remain = sum(
            float(r["amount"]) for r in plan_rows
            if r["account_to"] == "Обязательства"
            or r["cat_expense_type"] == "fixed"
            or r["cat_character"] == "Эпизодический"
        )

        # ── V_remain: поведенческий прогноз переменной повседневки ──
        cutoff = today - timedelta(days=30)
        daily_rows = await conn.fetch("""
            SELECT t.date, SUM(t.amount) AS daily_total
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = $1
              AND t.date >= $2 AND t.date < $3
              AND t.account_to = 'Расход'
              AND c.expense_type = 'variable'
              AND c.character != 'Эпизодический'
            GROUP BY t.date ORDER BY t.date
        """, OWNER_ID, cutoff, today)

        daily = [float(r["daily_total"]) for r in daily_rows]
        if len(daily) >= 4:
            r_var = statistics.median(daily)
        elif daily:
            r_var = statistics.mean(daily)
        else:
            r_var = 0.0

        v_remain = r_var * d_left

        # ── STS прогноз ──
        sts_forecast = b0 + i_remain - f_remain - v_remain

        # ── actual_b0: в последний день месяца B0 и есть финальный баланс ──
        actual_b0 = b0 if today == month_end else None

        # ── Upsert снапшота ──
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
        """, OWNER_ID, today, month_end,
             b0, i_remain, f_remain, v_remain, sts_forecast, actual_b0)

        print(
            f"[{today}] snapshot ok | "
            f"b0={b0:.0f} i_remain={i_remain:.0f} "
            f"f_remain={f_remain:.0f} v_remain={v_remain:.0f} "
            f"sts={sts_forecast:.0f}"
            + (f" | MONTH_END actual_b0={actual_b0:.0f}" if actual_b0 is not None else "")
        )

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())
