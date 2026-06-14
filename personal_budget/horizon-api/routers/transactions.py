from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import date

router = APIRouter(prefix="/api/transactions", tags=["transactions"])

class TxCreate(BaseModel):
    date: date
    amount: float
    account_from: str
    account_to: str
    category_id: Optional[int] = None
    participant_id: Optional[int] = None
    note: Optional[str] = None
    source: Optional[str] = "manual"

class TxUpdate(BaseModel):
    date: Optional[date] = None
    amount: Optional[float] = None
    account_from: Optional[str] = None
    account_to: Optional[str] = None
    category_id: Optional[int] = None
    note: Optional[str] = None

@router.get("")
async def get_transactions(
    request: Request,
    year: Optional[int] = None,
    month: Optional[int] = None,
    limit: Optional[int] = 200,
    plan: bool = False
):
    user_id = request.state.user_id
    db = request.state.db
    table = "plan" if plan else "transactions"

    conditions = ["t.user_id = $1"]
    params = [user_id]
    i = 2

    if year:
        conditions.append(f"EXTRACT(YEAR FROM t.date) = ${i}")
        params.append(year); i += 1
    if month:
        conditions.append(f"EXTRACT(MONTH FROM t.date) = ${i}")
        params.append(month); i += 1

    where = " AND ".join(conditions)
    rows = await db.fetch(f"""
        SELECT t.*,
               c.group_name, c.category, c.subcategory, c.character, c.expense_type
        FROM {table} t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE {where}
        ORDER BY t.date DESC
        LIMIT ${i}
    """, *params, limit)

    result = []
    for r in rows:
        d = dict(r)
        if d.get('group_name'):
            d['categories'] = {
                'group_name':   d.pop('group_name'),
                'category':     d.pop('category'),
                'subcategory':  d.pop('subcategory'),
                'character':    d.pop('character'),
                'expense_type': d.pop('expense_type'),
            }
        else:
            for k in ['group_name','category','subcategory','character','expense_type']:
                d.pop(k, None)
            d['categories'] = None
        result.append(d)
    return result

@router.post("")
async def create_transaction(data: TxCreate, request: Request, plan: bool = False):
    user_id = request.state.user_id
    db = request.state.db
    table = "plan" if plan else "transactions"
    row = await db.fetchrow(f"""
        INSERT INTO {table}
          (user_id, date, amount, account_from, account_to, category_id, participant_id, note, source)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *
    """, user_id, data.date, data.amount, data.account_from, data.account_to,
        data.category_id, data.participant_id, data.note, data.source)
    return dict(row)

@router.patch("/{tx_id}")
async def update_transaction(tx_id: int, data: TxUpdate, request: Request, plan: bool = False):
    user_id = request.state.user_id
    db = request.state.db
    table = "plan" if plan else "transactions"
    updates = data.dict(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields")
    sets = ", ".join(f"{k} = ${i+3}" for i, k in enumerate(updates))
    row = await db.fetchrow(
        f"UPDATE {table} SET {sets} WHERE id=$1 AND user_id=$2 RETURNING *",
        tx_id, user_id, *updates.values()
    )
    if not row:
        raise HTTPException(404)
    return dict(row)

@router.delete("/{tx_id}")
async def delete_transaction(tx_id: int, request: Request, plan: bool = False):
    user_id = request.state.user_id
    db = request.state.db
    table = "plan" if plan else "transactions"
    await db.execute(
        f"DELETE FROM {table} WHERE id=$1 AND user_id=$2",
        tx_id, user_id
    )
    return {"ok": True}
