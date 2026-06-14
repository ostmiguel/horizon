from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from datetime import date
from calendar import monthrange

router = APIRouter(prefix="/api/loans", tags=["loans"])

# category_ids для плановых записей кредита
CAT_PRINCIPAL = 179  # Кредиты - тело  (→ Обязательства)
CAT_INTEREST  = 144  # Кредиты - процент (→ Расход)

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


@router.post("/generate-plan")
async def generate_plan_from_loans(
    request: Request,
    year:  int = Query(...),
    month: int = Query(...),
):
    """Генерирует записи в plan из loan_schedule для заданного месяца.
    Тело → Обязательства, процент → Расход. Upsert по (user_id, date, account_from, account_to, category_id).
    Вызывается вручную или кроном в начале месяца."""
    user_id = request.state.user_id
    db = request.state.db

    rows = await db.fetch("""
        SELECT ls.loan_id, ls.date, ls.principal, ls.interest, l.name
        FROM loan_schedule ls
        JOIN loans l ON ls.loan_id = l.id
        WHERE l.user_id = $1
          AND EXTRACT(YEAR  FROM ls.date) = $2
          AND EXTRACT(MONTH FROM ls.date) = $3
          AND ls.is_paid = false
          AND l.is_active = true
          AND ls.principal IS NOT NULL
    """, user_id, year, month)

    async with db.transaction():
        # Удаляем старые авто-записи за этот месяц перед повторной генерацией
        await db.execute("""
            DELETE FROM plan
            WHERE user_id=$1 AND source='loan_schedule'
              AND EXTRACT(YEAR FROM date)=$2 AND EXTRACT(MONTH FROM date)=$3
        """, user_id, year, month)

        created = 0
        for r in rows:
            pay_date = r["date"]
            if r["principal"] and float(r["principal"]) > 0:
                await db.execute("""
                    INSERT INTO plan (user_id, date, amount, account_from, account_to, category_id, source)
                    VALUES ($1,$2,$3,'Карта Тбанк','Обязательства',$4,'loan_schedule')
                """, user_id, pay_date, float(r["principal"]), CAT_PRINCIPAL)
                created += 1
            if r["interest"] and float(r["interest"]) > 0:
                await db.execute("""
                    INSERT INTO plan (user_id, date, amount, account_from, account_to, category_id, source)
                    VALUES ($1,$2,$3,'Карта Тбанк','Расход',$4,'loan_schedule')
                """, user_id, pay_date, float(r["interest"]), CAT_INTEREST)
                created += 1

    return {"ok": True, "created": created, "year": year, "month": month}


@router.post("/{loan_id}/recalculate")
async def recalculate_loan(
    loan_id: int,
    request: Request,
    from_month: int = Query(..., description="month_num начиная с которого пересчитать"),
):
    """Пересчитывает оставшийся график аннуитета после досрочного платежа.
    Берёт баланс из loan_schedule[from_month-1].balance и пересчитывает вперёд."""
    user_id = request.state.user_id
    db = request.state.db

    loan = await db.fetchrow(
        "SELECT * FROM loans WHERE id=$1 AND user_id=$2", loan_id, user_id
    )
    if not loan:
        raise HTTPException(404)

    # Баланс после предыдущего платежа
    prev = await db.fetchrow(
        "SELECT balance FROM loan_schedule WHERE loan_id=$1 AND month_num=$2",
        loan_id, from_month - 1
    )
    if not prev:
        raise HTTPException(400, f"month_num {from_month-1} not found")

    balance = float(prev["balance"])
    monthly_rate = float(loan["rate"]) / 12
    payment = float(loan["monthly_payment"])

    # Все будущие строки начиная с from_month
    future = await db.fetch("""
        SELECT month_num, date, extra_payment
        FROM loan_schedule
        WHERE loan_id=$1 AND month_num >= $2
        ORDER BY month_num
    """, loan_id, from_month)

    updated = 0
    async with db.transaction():
        for row in future:
            if balance <= 0:
                # Кредит погашен досрочно — обнуляем остаток строк
                await db.execute("""
                    UPDATE loan_schedule
                    SET principal=0, interest=0, payment=0, balance=0
                    WHERE loan_id=$1 AND month_num=$2
                """, loan_id, row["month_num"])
                updated += 1
                continue

            extra = float(row["extra_payment"] or 0)
            interest   = round(balance * monthly_rate, 2)
            principal  = round(min(payment - interest + extra, balance), 2)
            new_balance = round(balance - principal, 2)
            total_pay  = round(interest + principal, 2)

            await db.execute("""
                UPDATE loan_schedule
                SET interest=$3, principal=$4, payment=$5, balance=$6
                WHERE loan_id=$1 AND month_num=$2
            """, loan_id, row["month_num"],
                interest, principal, total_pay, max(new_balance, 0))

            balance = max(new_balance, 0)
            updated += 1

        # Обновляем current_balance в loans
        await db.execute(
            "UPDATE loans SET current_balance=$2, updated_at=now() WHERE id=$1",
            loan_id, balance
        )

    return {"ok": True, "updated": updated, "final_balance": balance}
