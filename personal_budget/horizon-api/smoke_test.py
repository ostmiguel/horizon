"""Pre-restart smoke-проверка эндпоинтов.

Поднимает НОВЫЙ код через TestClient под сессией реального (самого активного)
пользователя и дёргает ключевые GET-эндпоинты. Любой ответ 5xx или падение при
импорте приложения → exit 1; деплой тогда ПРОПУСКАЕТ рестарт, и прод остаётся на
рабочем коде. Ловит рантайм-ошибки в теле роут-хендлеров (например переиспользование
переменной), которых не видят изолированные юнит-проверки функций.

Политика:
  • 5xx от эндпоинта или ошибка импорта main → БЛОК (exit 1);
  • нет DATABASE_URL / нет пользователей / не подключиться к БД → SKIP (exit 0),
    чтобы инфра-заминка не блокировала легитимный деплой.
"""
import os
import sys
import asyncio
from datetime import date


def _run(coro):
    return asyncio.run(coro)


async def _canary_uid(db_url):
    import asyncpg
    conn = await asyncpg.connect(db_url)
    try:
        uid = await conn.fetchval(
            "SELECT user_id FROM transactions GROUP BY user_id ORDER BY count(*) DESC LIMIT 1")
        if uid is None:
            uid = await conn.fetchval("SELECT id FROM users ORDER BY created_at LIMIT 1")
        return uid
    finally:
        await conn.close()


async def _make_session(db_url, uid):
    import asyncpg
    conn = await asyncpg.connect(db_url)
    try:
        return await conn.fetchval(
            "INSERT INTO sessions (user_id) VALUES ($1) RETURNING token", uid)
    finally:
        await conn.close()


async def _drop_session(db_url, token):
    import asyncpg
    conn = await asyncpg.connect(db_url)
    try:
        await conn.execute("DELETE FROM sessions WHERE token=$1", token)
    finally:
        await conn.close()


def main():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("SMOKE: DATABASE_URL не задан — пропускаю"); return 0

    try:
        uid = _run(_canary_uid(db_url))
    except Exception as e:
        print(f"SMOKE: не подключиться к БД ({type(e).__name__}: {e}) — пропускаю"); return 0
    if uid is None:
        print("SMOKE: нет пользователей — пропускаю"); return 0

    # Импорт приложения — ошибка тут = реальный баг нового кода → блок.
    try:
        from fastapi.testclient import TestClient
        from main import app
    except Exception as e:
        print(f"SMOKE: не импортировать приложение ({type(e).__name__}: {e})"); return 1

    token = _run(_make_session(db_url, uid))
    t = date.today()
    endpoints = [
        "/api/metrics",
        "/api/metrics/forecast?range=30",
        "/api/metrics/forecast?range=year",
        "/api/metrics/affordability",
        "/api/payments/pending",
        f"/api/payments/status?year={t.year}&month={t.month}",
        "/api/category-budgets",
        "/api/accounts",
        "/api/transactions?limit=5",
        "/api/plan-rules",
        "/api/loans",
        "/api/goals",
        "/api/account/status",
    ]
    failures = []
    try:
        # Дефолтный TestClient переподнимает серверное исключение (500) — ловим его
        # в except ниже; так проверка не зависит от версии starlette/kwarg.
        with TestClient(app) as client:
            client.cookies.set("session", token)
            for ep in endpoints:
                try:
                    r = client.get(ep)
                    if r.status_code >= 500:
                        failures.append(f"{ep} -> {r.status_code}: {r.text[:180]}")
                except Exception as e:
                    failures.append(f"{ep} -> EXC {type(e).__name__}: {e}")
    finally:
        try:
            _run(_drop_session(db_url, token))
        except Exception:
            pass

    if failures:
        print(f"SMOKE FAILED ({len(failures)} из {len(endpoints)}):")
        for f in failures:
            print("  -", f)
        return 1
    print(f"SMOKE OK — {len(endpoints)} эндпоинтов, канареечный user {uid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
