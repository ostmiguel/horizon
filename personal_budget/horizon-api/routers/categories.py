from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/categories", tags=["categories"])

class CategoryCreate(BaseModel):
    group_name: str
    category: str
    subcategory: str
    character: Optional[str] = None

class CategoryUpdate(BaseModel):
    group_name: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    character: Optional[str] = None

@router.get("")
async def get_categories(request: Request):
    user_id = request.state.user_id
    db = request.state.db
    rows = await db.fetch(
        "SELECT * FROM categories WHERE user_id = $1 ORDER BY group_name, category, subcategory",
        user_id
    )
    return [dict(r) for r in rows]

@router.post("")
async def create_category(data: CategoryCreate, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    row = await db.fetchrow("""
        INSERT INTO categories (user_id, group_name, category, subcategory, character)
        VALUES ($1,$2,$3,$4,$5) RETURNING *
    """, user_id, data.group_name, data.category, data.subcategory, data.character)
    return dict(row)

@router.patch("/{cat_id}")
async def update_category(cat_id: int, data: CategoryUpdate, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    updates = data.dict(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields")
    sets = ", ".join(f"{k} = ${i+3}" for i, k in enumerate(updates))
    row = await db.fetchrow(
        f"UPDATE categories SET {sets} WHERE id=$1 AND user_id=$2 RETURNING *",
        cat_id, user_id, *updates.values()
    )
    if not row:
        raise HTTPException(404)
    return dict(row)

@router.delete("/{cat_id}")
async def delete_category(cat_id: int, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    await db.execute(
        "DELETE FROM categories WHERE id=$1 AND user_id=$2",
        cat_id, user_id
    )
    return {"ok": True}
