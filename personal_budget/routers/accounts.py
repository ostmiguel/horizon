from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

class AccountCreate(BaseModel):
    name: str
    account_type: str
    color: Optional[str] = "#b0c4de"
    initial_balance: Optional[float] = 0
    include_in_balance: Optional[bool] = True

class AccountUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    initial_balance: Optional[float] = None
    include_in_balance: Optional[bool] = None
    sort_order: Optional[int] = None

@router.get("")
async def get_accounts(request: Request):
    user_id = request.state.user_id
    db = request.state.db
    rows = await db.fetch("""
        SELECT a.*,
          COALESCE(
            a.initial_balance
            + COALESCE(SUM(CASE WHEN t.account_to   = a.name THEN t.amount ELSE 0 END), 0)
            - COALESCE(SUM(CASE WHEN t.account_from = a.name THEN t.amount ELSE 0 END), 0),
            a.initial_balance
          ) AS current_balance
        FROM accounts a
        LEFT JOIN transactions t
          ON (t.account_from = a.name OR t.account_to = a.name)
          AND t.user_id = $1
        WHERE a.user_id = $1
        GROUP BY a.id
        ORDER BY a.sort_order, a.id
    """, user_id)
    return [dict(r) for r in rows]

@router.post("")
async def create_account(data: AccountCreate, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    row = await db.fetchrow("""
        INSERT INTO accounts (user_id, name, account_type, color, initial_balance, include_in_balance)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING *
    """, user_id, data.name, data.account_type, data.color,
        data.initial_balance, data.include_in_balance)
    return dict(row)

@router.patch("/{account_id}")
async def update_account(account_id: int, data: AccountUpdate, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    updates = data.dict(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    sets = ", ".join(f"{k} = ${i+3}" for i, k in enumerate(updates))
    row = await db.fetchrow(
        f"UPDATE accounts SET {sets} WHERE id = $1 AND user_id = $2 RETURNING *",
        account_id, user_id, *updates.values()
    )
    if not row:
        raise HTTPException(404, "Account not found")
    return dict(row)

@router.delete("/{account_id}")
async def delete_account(account_id: int, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    await db.execute(
        "DELETE FROM accounts WHERE id = $1 AND user_id = $2",
        account_id, user_id
    )
    return {"ok": True}
