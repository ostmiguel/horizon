from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/goals", tags=["goals"])


class GoalCreate(BaseModel):
    name: str
    target_amount: float
    account_id: Optional[int] = None
    due_date: Optional[str] = None


class GoalUpdate(BaseModel):
    name: Optional[str] = None
    target_amount: Optional[float] = None
    account_id: Optional[int] = None
    due_date: Optional[str] = None


@router.get("")
async def get_goals(request: Request):
    user_id = request.state.user_id
    db = request.state.db
    rows = await db.fetch("""
        SELECT g.*,
               COALESCE(
                 a.initial_balance
                 + COALESCE(SUM(CASE WHEN t.account_to   = a.name THEN t.amount ELSE 0 END), 0)
                 - COALESCE(SUM(CASE WHEN t.account_from = a.name THEN t.amount ELSE 0 END), 0),
                 0
               ) AS current_balance
        FROM goals g
        LEFT JOIN accounts a ON a.id = g.account_id AND a.user_id = $1
        LEFT JOIN transactions t
            ON (t.account_from = a.name OR t.account_to = a.name) AND t.user_id = $1
        WHERE g.user_id = $1
        GROUP BY g.id, g.name, g.target_amount, g.account_id, g.due_date, g.created_at,
                 a.initial_balance
        ORDER BY g.created_at
    """, user_id)
    return [dict(r) for r in rows]


@router.post("")
async def create_goal(data: GoalCreate, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    row = await db.fetchrow("""
        INSERT INTO goals (user_id, name, target_amount, account_id, due_date)
        VALUES ($1, $2, $3, $4, $5) RETURNING *
    """, user_id, data.name, data.target_amount, data.account_id,
        data.due_date)
    return dict(row)


@router.patch("/{goal_id}")
async def update_goal(goal_id: int, data: GoalUpdate, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    updates = data.dict(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    sets = ", ".join(f"{k} = ${i+3}" for i, k in enumerate(updates))
    row = await db.fetchrow(
        f"UPDATE goals SET {sets} WHERE id=$1 AND user_id=$2 RETURNING *",
        goal_id, user_id, *updates.values()
    )
    if not row:
        raise HTTPException(404, "Goal not found")
    return dict(row)


@router.delete("/{goal_id}")
async def delete_goal(goal_id: int, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    await db.execute("DELETE FROM goals WHERE id=$1 AND user_id=$2", goal_id, user_id)
    return {"ok": True}
