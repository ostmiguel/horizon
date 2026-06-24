from datetime import date
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from plan_materialize import materialize_rules, current_and_next_month

router = APIRouter(prefix="/api/plan-rules", tags=["plan_rules"])


async def _materialize_current_next(db, user_id):
    """Материализовать активные правила на текущий и следующий месяц."""
    for y, m in current_and_next_month():
        await materialize_rules(db, user_id, y, m)


class PlanRuleCreate(BaseModel):
    name: str
    amount: float
    account_from: str
    account_to: str
    category_id: Optional[int] = None
    day_of_month: Optional[int] = None


class PlanRuleUpdate(BaseModel):
    name: Optional[str] = None
    amount: Optional[float] = None
    account_from: Optional[str] = None
    account_to: Optional[str] = None
    category_id: Optional[int] = None
    day_of_month: Optional[int] = None
    is_active: Optional[bool] = None


@router.get("")
async def get_plan_rules(request: Request):
    user_id = request.state.user_id
    db = request.state.db
    rows = await db.fetch("""
        SELECT pr.*,
               c.subcategory, c.category, c.group_name,
               c.expense_type, c.character
        FROM plan_rules pr
        LEFT JOIN categories c ON pr.category_id = c.id
        WHERE pr.user_id = $1
        ORDER BY pr.day_of_month NULLS LAST, pr.id
    """, user_id)
    result = []
    for r in rows:
        d = dict(r)
        if d.get("subcategory"):
            d["categories"] = {
                "subcategory":  d.pop("subcategory"),
                "category":     d.pop("category"),
                "group_name":   d.pop("group_name"),
                "expense_type": d.pop("expense_type"),
                "character":    d.pop("character"),
            }
        else:
            for k in ["subcategory", "category", "group_name", "expense_type", "character"]:
                d.pop(k, None)
            d["categories"] = None
        result.append(d)
    return result


@router.post("")
async def create_plan_rule(data: PlanRuleCreate, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    row = await db.fetchrow("""
        INSERT INTO plan_rules (user_id, name, amount, account_from, account_to, category_id, day_of_month)
        VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *
    """, user_id, data.name, data.amount, data.account_from, data.account_to,
        data.category_id, data.day_of_month)
    await _materialize_current_next(db, user_id)
    return dict(row)


@router.patch("/{rule_id}")
async def update_plan_rule(rule_id: int, data: PlanRuleUpdate, request: Request,
                           clear_from_year: int = Query(None),
                           clear_from_month: int = Query(None)):
    """Правка правила. Если переданы clear_from_year/month (правка «все будущие»),
    снимаем ручные правки/пропуски этого правила начиная с указанного месяца —
    чтобы новое значение применилось к текущему и будущим месяцам."""
    user_id = request.state.user_id
    db = request.state.db
    updates = data.dict(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    sets = ", ".join(f"{k} = ${i+3}" for i, k in enumerate(updates))
    row = await db.fetchrow(
        f"UPDATE plan_rules SET {sets} WHERE id=$1 AND user_id=$2 RETURNING *",
        rule_id, user_id, *updates.values()
    )
    if not row:
        raise HTTPException(404, "Rule not found")

    if clear_from_year and clear_from_month:
        start = date(clear_from_year, clear_from_month, 1)
        # снять ручные правки (pinned) этого правила с указанного месяца и далее
        await db.execute("""
            DELETE FROM plan
            WHERE user_id=$1 AND source_rule_id=$2 AND pinned=true AND date >= $3
        """, user_id, rule_id, start)
        # снять «удалено в этом месяце» с указанного месяца и далее
        await db.execute("""
            DELETE FROM plan_rule_skips
            WHERE user_id=$1 AND rule_id=$2
              AND (year > $3 OR (year = $3 AND month >= $4))
        """, user_id, rule_id, clear_from_year, clear_from_month)

    await _materialize_current_next(db, user_id)
    return dict(row)


class RuleMonthOverride(BaseModel):
    year: int
    month: int
    amount: float
    account_from: str
    account_to: str
    category_id: Optional[int] = None
    note: Optional[str] = None
    day_of_month: Optional[int] = None


@router.post("/{rule_id}/set-month")
async def set_rule_month(rule_id: int, data: RuleMonthOverride, request: Request):
    """«Изменить только этот месяц»: создаёт/заменяет закреплённую (pinned) плановую
    строку правила на (год, месяц). Работает и для будущих месяцев, где правило ещё
    не материализовано. pinned → перештамповка эту строку не трогает."""
    from calendar import monthrange
    user_id = request.state.user_id
    db = request.state.db
    rule = await db.fetchrow("SELECT id FROM plan_rules WHERE id=$1 AND user_id=$2", rule_id, user_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    dim = monthrange(data.year, data.month)[1]
    day = max(1, min(int(data.day_of_month) if data.day_of_month else 1, dim))
    d = date(data.year, data.month, day)
    async with db.transaction():
        await db.execute("""
            DELETE FROM plan WHERE user_id=$1 AND source_rule_id=$2
              AND EXTRACT(YEAR FROM date)=$3 AND EXTRACT(MONTH FROM date)=$4
        """, user_id, rule_id, data.year, data.month)
        await db.execute("""
            DELETE FROM plan_rule_skips WHERE user_id=$1 AND rule_id=$2 AND year=$3 AND month=$4
        """, user_id, rule_id, data.year, data.month)
        row = await db.fetchrow("""
            INSERT INTO plan
              (user_id, date, amount, account_from, account_to, category_id, note, source, source_rule_id, pinned)
            VALUES ($1,$2,$3,$4,$5,$6,$7,'plan_rule',$8,true) RETURNING *
        """, user_id, d, data.amount, data.account_from, data.account_to,
            data.category_id, data.note, rule_id)
    return dict(row)


@router.post("/{rule_id}/skip-month")
async def skip_rule_month(rule_id: int, request: Request,
                          year: int = Query(...), month: int = Query(...)):
    """«Удалить только этот месяц»: помечаем (правило, год, месяц) как пропуск и
    убираем материализованную строку этого месяца. Правило и другие месяцы целы;
    перештамповка эту строку больше не воссоздаёт."""
    user_id = request.state.user_id
    db = request.state.db
    rule = await db.fetchrow("SELECT id FROM plan_rules WHERE id=$1 AND user_id=$2", rule_id, user_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    await db.execute("""
        INSERT INTO plan_rule_skips (user_id, rule_id, year, month)
        VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING
    """, user_id, rule_id, year, month)
    await db.execute("""
        DELETE FROM plan
        WHERE user_id=$1 AND source_rule_id=$2
          AND EXTRACT(YEAR FROM date)=$3 AND EXTRACT(MONTH FROM date)=$4
    """, user_id, rule_id, year, month)
    return {"ok": True}


@router.delete("/{rule_id}")
async def delete_plan_rule(rule_id: int, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    # ON DELETE CASCADE удалит материализованные строки plan этого правила
    await db.execute("DELETE FROM plan_rules WHERE id=$1 AND user_id=$2", rule_id, user_id)
    return {"ok": True}


@router.post("/materialize")
async def materialize(request: Request,
                      year: int = Query(...), month: int = Query(...)):
    """Ручной прогон материализации правил в plan на указанный месяц."""
    user_id = request.state.user_id
    db = request.state.db
    n = await materialize_rules(db, user_id, year, month)
    return {"ok": True, "created": n, "year": year, "month": month}
