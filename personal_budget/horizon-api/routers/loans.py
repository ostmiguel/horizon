from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import date

router = APIRouter(prefix="/api/loans", tags=["loans"])

class LoanCreate(BaseModel):
    name: str
    initial_amount: float
    current_balance: float
    rate: float
    monthly_payment: float
    total_payments: int
    next_payment_date: Optional[date] = None
    color: Optional[str] = "#e24b4a"

class ScheduleRow(BaseModel):
    month_num: int
    date: Optional[date] = None
    payment: Optional[float] = None
    principal: Optional[float] = None
    interest: Optional[float] = None
    balance: Optional[float] = None
    extra_payment: Optional[float] = None
    is_paid: Optional[bool] = False

@router.get("")
async def get_loans(request: Request):
    user_id = request.state.user_id
    db = request.state.db
    rows = await db.fetch(
        "SELECT * FROM loans WHERE user_id=$1 AND is_active=true ORDER BY id",
        user_id
    )
    return [dict(r) for r in rows]

@router.post("")
async def create_loan(data: LoanCreate, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    row = await db.fetchrow("""
        INSERT INTO loans
          (user_id, name, initial_amount, current_balance, rate, monthly_payment, total_payments, next_payment_date, color)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *
    """, user_id, data.name, data.initial_amount, data.current_balance,
        data.rate, data.monthly_payment, data.total_payments,
        data.next_payment_date, data.color)
    return dict(row)

@router.get("/{loan_id}/schedule")
async def get_schedule(loan_id: int, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    # verify ownership
    loan = await db.fetchrow("SELECT id FROM loans WHERE id=$1 AND user_id=$2", loan_id, user_id)
    if not loan:
        raise HTTPException(404)
    rows = await db.fetch(
        "SELECT * FROM loan_schedule WHERE loan_id=$1 ORDER BY month_num",
        loan_id
    )
    return [dict(r) for r in rows]

@router.post("/{loan_id}/schedule")
async def upsert_schedule(loan_id: int, rows: list[ScheduleRow], request: Request):
    user_id = request.state.user_id
    db = request.state.db
    loan = await db.fetchrow("SELECT id FROM loans WHERE id=$1 AND user_id=$2", loan_id, user_id)
    if not loan:
        raise HTTPException(404)
    async with db.transaction():
        for r in rows:
            await db.execute("""
                INSERT INTO loan_schedule
                  (loan_id, month_num, date, payment, principal, interest, balance, extra_payment, is_paid)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (loan_id, month_num) DO UPDATE SET
                  date=$3, payment=$4, principal=$5, interest=$6,
                  balance=$7, extra_payment=$8, is_paid=$9
            """, loan_id, r.month_num, r.date, r.payment, r.principal,
                r.interest, r.balance, r.extra_payment, r.is_paid)
    return {"ok": True}

@router.patch("/{loan_id}/schedule/{month_num}")
async def update_schedule_row(loan_id: int, month_num: int, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    body = await request.json()
    loan = await db.fetchrow("SELECT id FROM loans WHERE id=$1 AND user_id=$2", loan_id, user_id)
    if not loan:
        raise HTTPException(404)
    updates = {k: v for k, v in body.items() if k in ['is_paid','extra_payment','balance','payment','principal','interest','date']}
    if not updates:
        raise HTTPException(400)
    sets = ", ".join(f"{k} = ${i+3}" for i, k in enumerate(updates))
    await db.execute(
        f"UPDATE loan_schedule SET {sets} WHERE loan_id=$1 AND month_num=$2",
        loan_id, month_num, *updates.values()
    )
    return {"ok": True}
