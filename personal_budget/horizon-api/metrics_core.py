"""
metrics_core — единый источник истины для расчёта Safe-to-Spend и дневной ставки.

Используется и в routers/metrics.py (живой /api/metrics), и в forecast_cron.py
(ежедневный снапшот). Нельзя дублировать формулы r_var / STS в этих местах —
любые изменения математики делать ТОЛЬКО здесь.
"""
from datetime import date, timedelta
from calendar import monthrange
import statistics
import math

# ── Константы ─────────────────────────────────────────────────────────────────
Z_80 = 1.2816  # z для 80%-коридора

FLOW_CHARS     = ('flow', 'Повседневный')        # повседневные (variable)
EPISODIC_CHARS = ('episodic', 'Эпизодический')   # эпизодические (variable по типу, но отделены)
FIXED_CHARS    = ('fixed', 'Фиксированный')


# ── Чистые помощники ──────────────────────────────────────────────────────────
def month_context(d=None):
    today = d or date.today()
    year, month = today.year, today.month
    days_in_month = monthrange(year, month)[1]
    d_left = days_in_month - today.day
    month_start = date(year, month, 1)
    month_end = date(year, month, days_in_month)
    return today, year, month, today.day, d_left, days_in_month, month_start, month_end


def robust_rate(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) < 4:
        return statistics.mean(values)
    return statistics.median(values)


def robust_sigma(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    med = statistics.median(values)
    mad = statistics.median([abs(x - med) for x in values])
    return 1.4826 * mad


def _is_op(a: dict) -> bool:
    """Operational: Актив + include_in_balance=True, кроме подушки.
    Операционность определяется include_in_balance (флаг is_operational не используется)."""
    return (
        a["account_type"] == "Актив"
        and a.get("include_in_balance") is True
        and not a.get("is_cushion")
    )


def _is_rsv(a: dict) -> bool:
    """Настоящие резервные счета (is_reserve=True). Для s_reserve и runway-ликвидности.
    Исключает не-операционные инвестиции (Брокерский) — это капитал, но не ликвидный runway."""
    return (
        a["account_type"] == "Актив"
        and a.get("is_reserve") is True
        and not a.get("is_cushion")
    )


# ── DB-помощники ──────────────────────────────────────────────────────────────
async def account_balances(db, user_id: str) -> dict:
    rows = await db.fetch("""
        SELECT
            a.id, a.name, a.account_type,
            a.is_reserve, a.is_cushion,
            a.include_in_balance, a.initial_balance,
            a.initial_balance
            + COALESCE(SUM(CASE WHEN t.account_to   = a.name THEN t.amount ELSE 0 END), 0)
            - COALESCE(SUM(CASE WHEN t.account_from = a.name THEN t.amount ELSE 0 END), 0)
            AS balance
        FROM accounts a
        LEFT JOIN transactions t
            ON (t.account_from = a.name OR t.account_to = a.name)
            AND t.user_id = $1
        WHERE a.user_id = $1 AND a.is_active = true
        GROUP BY a.id, a.name, a.account_type,
                 a.is_reserve, a.is_cushion,
                 a.include_in_balance, a.initial_balance
    """, user_id)
    return {r["name"]: dict(r) for r in rows}


async def flow_daily_rate(db, user_id: str, today: date) -> tuple[float, float]:
    """§3.3 Робастная дневная ставка (r_var) и σ за последние 30 календарных дней.

    Нулевые дни ВКЛЮЧЕНЫ в окно (ставка = сумма_трат / 30, а не медиана дней трат).
    MAD-фильтр применяется к ненулевым значениям, убирая дни-выбросы.
    """
    cutoff = today - timedelta(days=30)
    rows = await db.fetch("""
        SELECT t.date, SUM(t.amount) AS daily_total
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE t.user_id=$1 AND t.date >= $2 AND t.date < $3
          AND t.account_to = 'Расход'
          AND c.expense_type = 'variable'
          AND c.character != 'Эпизодический'
        GROUP BY t.date ORDER BY t.date
    """, user_id, cutoff, today)

    WINDOW = 30
    daily_by_date = {r["date"]: float(r["daily_total"]) for r in rows}
    daily = [daily_by_date.get(cutoff + timedelta(days=i), 0.0) for i in range(WINDOW)]

    pos = [x for x in daily if x > 0]
    if len(pos) >= 2:
        med = statistics.median(pos)
        mad = statistics.median([abs(x - med) for x in pos])
        threshold = med + 3 * 1.4826 * mad
        daily = [x if x <= threshold else 0.0 for x in daily]

    rate = sum(daily) / WINDOW
    sigma = robust_sigma(daily)
    return rate, sigma


async def monthly_income_sum(db, user_id: str, year: int, month: int) -> float:
    val = await db.fetchval("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
        WHERE user_id=$1
          AND EXTRACT(YEAR  FROM date)=$2
          AND EXTRACT(MONTH FROM date)=$3
          AND account_from = 'Доход'
    """, user_id, year, month)
    return float(val)


async def monthly_fixed_income_sum(db, user_id: str, year: int, month: int) -> float:
    """Гарантированный (фиксированный) доход за месяц — приход по категориям с
    character='Фиксированный' (зарплата и т.п.). Используется для честного DSR:
    платежи по долгам соотносим со стабильным доходом, а не со всем приходом."""
    val = await db.fetchval("""
        SELECT COALESCE(SUM(t.amount), 0) FROM transactions t
        JOIN categories c ON t.category_id = c.id
        WHERE t.user_id=$1
          AND EXTRACT(YEAR  FROM t.date)=$2
          AND EXTRACT(MONTH FROM t.date)=$3
          AND t.account_from = 'Доход'
          AND c.character = 'Фиксированный'
    """, user_id, year, month)
    return float(val)


async def monthly_expense_sum(db, user_id: str, year: int, month: int) -> float:
    val = await db.fetchval("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
        WHERE user_id=$1
          AND EXTRACT(YEAR  FROM date)=$2
          AND EXTRACT(MONTH FROM date)=$3
          AND account_to IN ('Расход', 'Обязательства')
    """, user_id, year, month)
    return float(val)


async def plan_remaining(db, user_id: str, year: int, month: int, today: date) -> list:
    rows = await db.fetch("""
        SELECT p.date, p.amount, p.account_from, p.account_to,
               c.category AS cat_category,
               c.character AS cat_character,
               c.expense_type AS cat_expense_type
        FROM plan p
        LEFT JOIN categories c ON p.category_id = c.id
        WHERE p.user_id=$1
          AND EXTRACT(YEAR  FROM p.date)=$2
          AND EXTRACT(MONTH FROM p.date)=$3
          AND p.date > $4
    """, user_id, year, month, today)
    return [dict(r) for r in rows]


async def plan_window(db, user_id: str, after: date, until: date) -> list:
    """Плановые строки в (after, until] — для расчёта «до следующего дохода».
    Окно может выходить за границу месяца (план следующего месяца)."""
    rows = await db.fetch("""
        SELECT p.date, p.amount, p.account_from, p.account_to,
               c.category AS cat_category,
               c.character AS cat_character,
               c.expense_type AS cat_expense_type
        FROM plan p
        LEFT JOIN categories c ON p.category_id = c.id
        WHERE p.user_id=$1 AND p.date > $2 AND p.date <= $3
        ORDER BY p.date
    """, user_id, after, until)
    return [dict(r) for r in rows]


# ── Safe-to-Spend (единый расчёт для API и крона) ─────────────────────────────
async def safe_to_spend(db, user_id: str, today: date = None) -> dict:
    """Считает STS и все его компоненты. Единственное место расчёта STS.

    Возвращает словарь со всеми промежуточными величинами, чтобы вызывающий
    (get_metrics) мог продолжить расчёт остальных метрик без повторных запросов.
    """
    today, year, month, d_now, d_left, days_in_month, month_start, month_end = month_context(today)

    # ── Балансы ───────────────────────────────────────────────────────────────
    accs = await account_balances(db, user_id)

    b0_accounts = [
        {"name": a["name"], "balance": round(float(a["balance"])),
         "type": a["account_type"]}
        for a in accs.values() if _is_op(a)
    ]
    B0 = sum(a["balance"] for a in b0_accounts)
    C_cushion = sum(float(a["balance"]) for a in accs.values() if a.get("is_cushion"))
    reserve_balance = sum(float(a["balance"]) for a in accs.values() if _is_rsv(a))
    liabilities = sum(
        abs(float(a["balance"])) for a in accs.values() if a["account_type"] == "Пассив"
    )

    # ── Дневная ставка ─────────────────────────────────────────────────────────
    r_var, sigma_day = await flow_daily_rate(db, user_id, today)
    V_remain = r_var * d_left
    sigma_remain = sigma_day * math.sqrt(d_left) if d_left > 0 else 0.0

    # ── Остаток плана ──────────────────────────────────────────────────────────
    plan_rows = await plan_remaining(db, user_id, year, month, today)
    I_remain = sum(float(r["amount"]) for r in plan_rows if r.get("account_from") == "Доход")
    F_remain = sum(
        float(r["amount"]) for r in plan_rows
        if r.get("account_to") == "Расход" and (
            r.get("cat_expense_type") == "fixed"
            or r.get("cat_character") in EPISODIC_CHARS
        )
    )
    F_remain += sum(float(r["amount"]) for r in plan_rows if r.get("account_to") == "Обязательства")

    reserve_names = {name for name, a in accs.items() if a.get("is_reserve") is True}
    R_topup = sum(
        float(r["amount"]) for r in plan_rows
        if r.get("account_to") in reserve_names
    )

    # ── Свободно «до следующего дохода» (trough) ───────────────────────────────
    # Между «сегодня» и следующим плановым доходом доходов нет → баланс только
    # убывает, низшая точка — перед самой зарплатой. Поэтому:
    #   Свободно = B0 − (плановые оттоки до даты дохода) − r_var × дней_до_дохода.
    # Доход — граница окна, в сумму НЕ входит. Окно может выходить за месяц.
    # Подушка уже вне B0 (через _is_op), повторно не вычитаем.
    horizon = today + timedelta(days=75)
    fut_rows = await plan_window(db, user_id, today, horizon)
    incomes = [r for r in fut_rows if r.get("account_from") == "Доход"]
    next_income_date = min((r["date"] for r in incomes), default=month_end)
    days_to_income = max((next_income_date - today).days, 0)
    win = [r for r in fut_rows if r["date"] < next_income_date]

    F_before = sum(
        float(r["amount"]) for r in win
        if r.get("account_to") == "Расход" and (
            r.get("cat_expense_type") == "fixed"
            or r.get("cat_character") in EPISODIC_CHARS
        )
    )
    F_before += sum(float(r["amount"]) for r in win if r.get("account_to") == "Обязательства")
    R_before = sum(float(r["amount"]) for r in win if r.get("account_to") in reserve_names)
    V_to_income = r_var * days_to_income
    sigma_to_income = sigma_day * math.sqrt(days_to_income) if days_to_income > 0 else 0.0

    sts = B0 - F_before - V_to_income - R_before
    sts_low  = sts - Z_80 * sigma_to_income
    sts_high = sts + Z_80 * sigma_to_income

    buffer = sts / max(V_to_income, 1)
    if sts < 0:
        sts_status = "red"
    elif buffer < 0.15:
        sts_status = "yellow"
    else:
        sts_status = "green"

    return {
        "today": today, "year": year, "month": month,
        "d_now": d_now, "d_left": d_left, "days_in_month": days_in_month,
        "month_start": month_start, "month_end": month_end,
        "accs": accs, "b0_accounts": b0_accounts,
        "B0": B0, "C_cushion": C_cushion,
        "reserve_balance": reserve_balance, "liabilities": liabilities,
        "r_var": r_var, "sigma_day": sigma_day,
        "V_remain": V_remain, "sigma_remain": sigma_remain,
        "plan_rows": plan_rows, "reserve_names": reserve_names,
        "I_remain": I_remain, "F_remain": F_remain, "R_topup": R_topup,
        # trough «до следующего дохода»
        "next_income_date": next_income_date, "days_to_income": days_to_income,
        "F_before": F_before, "V_to_income": V_to_income, "R_before": R_before,
        "sts": sts, "sts_low": sts_low, "sts_high": sts_high, "sts_status": sts_status,
    }
