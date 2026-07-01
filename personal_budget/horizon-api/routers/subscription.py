"""
Подписка / триал / промокоды.

Статус доступа выводится из users: is_free_forever → paid_until → триал 35 дней
от trial_started_at. Оплата (ЮKassa) — отдельным этапом; здесь только модель
доступа, активация промокода и статус для фронта.
"""
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import asyncpg

router = APIRouter(prefix="/api/account", tags=["account"])

TRIAL_DAYS = 35


def _now() -> datetime:
    return datetime.now(timezone.utc)


def compute_status(user: dict, now: datetime = None) -> dict:
    """Единый вывод статуса доступа из строки users."""
    now = now or _now()
    if user.get("is_free_forever"):
        return {"status": "free_forever", "access": True,
                "trial_days_left": None, "trial_ends_at": None, "paid_until": None}

    paid_until = user.get("paid_until")
    if paid_until and paid_until > now:
        return {"status": "active", "access": True,
                "trial_days_left": None, "trial_ends_at": None,
                "paid_until": paid_until.isoformat()}

    trial_start = user.get("trial_started_at") or now
    trial_end = trial_start + timedelta(days=TRIAL_DAYS)
    if trial_end > now:
        days_left = max(0, (trial_end - now).days)
        return {"status": "trial", "access": True,
                "trial_days_left": days_left, "trial_ends_at": trial_end.isoformat(),
                "paid_until": None}

    return {"status": "expired", "access": False,
            "trial_days_left": 0, "trial_ends_at": trial_end.isoformat(), "paid_until": None}


async def _load_user(db, user_id: str) -> dict:
    row = await db.fetchrow(
        "SELECT id, email, name, trial_started_at, paid_until, is_free_forever "
        "FROM users WHERE id=$1", user_id
    )
    if not row:
        raise HTTPException(404, "Пользователь не найден")
    return dict(row)


@router.get("/status")
async def account_status(request: Request):
    user = await _load_user(request.state.db, request.state.user_id)
    return compute_status(user)


class PromoBody(BaseModel):
    code: str


@router.post("/promo")
async def redeem_promo(request: Request, body: PromoBody):
    db = request.state.db
    user_id = request.state.user_id
    code = (body.code or "").strip().upper()
    if not code:
        raise HTTPException(400, "Введите промокод")

    async with db.transaction():
        # Блокируем строку кода на время проверки+инкремента — лимит соблюдается
        # даже при одновременных активациях (FOR UPDATE сериализует).
        pc = await db.fetchrow("SELECT * FROM promo_codes WHERE code=$1 FOR UPDATE", code)
        if not pc:
            raise HTTPException(404, "Промокод не найден")
        if not pc["is_active"]:
            raise HTTPException(400, "Промокод больше не действует")
        if pc["redeemed_count"] >= pc["max_redemptions"]:
            raise HTTPException(400, "Промокод исчерпан")

        try:
            await db.execute(
                "INSERT INTO promo_redemptions (code, user_id) VALUES ($1, $2)",
                code, user_id
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(400, "Вы уже активировали промокод")

        await db.execute(
            "UPDATE promo_codes SET redeemed_count = redeemed_count + 1 WHERE code=$1", code
        )
        if pc["kind"] == "free_forever":
            await db.execute("UPDATE users SET is_free_forever = true WHERE id=$1", user_id)

    user = await _load_user(db, user_id)
    result = compute_status(user)
    result["ok"] = True
    return result
