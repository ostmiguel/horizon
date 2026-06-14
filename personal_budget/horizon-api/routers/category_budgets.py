from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/category-budgets", tags=["category_budgets"])


class BudgetUpsert(BaseModel):
    category_id: int
    year: int
    month: int
    budget: float


@router.get("")
async def get_category_budgets(request: Request, year: Optional[int] = None, month: Optional[int] = None):
    user_id = request.state.user_id
    db = request.state.db

    conditions = ["cb.user_id = $1"]
    params = [user_id]
    i = 2
    if year:
        conditions.append(f"cb.year = ${i}"); params.append(year); i += 1
    if month:
        conditions.append(f"cb.month = ${i}"); params.append(month); i += 1

    rows = await db.fetch(f"""
        SELECT cb.*,
               c.subcategory, c.category, c.group_name,
               c.expense_type, c.character
        FROM category_budgets cb
        JOIN categories c ON cb.category_id = c.id
        WHERE {' AND '.join(conditions)}
        ORDER BY cb.year, cb.month, c.category, c.subcategory
    """, *params)

    result = []
    for r in rows:
        d = dict(r)
        d["categories"] = {
            "subcategory":  d.pop("subcategory"),
            "category":     d.pop("category"),
            "group_name":   d.pop("group_name"),
            "expense_type": d.pop("expense_type"),
            "character":    d.pop("character"),
        }
        result.append(d)
    return result


@router.put("")
async def upsert_budget(data: BudgetUpsert, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    row = await db.fetchrow("""
        INSERT INTO category_budgets (user_id, category_id, year, month, budget)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (user_id, category_id, year, month)
        DO UPDATE SET budget = EXCLUDED.budget
        RETURNING *
    """, user_id, data.category_id, data.year, data.month, data.budget)
    return dict(row)


@router.delete("/{budget_id}")
async def delete_budget(budget_id: int, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    await db.execute(
        "DELETE FROM category_budgets WHERE id=$1 AND user_id=$2",
        budget_id, user_id
    )
    return {"ok": True}
