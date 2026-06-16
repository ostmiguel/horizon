from fastapi import APIRouter, Request
from datetime import date, timedelta
from calendar import monthrange
import statistics
import math

router = APIRouter(prefix="/api/metrics", tags=["metrics"])

Z_80 = 1.2816


# ── Helpers ───────────────────────────────────────────────────────────────────

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
    """Operational: Актив + include_in_balance=True.
    Ignores is_operational flag — it is set to True for virtual accounts (Доход, Расход, Обязательства)."""
    return (
        a["account_type"] == "Актив"
        and a.get("include_in_balance") is True
        and not a.get("is_cushion")
    )


def _is_rsv(a: dict) -> bool:
    """True reserve accounts only (is_reserve=True). Used for s_reserve and runway liquid.
    Excludes non-operational investments (Брокерский) — those are capital but not liquid runway."""
    return (
        a["account_type"] == "Актив"
        and a.get("is_reserve") is True
        and not a.get("is_cushion")
    )


# ── DB helpers ────────────────────────────────────────────────────────────────

async def account_balances(db, user_id: str) -> dict:
    rows = await db.fetch("""
        SELECT
            a.id, a.name, a.account_type,
            a.is_reserve, a.is_operational, a.is_cushion,
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
                 a.is_reserve, a.is_operational, a.is_cushion,
                 a.include_in_balance, a.initial_balance
    """, user_id)
    return {r["name"]: dict(r) for r in rows}


FLOW_CHARS = ('flow', 'Повседневный')       # supported character values for flow
EPISODIC_CHARS = ('episodic', 'Эпизодический')
FIXED_CHARS = ('fixed', 'Фиксированный')


async def flow_daily_rate(db, user_id: str, today: date) -> tuple[float, float]:
    """§3.3 Robust daily rate (r_var) and σ from last 30 calendar days.

    Key fix vs. previous version: include zero-spend days in the window so
    the rate is total_spending / 30, not median(spending-day amounts).
    MAD filter is applied to non-zero values only to remove outlier days
    (e.g. one-off large purchases in an otherwise flow category).
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
    # Full 30-day series including zero-spend days
    daily = [daily_by_date.get(cutoff + timedelta(days=i), 0.0) for i in range(WINDOW)]

    # MAD outlier filter on positive values only (zeros are valid, not outliers)
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


# ── /api/metrics ──────────────────────────────────────────────────────────────

@router.get("")
async def get_metrics(request: Request):
    user_id = request.state.user_id
    db = request.state.db

    today, year, month, d_now, d_left, days_in_month, month_start, month_end = month_context()

    # ── Balances ──────────────────────────────────────────────────────────────
    accs = await account_balances(db, user_id)

    b0_accounts = [
        {"name": a["name"], "balance": round(float(a["balance"])),
         "type": a["account_type"], "include_in_balance": a.get("include_in_balance"),
         "is_operational": a.get("is_operational"), "is_active": a.get("is_active")}
        for a in accs.values() if _is_op(a)
    ]
    B0 = sum(a["balance"] for a in b0_accounts)
    C_cushion = sum(float(a["balance"]) for a in accs.values() if a.get("is_cushion"))
    reserve_balance = sum(float(a["balance"]) for a in accs.values() if _is_rsv(a))
    liabilities = sum(
        abs(float(a["balance"])) for a in accs.values() if a["account_type"] == "Пассив"
    )

    # ── Flow rate ─────────────────────────────────────────────────────────────
    r_var, sigma_day = await flow_daily_rate(db, user_id, today)
    V_remain = r_var * d_left
    sigma_remain = sigma_day * math.sqrt(d_left) if d_left > 0 else 0.0

    # ── Plan remaining ────────────────────────────────────────────────────────
    plan_rows = await plan_remaining(db, user_id, year, month, today)
    I_remain = sum(float(r["amount"]) for r in plan_rows if r.get("account_from") == "Доход")
    plan_expenses = [r for r in plan_rows if r.get("account_to") == "Расход"]
    # F_remain = fixed plan expenses (expense_type='fixed') + episodic (character='Эпизодический')
    # Variable everyday (expense_type='variable', character!='Эпизодический') → covered by V_remain
    F_remain = sum(
        float(r["amount"]) for r in plan_expenses
        if r.get("cat_expense_type") == "fixed"
        or r.get("cat_character") in EPISODIC_CHARS
    )
    F_remain += sum(float(r["amount"]) for r in plan_rows if r.get("account_to") == "Обязательства")

    # §4.1 R_topup: плановые пополнения резерва в оставшиеся дни месяца
    reserve_names = {name for name, a in accs.items() if a.get("is_reserve") is True}
    R_topup = sum(
        float(r["amount"]) for r in plan_rows
        if r.get("account_to") in reserve_names
    )

    # ── §4.1 Safe to spend ────────────────────────────────────────────────────
    sts = B0 + I_remain - F_remain - V_remain - R_topup - C_cushion
    sts_low  = sts - Z_80 * sigma_remain
    sts_high = sts + Z_80 * sigma_remain

    buffer = sts / max(V_remain, 1)
    if sts < 0:
        sts_status = "red"
    elif buffer < 0.15:
        sts_status = "yellow"
    else:
        sts_status = "green"

    # ── §4.3 Net capital + Δ% ────────────────────────────────────────────────
    total_assets = sum(float(a["balance"]) for a in accs.values() if a["account_type"] == "Актив")
    net_capital = total_assets - liabilities

    # Δ% = (капитал_сегодня − капитал_месяц_назад) / |капитал_месяц_назад|
    month_ago = today - timedelta(days=30)
    assets_30d = await db.fetchval("""
        SELECT COALESCE(SUM(
            a.initial_balance
            + COALESCE((SELECT SUM(t.amount) FROM transactions t
               WHERE (t.account_to=a.name) AND t.user_id=$1 AND t.date <= $2), 0)
            - COALESCE((SELECT SUM(t.amount) FROM transactions t
               WHERE (t.account_from=a.name) AND t.user_id=$1 AND t.date <= $2), 0)
        ), 0)
        FROM accounts a
        WHERE a.user_id=$1 AND a.is_active=true AND a.account_type='Актив'
    """, user_id, month_ago)
    liabilities_30d = await db.fetchval("""
        SELECT COALESCE(SUM(ABS(
            a.initial_balance
            + COALESCE((SELECT SUM(t.amount) FROM transactions t
               WHERE (t.account_to=a.name) AND t.user_id=$1 AND t.date <= $2), 0)
            - COALESCE((SELECT SUM(t.amount) FROM transactions t
               WHERE (t.account_from=a.name) AND t.user_id=$1 AND t.date <= $2), 0)
        )), 0)
        FROM accounts a
        WHERE a.user_id=$1 AND a.is_active=true AND a.account_type='Пассив'
    """, user_id, month_ago)
    net_capital_30d = float(assets_30d) - float(liabilities_30d)
    if net_capital_30d != 0:
        net_capital_delta_pct = round((net_capital - net_capital_30d) / abs(net_capital_30d) * 100, 1)
    else:
        net_capital_delta_pct = None

    # ── §4.4 DSR ──────────────────────────────────────────────────────────────
    monthly_payments = float(await db.fetchval("""
        SELECT COALESCE(SUM(monthly_payment), 0) FROM loans WHERE user_id=$1 AND is_active=true
    """, user_id))

    cur_income = await monthly_income_sum(db, user_id, year, month)
    if cur_income == 0:
        prev_incomes = []
        for i in range(1, 4):
            m, y = month - i, year
            if m <= 0:
                m += 12; y -= 1
            prev_incomes.append(await monthly_income_sum(db, user_id, y, m))
        cur_income = statistics.mean(prev_incomes) if prev_incomes else 1

    dsr = monthly_payments / cur_income if cur_income > 0 else 0.0
    if dsr < 0.30:
        dsr_status = "green"
    elif dsr <= 0.45:
        dsr_status = "yellow"
    else:
        dsr_status = "red"

    # ── §4.7 Runway (behavioral + planned) ───────────────────────────────────
    liquid = B0 + reserve_balance

    behavioral_exps = []
    for i in range(1, 4):
        m, y = month - i, year
        if m <= 0:
            m += 12; y -= 1
        behavioral_exps.append(await monthly_expense_sum(db, user_id, y, m))
    avg_exp = statistics.mean(behavioral_exps) if behavioral_exps else 1
    behavioral_runway = max(liquid / avg_exp if avg_exp > 0 else 99.0, 0.0)

    # Плановый runway: liquid / (F_remain_month_full + V_plan_month)
    # F_remain_month_full = все плановые обязательные за месяц (не только остаток)
    plan_month_all = await db.fetch("""
        SELECT p.account_to, c.expense_type AS cat_expense_type, c.character AS cat_character,
               SUM(p.amount) AS total
        FROM plan p
        LEFT JOIN categories c ON p.category_id = c.id
        WHERE p.user_id=$1
          AND EXTRACT(YEAR FROM p.date)=$2 AND EXTRACT(MONTH FROM p.date)=$3
        GROUP BY p.account_to, c.expense_type, c.character
    """, user_id, year, month)
    plan_fixed_month = sum(
        float(r["total"]) for r in plan_month_all
        if r["account_to"] == "Обязательства"
        or r["cat_expense_type"] == "fixed"
        or r["cat_character"] == "Эпизодический"
    )
    plan_variable_month = sum(
        float(r["total"]) for r in plan_month_all
        if r["account_to"] == "Расход"
        and r["cat_expense_type"] == "variable"
        and r["cat_character"] != "Эпизодический"
    )
    e_plan = plan_fixed_month + plan_variable_month
    planned_runway = max(liquid / e_plan if e_plan > 0 else 99.0, 0.0)

    if behavioral_runway >= 6:
        runway_status = "green"
    elif behavioral_runway >= 3:
        runway_status = "yellow"
    else:
        runway_status = "red"

    # ── §4.5 Resilience ───────────────────────────────────────────────────────
    monthly_fixed_exp = float(await db.fetchval("""
        SELECT COALESCE(SUM(t.amount), 0)
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE t.user_id=$1
          AND EXTRACT(YEAR  FROM t.date)=$2
          AND EXTRACT(MONTH FROM t.date)=$3
          AND t.account_to = 'Расход'
          AND c.expense_type = 'fixed'
    """, user_id, year, month))

    cur_expenses = float(await db.fetchval("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
        WHERE user_id=$1
          AND EXTRACT(YEAR  FROM date)=$2
          AND EXTRACT(MONTH FROM date)=$3
          AND account_to IN ('Расход', 'Обязательства')
    """, user_id, year, month))

    savings_rate = max((cur_income - cur_expenses) / max(cur_income, 1), 0)

    recent_incomes = []
    for i in range(1, 7):
        m, y = month - i, year
        if m <= 0:
            m += 12; y -= 1
        inc = await monthly_income_sum(db, user_id, y, m)
        if inc > 0:
            recent_incomes.append(inc)

    if len(recent_incomes) >= 2:
        cv = statistics.stdev(recent_incomes) / statistics.mean(recent_incomes)
        s_income = 1 - min(cv / 0.50, 1.0)
    else:
        s_income = 0.5

    # §4.5 formula: s_reserve = резерв / (3 × обязательные_мес)
    # обязательные = фикс расходы + платежи по долгам (monthly_payments already computed above)
    monthly_obligations = monthly_fixed_exp + monthly_payments
    s_runway  = min(behavioral_runway / 6, 1.0)
    s_reserve = min(reserve_balance / max(3 * monthly_obligations, 1), 1.0)
    s_debt    = 1 - min(dsr / 0.40, 1.0)
    s_savings = min(savings_rate / 0.20, 1.0)

    resilience = 10 * (
        0.30 * s_runway +
        0.20 * s_reserve +
        0.20 * s_debt +
        0.15 * s_savings +
        0.15 * s_income
    )

    if resilience >= 7.0:
        resilience_status = "green"
    elif resilience >= 4.0:
        resilience_status = "yellow"
    else:
        resilience_status = "red"

    # ── §4.6 Categories ───────────────────────────────────────────────────────
    cat_rows = await db.fetch("""
        SELECT c.category, c.character AS cat_char, c.expense_type AS cat_expense_type,
               SUM(t.amount) AS total
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        WHERE t.user_id=$1
          AND EXTRACT(YEAR  FROM t.date)=$2
          AND EXTRACT(MONTH FROM t.date)=$3
          AND t.account_to = 'Расход'
        GROUP BY c.category, c.character, c.expense_type
        ORDER BY total DESC
    """, user_id, year, month)

    # Track which categories already have fact data
    seen_cat_chars: set[tuple] = set()
    categories = []
    CAT_WINDOW = 30

    for r in cat_rows:
        fact = float(r["total"])
        is_variable_everyday = (
            r["cat_expense_type"] == "variable"
            and r["cat_char"] not in EPISODIC_CHARS
        )
        if is_variable_everyday and d_left > 0:
            # §3.3 fix: rate = total_in_window / window_days (not median of spending-days)
            # MAD filter removes outlier days before summing.
            cat_cutoff = today - timedelta(days=CAT_WINDOW)
            cat_daily_rows = await db.fetch("""
                SELECT t.date, SUM(t.amount) AS dt
                FROM transactions t
                JOIN categories c ON t.category_id = c.id
                WHERE t.user_id=$1 AND t.date >= $2 AND t.date < $3
                  AND c.category=$4 AND c.character NOT IN ('Эпизодический','episodic')
                  AND t.account_to = 'Расход'
                GROUP BY t.date
            """, user_id, cat_cutoff, today, r["category"])
            daily_by_date = {row["date"]: float(row["dt"]) for row in cat_daily_rows}
            cat_daily = [
                daily_by_date.get(cat_cutoff + timedelta(days=i), 0.0)
                for i in range(CAT_WINDOW)
            ]
            # MAD outlier filter (same logic as flow_daily_rate)
            pos = [x for x in cat_daily if x > 0]
            if len(pos) >= 2:
                med = statistics.median(pos)
                mad = statistics.median([abs(x - med) for x in pos])
                threshold = med + 3 * 1.4826 * mad
                cat_daily = [x if x <= threshold else 0.0 for x in cat_daily]
            cat_rate = sum(cat_daily) / CAT_WINDOW
            forecast = fact + cat_rate * d_left
        elif r["cat_expense_type"] == "fixed":
            # Fixed: show plan amount for the month (max of fact vs plan)
            plan_cat_total = await db.fetchval("""
                SELECT COALESCE(SUM(p.amount), 0)
                FROM plan p
                JOIN categories c ON p.category_id = c.id
                WHERE p.user_id=$1
                  AND EXTRACT(YEAR  FROM p.date)=$2
                  AND EXTRACT(MONTH FROM p.date)=$3
                  AND c.category=$4
                  AND p.account_to = 'Расход'
            """, user_id, year, month, r["category"])
            forecast = max(fact, float(plan_cat_total or 0))
        else:
            # Episodic: show only what was actually spent
            forecast = fact

        seen_cat_chars.add((r["category"], r["cat_char"]))
        categories.append({
            "category":        r["category"],
            "character":       r["cat_char"],
            "expense_type":    r["cat_expense_type"],
            "amount_fact":     round(fact),
            "amount_forecast": round(forecast),
        })

    # Add fixed categories that have plan entries but no fact yet this month
    plan_fixed_cats = await db.fetch("""
        SELECT c.category, c.character AS cat_char, c.expense_type AS cat_expense_type,
               SUM(p.amount) AS plan_total
        FROM plan p
        JOIN categories c ON p.category_id = c.id
        WHERE p.user_id=$1
          AND EXTRACT(YEAR  FROM p.date)=$2
          AND EXTRACT(MONTH FROM p.date)=$3
          AND p.account_to = 'Расход'
          AND c.expense_type = 'fixed'
        GROUP BY c.category, c.character, c.expense_type
    """, user_id, year, month)
    for r in plan_fixed_cats:
        if (r["category"], r["cat_char"]) not in seen_cat_chars:
            categories.append({
                "category":        r["category"],
                "character":       r["cat_char"],
                "expense_type":    "fixed",
                "amount_fact":     0,
                "amount_forecast": round(float(r["plan_total"])),
            })

    # ── Flow fact this month ──────────────────────────────────────────────────
    flow_fact = float(await db.fetchval("""
        SELECT COALESCE(SUM(t.amount), 0)
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE t.user_id=$1
          AND EXTRACT(YEAR  FROM t.date)=$2
          AND EXTRACT(MONTH FROM t.date)=$3
          AND t.account_to = 'Расход'
          AND c.expense_type = 'variable'
          AND c.character != 'Эпизодический'
    """, user_id, year, month))

    # ── Waterfall detail (pill breakdowns) ───────────────────────────────────
    income_by_cat: dict[str, float] = {}
    for r in plan_rows:
        if r.get("account_from") == "Доход":
            cat = r.get("cat_category") or "Доходы"
            income_by_cat[cat] = income_by_cat.get(cat, 0) + float(r["amount"])
    income_items = [{"category": k, "amount": round(v)}
                    for k, v in sorted(income_by_cat.items(), key=lambda x: -x[1])]

    fixed_by_cat: dict[str, float] = {}
    for r in plan_rows:
        if r.get("account_to") == "Расход" and (
            r.get("cat_expense_type") == "fixed"
            or r.get("cat_character") in EPISODIC_CHARS
        ):
            cat = r.get("cat_category") or "Расходы"
            fixed_by_cat[cat] = fixed_by_cat.get(cat, 0) + float(r["amount"])
        elif r.get("account_to") == "Обязательства":
            cat = r.get("cat_category") or "Обязательства"
            fixed_by_cat[cat] = fixed_by_cat.get(cat, 0) + float(r["amount"])
    fixed_items = [{"category": k, "amount": round(v)}
                   for k, v in sorted(fixed_by_cat.items(), key=lambda x: -x[1])]

    variable_items = sorted(
        [{"category": c["category"], "amount": c["amount_fact"]}
         for c in categories if c.get("expense_type") == "variable" and c.get("amount_fact", 0) > 0],
        key=lambda x: -x["amount"]
    )[:5]

    # ── Response ──────────────────────────────────────────────────────────────
    return {
        "as_of": today.isoformat(),
        "safe_to_spend": {
            "value":   round(sts),
            "low":     round(sts_low),
            "high":    round(sts_high),
            "status":  sts_status,
            "waterfall": {
                "b0":        round(B0),
                "i_remain":  round(I_remain),
                "f_remain":  round(F_remain),
                "v_remain":  round(V_remain),
                "r_topup":   round(R_topup),
                "c_cushion": round(C_cushion),
            },
            "b0_accounts": b0_accounts,
            "waterfall_detail": {
                "income_items":   income_items,
                "fixed_items":    fixed_items,
                "variable_items": variable_items,
                "v_daily_rate":   round(r_var),
                "d_left":         d_left,
            },
        },
        "net_capital": {
            "value":     round(net_capital),
            "delta_pct": net_capital_delta_pct,
            "status":    "green" if net_capital >= 0 else "red",
            "delta_status": (
                "green" if net_capital_delta_pct is not None and net_capital_delta_pct >= 0
                else "red" if net_capital_delta_pct is not None
                else None
            ),
        },
        "dsr": {
            "value":            round(dsr * 100, 1),
            "monthly_payments": round(monthly_payments),
            "status":           dsr_status,
        },
        "runway": {
            "behavioral_months": round(behavioral_runway, 1),
            "planned_months":    round(planned_runway, 1),
            "status":            runway_status,
        },
        "resilience": {
            "value":  round(resilience, 1),
            "status": resilience_status,
            "components": {
                "s_runway":  round(s_runway,  2),
                "s_reserve": round(s_reserve, 2),
                "s_debt":    round(s_debt,    2),
                "s_savings": round(s_savings, 2),
                "s_income":  round(s_income,  2),
            },
        },
        "flow_rate": {
            "daily":               round(r_var),
            "daily_sigma":         round(sigma_day),
            "flow_fact_month":     round(flow_fact),
            "flow_forecast_month": round(flow_fact + V_remain),
        },
        "categories": categories,
    }


# ── /api/metrics/forecast — balance trajectory for chart ─────────────────────

@router.get("/forecast")
async def get_forecast(request: Request):
    user_id = request.state.user_id
    db = request.state.db

    today, year, month, d_now, d_left, days_in_month, month_start, month_end = month_context()

    accs = await account_balances(db, user_id)
    op_names = [a["name"] for a in accs.values() if _is_op(a)]

    B0_now = sum(float(a["balance"]) for a in accs.values() if _is_op(a))

    if op_names:
        daily_fact = await db.fetch("""
            SELECT date,
                   SUM(CASE WHEN account_to   = ANY($4::text[]) THEN amount ELSE 0 END)
                 - SUM(CASE WHEN account_from = ANY($4::text[]) THEN amount ELSE 0 END)
                   AS net
            FROM transactions
            WHERE user_id=$1
              AND EXTRACT(YEAR FROM date)=$2 AND EXTRACT(MONTH FROM date)=$3
            GROUP BY date ORDER BY date
        """, user_id, year, month, op_names)
    else:
        daily_fact = []

    fact_by_day = {r["date"].day: float(r["net"]) for r in daily_fact}

    month_net_so_far = sum(fact_by_day.get(d, 0) for d in range(1, d_now + 1))
    B_month_start = B0_now - month_net_so_far

    r_var, sigma_day = await flow_daily_rate(db, user_id, today)

    plan_rows = await plan_remaining(db, user_id, year, month, today)

    # plan_by_day_all  → plan line (все плановые операции, как задумал пользователь)
    # plan_by_day_fixed → forecast line (только фикс/долги/эпизод — переменные уже в r_var)
    plan_by_day_all:   dict[int, float] = {}
    plan_by_day_fixed: dict[int, float] = {}
    for r in plan_rows:
        day    = r["date"].day
        amount = float(r["amount"])
        af     = r.get("account_from", "")
        at     = r.get("account_to", "")
        cat_et = r.get("cat_expense_type")
        cat_ch = r.get("cat_character", "")

        is_variable_flow = (
            at == "Расход"
            and cat_et == "variable"
            and cat_ch not in EPISODIC_CHARS
        )

        if af == "Доход":
            plan_by_day_all[day]   = plan_by_day_all.get(day, 0)   + amount
            plan_by_day_fixed[day] = plan_by_day_fixed.get(day, 0) + amount
        elif at in ("Расход", "Обязательства"):
            plan_by_day_all[day] = plan_by_day_all.get(day, 0) - amount
            if not is_variable_flow:
                # Variable flow already accounted for via r_var; skip to avoid double-counting
                plan_by_day_fixed[day] = plan_by_day_fixed.get(day, 0) - amount

    points = []
    running_fact     = B_month_start
    running_forecast = B0_now
    running_plan     = B0_now

    for d in range(1, days_in_month + 1):
        point: dict = {"day": d}

        if d <= d_now:
            running_fact += fact_by_day.get(d, 0)
            point["fact"] = round(running_fact)
        else:
            point["fact"] = None
            running_forecast -= r_var
            running_forecast += plan_by_day_fixed.get(d, 0)
            days_ahead = d - d_now
            sigma = sigma_day * math.sqrt(days_ahead)
            point["forecast"] = round(running_forecast)
            point["low"]      = round(running_forecast - Z_80 * sigma)
            point["high"]     = round(running_forecast + Z_80 * sigma)

            running_plan += plan_by_day_all.get(d, 0)
            point["plan"] = round(running_plan)

        points.append(point)

    return {"month": f"{year}-{month:02d}", "points": points}


# ── /api/metrics/affordability — §4.8 ────────────────────────────────────────

def norm_cdf(x: float) -> float:
    return (1 + math.erf(x / math.sqrt(2))) / 2


async def monthly_free_cash_history(db, user_id: str, today: date, n_months: int = 6) -> list[float]:
    results = []
    for i in range(1, n_months + 1):
        m, y = today.month - i, today.year
        if m <= 0:
            m += 12; y -= 1
        inc = await monthly_income_sum(db, user_id, y, m)
        exp = await monthly_expense_sum(db, user_id, y, m)
        results.append(inc - exp)
    return results


def find_eta(remaining: float, monthly_fc: float, sigma_monthly: float, max_months: int = 60):
    if monthly_fc <= 0:
        return None, 0.0
    cfc = 0.0
    for m in range(1, max_months + 1):
        cfc += monthly_fc
        if cfc >= remaining:
            if sigma_monthly > 0:
                sigma_cfc = sigma_monthly * math.sqrt(m)
                conf = norm_cdf((cfc - remaining) / sigma_cfc) * 100
            else:
                conf = 99.0
            return m, round(conf, 1)
    return None, 0.0


@router.get("/affordability")
async def get_affordability(request: Request):
    user_id = request.state.user_id
    db = request.state.db
    today = date.today()

    history = await monthly_free_cash_history(db, user_id, today)
    avg_fc = statistics.mean(history) if history else 0.0
    sigma_fc = statistics.stdev(history) if len(history) >= 2 else 0.0

    goal_rows = await db.fetch("""
        SELECT g.id, g.name, g.target_amount, g.account_id, g.due_date,
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
        GROUP BY g.id, g.name, g.target_amount, g.account_id, g.due_date,
                 a.initial_balance
        ORDER BY g.target_amount
    """, user_id)

    goals = []
    for r in goal_rows:
        target  = float(r["target_amount"])
        current = float(r["current_balance"])
        remaining = max(0.0, target - current)
        done = current >= target
        pct  = round(min(100, current / target * 100)) if target > 0 else 0

        months_to_goal, confidence = find_eta(remaining, avg_fc, sigma_fc)

        if months_to_goal is not None:
            eta_date = date(
                today.year + (today.month + months_to_goal - 1) // 12,
                (today.month + months_to_goal - 1) % 12 + 1,
                1,
            )
            eta_label = eta_date.strftime("%B %Y")
        else:
            eta_label = None

        due_date = r["due_date"]
        if due_date and months_to_goal is not None:
            months_due = (due_date.year - today.year) * 12 + (due_date.month - today.month)
            on_track = months_to_goal <= months_due
        else:
            on_track = None

        goals.append({
            "id":         r["id"],
            "name":       r["name"],
            "target":     round(target),
            "current":    round(current),
            "remaining":  round(remaining),
            "pct":        pct,
            "done":       done,
            "due_date":   due_date.isoformat() if due_date else None,
            "eta_months": months_to_goal,
            "eta_label":  eta_label,
            "confidence": confidence,
            "on_track":   on_track,
        })

    return {
        "as_of":            today.isoformat(),
        "monthly_fc_avg":   round(avg_fc),
        "monthly_fc_sigma": round(sigma_fc),
        "goals":            goals,
    }
