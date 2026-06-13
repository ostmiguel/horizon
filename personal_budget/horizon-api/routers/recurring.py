from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

router = APIRouter(prefix="/api/recurring", tags=["recurring"])

class RecurringCreate(BaseModel):
    amount: float
    account_from: str
    account_to: str
    category_id: Optional[int] = None
    note: Optional[str] = None
    frequency: str  # monthly | weekly | daily
    start_date: date
    end_date: Optional[date] = None

@router.get("")
async def get_recurring(request: Request):
    user_id = request.state.user_id
    db = request.state.db
    rows = await db.fetch(
        "SELECT * FROM recurring WHERE user_id=$1 AND is_active=true ORDER BY id",
        user_id
    )
    return [dict(r) for r in rows]

@router.post("")
async def create_recurring(data: RecurringCreate, request: Request):
    user_id = request.state.user_id
    db = request.state.db

    # Save template
    row = await db.fetchrow("""
        INSERT INTO recurring
          (user_id, amount, account_from, account_to, category_id, note, frequency, start_date, end_date)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *
    """, user_id, data.amount, data.account_from, data.account_to,
        data.category_id, data.note, data.frequency, data.start_date, data.end_date)

    # Generate plan entries
    end = data.end_date or date(data.start_date.year, 12, 31)
    dates = []
    cur = data.start_date
    while cur <= end and len(dates) < 500:
        dates.append(cur)
        if data.frequency == "monthly":
            cur = cur + relativedelta(months=1)
        elif data.frequency == "weekly":
            cur = cur + timedelta(weeks=1)
        else:
            cur = cur + timedelta(days=1)

    async with db.transaction():
        for d in dates:
            await db.execute("""
                INSERT INTO plan (user_id, date, amount, account_from, account_to, category_id, note, source)
                VALUES ($1,$2,$3,$4,$5,$6,$7,'recurring')
            """, user_id, d, data.amount, data.account_from, data.account_to,
                data.category_id, data.note)

    return {"template": dict(row), "plan_entries_created": len(dates)}

@router.delete("/{rec_id}")
async def delete_recurring(rec_id: int, request: Request, delete_future_plan: bool = False):
    user_id = request.state.user_id
    db = request.state.db
    rec = await db.fetchrow("SELECT * FROM recurring WHERE id=$1 AND user_id=$2", rec_id, user_id)
    if not rec:
        raise HTTPException(404)
    await db.execute("UPDATE recurring SET is_active=false WHERE id=$1", rec_id)
    if delete_future_plan:
        await db.execute("""
            DELETE FROM plan
            WHERE user_id=$1 AND source='recurring'
            AND account_from=$2 AND account_to=$3
            AND date > NOW()
        """, user_id, rec["account_from"], rec["account_to"])
    return {"ok": True}
