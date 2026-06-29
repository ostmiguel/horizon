from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from datetime import date

router = APIRouter(prefix="/api/category-budgets", tags=["category_budgets"])


class BudgetUpsert(BaseModel):
    category: str
    year: int
    month: int
    budget: float


# ── Подсказки сумм: окно от целевого месяца + среднее со свежестью ────────────
def _recency_weighted_mean(values: list[float]) -> float:
    """Среднее со свежестью: чем свежее месяц — тем больше вес.

    `values` — суммы по месяцам от самого СВЕЖЕГО к старому; нулевые месяцы
    (нет данных) пропускаем. Вес = ранг по свежести среди непустых месяцев
    (самый свежий — наибольший). Не выбрасывает пики и не теряет свежий месяц.
    """
    nz = [v for v in values if v > 0]   # порядок сохранён: свежие первыми
    if not nz:
        return 0.0
    n = len(nz)
    weights = [n - k for k in range(n)]   # [n, n-1, …, 1]
    return sum(v * w for v, w in zip(nz, weights)) / sum(weights)


async def _category_monthly_totals(db, user_id, category: str, max_months: int = 6,
                                   year: int | None = None, month: int | None = None) -> list[float]:
    """Суммы повседневных трат по категории за max_months месяцев ДО целевого месяца.

    Целевой месяц = (year, month), если заданы (месяц бюджета, который заводим);
    иначе — текущий месяц по дате сервера. Возвращает список от самого свежего
    месяца к старому (для взвешивания по свежести).
    """
    if year and month:
        a_y, a_m = int(year), int(month)
    else:
        t = date.today(); a_y, a_m = t.year, t.month
    totals = []
    for i in range(1, max_months + 1):
        m, y = a_m - i, a_y
        while m <= 0:
            m += 12; y -= 1
        v = await db.fetchval("""
            SELECT COALESCE(SUM(t.amount), 0)
            FROM transactions t JOIN categories c ON t.category_id = c.id
            WHERE t.user_id=$1 AND c.category=$2 AND t.account_to='Расход'
              AND c.expense_type='variable' AND c.character != 'Эпизодический'
              AND EXTRACT(YEAR FROM t.date)=$3 AND EXTRACT(MONTH FROM t.date)=$4
        """, user_id, category, y, m)
        totals.append(float(v))
    return totals


@router.get("/suggest")
async def suggest_budget(request: Request, category: str = Query(...),
                         year: int | None = None, month: int | None = None):
    """Подсказка суммы конверта = среднее со свежестью за месяцы до целевого месяца."""
    user_id = request.state.user_id
    db = request.state.db
    totals = await _category_monthly_totals(db, user_id, category, year=year, month=month)
    nz = [t for t in totals if t > 0]
    return {"category": category, "suggested": round(_recency_weighted_mean(totals)),
            "history": [round(t) for t in nz], "months": len(nz)}


@router.get("/suggestions")
async def suggestions(request: Request, year: int = Query(...), month: int = Query(...)):
    """Предложенные конверты на месяц по всем повседневным категориям с историей."""
    user_id = request.state.user_id
    db = request.state.db
    cats = await db.fetch("""
        SELECT DISTINCT c.category
        FROM transactions t JOIN categories c ON t.category_id = c.id
        WHERE t.user_id=$1 AND t.account_to='Расход'
          AND c.expense_type='variable' AND c.character != 'Эпизодический'
          AND t.date >= (CURRENT_DATE - INTERVAL '6 months')
    """, user_id)
    result = []
    for r in cats:
        cat = r["category"]
        totals = await _category_monthly_totals(db, user_id, cat, year=year, month=month)
        nz = [t for t in totals if t > 0]
        if not nz:
            continue
        suggested = round(_recency_weighted_mean(totals))
        if suggested <= 0:
            continue
        result.append({"category": cat, "suggested": suggested,
                       "history": [round(t) for t in nz], "months": len(nz)})
    result.sort(key=lambda x: -x["suggested"])
    return {"year": year, "month": month, "suggestions": result}


@router.get("")
async def get_category_budgets(request: Request,
                               year: Optional[int] = None, month: Optional[int] = None):
    user_id = request.state.user_id
    db = request.state.db
    conditions = ["user_id = $1"]
    params = [user_id]
    i = 2
    if year:
        conditions.append(f"year = ${i}"); params.append(year); i += 1
    if month:
        conditions.append(f"month = ${i}"); params.append(month); i += 1
    rows = await db.fetch(f"""
        SELECT id, category, year, month, budget
        FROM category_budgets
        WHERE {' AND '.join(conditions)}
        ORDER BY category
    """, *params)
    return [dict(r) for r in rows]


@router.put("")
async def upsert_budget(data: BudgetUpsert, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    row = await db.fetchrow("""
        INSERT INTO category_budgets (user_id, category, year, month, budget)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (user_id, category, year, month)
        DO UPDATE SET budget = EXCLUDED.budget
        RETURNING id, category, year, month, budget
    """, user_id, data.category, data.year, data.month, data.budget)
    return dict(row)


@router.delete("/{budget_id}")
async def delete_budget(budget_id: int, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    await db.execute("DELETE FROM category_budgets WHERE id=$1 AND user_id=$2", budget_id, user_id)
    return {"ok": True}
