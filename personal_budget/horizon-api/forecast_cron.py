#!/usr/bin/env python3
"""
Horizon — ежедневный снапшот прогнозов расходов.
Запускается кроном в 23:55 каждый день.
Сохраняет три модели в forecast_snapshots.
"""
import asyncio
import asyncpg
import os
import statistics
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL")
OWNER_ID = "084f18c0-927d-444e-a9d0-7313042d7163"


async def run():
    conn = await asyncpg.connect(DB_URL)
    today = date.today()
    year, month = today.year, today.month
    days_in_month = (date(year, month % 12 + 1, 1) - timedelta(days=1)).day if month < 12 else 31
    days_elapsed = today.day
    days_left = days_in_month - days_elapsed
    month_end = date(year, month, days_in_month)

    try:
        # ── Факт текущего месяца (только flow расходы) ──
        flow_fact = await conn.fetchval("""
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE user_id = $1
              AND EXTRACT(YEAR FROM date) = $2
              AND EXTRACT(MONTH FROM date) = $3
              AND op_type = 'expense'
              AND character = 'flow'
        """, OWNER_ID, year, month)

        # ── Модель 1: Текущая (простая экстраполяция) ──
        rate_current = float(flow_fact) / days_elapsed if days_elapsed > 0 else 0
        forecast_current = float(flow_fact) + rate_current * days_left

        # ── Модель 2: Smart (робастная — медиана дневных трат за 30 дней) ──
        cutoff = today - timedelta(days=30)
        daily_rows = await conn.fetch("""
            SELECT date, SUM(amount) as daily_total
            FROM transactions
            WHERE user_id = $1
              AND date >= $2 AND date < $3
              AND op_type = 'expense'
              AND character = 'flow'
            GROUP BY date
            ORDER BY date
        """, OWNER_ID, cutoff, today)

        daily_amounts = [float(r["daily_total"]) for r in daily_rows]
        if len(daily_amounts) >= 7:
            rate_smart = statistics.median(daily_amounts)
        elif len(daily_amounts) > 0:
            rate_smart = statistics.mean(daily_amounts)
        else:
            rate_smart = rate_current

        forecast_smart = float(flow_fact) + rate_smart * days_left

        # ── Модель 3: Гибрид (смесь current и smart с весом по дням) ──
        # Вес текущего месяца растёт от 0 до 1 к 21-му дню
        weight = min(days_elapsed / 21, 1.0)
        rate_hybrid = rate_current * weight + rate_smart * (1 - weight)
        forecast_hybrid = float(flow_fact) + rate_hybrid * days_left

        # ── Actual balance (только если последний день месяца) ──
        actual_balance = None
        if today == month_end:
            actual_balance = await conn.fetchval("""
                SELECT COALESCE(SUM(current_balance), 0)
                FROM accounts
                WHERE user_id = $1
                  AND include_in_balance = true
                  AND account_type = 'Актив'
            """, OWNER_ID)

        # ── Запись снапшота (upsert по user_id + snapshot_date) ──
        await conn.execute("""
            INSERT INTO forecast_snapshots
              (user_id, snapshot_date, month_end, forecast_current, forecast_smart, forecast_hybrid, actual_balance)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (user_id, snapshot_date)
            DO UPDATE SET
              forecast_current = EXCLUDED.forecast_current,
              forecast_smart   = EXCLUDED.forecast_smart,
              forecast_hybrid  = EXCLUDED.forecast_hybrid,
              actual_balance   = COALESCE(EXCLUDED.actual_balance, forecast_snapshots.actual_balance)
        """, OWNER_ID, today, month_end,
             forecast_current, forecast_smart, forecast_hybrid, actual_balance)

        print(f"[{today}] Снапшот записан: current={forecast_current:.0f} smart={forecast_smart:.0f} hybrid={forecast_hybrid:.0f}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())
