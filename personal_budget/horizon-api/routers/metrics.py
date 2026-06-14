from fastapi import APIRouter, Request
from datetime import date, timedelta
from calendar import monthrange
import statistics
import math

router = APIRouter(prefix="/api/metrics", tags=["metrics"])

Z_80 = 1.2816


# ── Helpers ───────────────────────────────────────────────────────────────────

def month_context(d: date | None = None):
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


# ── DB helpers ────────────────────────────────────────────────────────────────

async def account_balances(db, user_id: str) -> dict:
    """Dynamic balances for all active accounts."""
    rows = await db.fetch("""
        SELECT
            a.id, a.name, a.account_type,
            a.is_reserve, a.is_operational, a.is_cushion,
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
                 a.is_reserve, a.is_operational, a.is_cushion, a.initial_balance
    """, user_id)
    return {r["name"]: dict(r) for r in rows}


async def flow_daily_rate(db, user_id: str, today: date) -> tuple[float, float]:
    """Robust daily rate and σ from last 30 days of flow expenses."""
    cutoff = today - timedelta(days=30)
    rows = await db.fetch("""
        SELECT date, SUM(amount) AS daily_total
        FROM transactions
        WHERE user_id=$1 AND date >= $2 AND date < $3
          AND op_type='expense' AND character='flow'
        GROUP BY date ORDER BY date
    """, user_id, cutoff, today)
    daily = [float(r["daily_total"]) for r in rows]
    return robust_rate(daily), robust_sigma(daily)


async def monthly_income_sum(db, user_id: str, year: int, month: int) -> float:
    val = await db.fetchval("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
        WHERE user_id=$1
          AND EXTRACT(YEAR  FROM date)=$2
          AND EXTRACT(MONTH FROM date)=$3
          AND op_type='income'
    """, user_id, year, month)
    return float(val)


async def monthly_expense_sum(db, user_id: str, year: int, month: int) -> float:
    val = await db.fetchval("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
        WHERE user_id=$1
          AND EXTRACT(YEAR  FROM date)=$2
          AND EXTRACT(MONTH FROM date)=$3
          AND op_type IN ('expense', 'debt_payment')
    """, user_id, year, month)
    return float(val)


async def plan_remaining(db, user_id: str, year: int, month: int, today: date) -> list:
    """Planned transactions after today in the current month."""
    rows = await db.fetch("""
        SELECT p.date, p.amount, p.op_type, p.character
        FROM plan p
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

    B0 = sum(
        float(a["balance"]) for a in accs.values()
        if a["is_operational"] and not a["is_reserve"] and not a["is_cushion"]
    )
    C_cushion = sum(float(a["balance"]) for a in accs.values() if a["is_cushion"])
    reserve_balance = sum(float(a["balance"]) for a in accs.values() if a["is_reserve"])
    liabilities = sum(
        abs(float(a["balance"])) for a in accs.values() if a["account_type"] == "Пассив"
    )
    savings_balance = sum(
        float(a["balance"]) for a in accs.values()
        if not a["is_operational"] and not a["is_reserve"] and not a["is_cushion"]
        and a["account_type"] == "Актив"
    )

    # ── Flow rate ─────────────────────────────────────────────────────────────
    r_var, sigma_day = await flow_daily_rate(db, user_id, today)
    V_remain = r_var * d_left
    sigma_remain = sigma_day * math.sqrt(d_left) if d_left > 0 else 0.0

    # ── Plan remaining ────────────────────────────────────────────────────────
    plan_rows = await plan_remaining(db, user_id, year, month, today)
    I_remain = sum(float(r["amount"]) for r in plan_rows if r.get("op_type") == "income")
    F_remain = sum(
        float(r["amount"]) for r in plan_rows
        if r.get("op_type") == "expense" and r.get("character") == "fixed"
    )
    F_remain += sum(float(r["amount"]) for r in plan_rows if r.get("op_type") == "debt_payment")
    R_topup = sum(float(r["amount"]) for r in plan_rows if r.get("op_type") == "reserve_contribution")

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

    # ── §4.3 Net capital ──────────────────────────────────────────────────────
    total_assets = sum(float(a["balance"]) for a in accs.values() if a["account_type"] != "Пассив")
    net_capital = total_assets - liabilities

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

    # ── §4.7 Runway ───────────────────────────────────────────────────────────
    liquid = B0 + reserve_balance + savings_balance

    behavioral_exps = []
    for i in range(1, 4):
        m, y = month - i, year
        if m <= 0:
            m += 12; y -= 1
        behavioral_exps.append(await monthly_expense_sum(db, user_id, y, m))
    avg_exp = statistics.mean(behavioral_exps) if behavioral_exps else 1
    behavioral_runway = liquid / avg_exp if avg_exp > 0 else 99.0

    if behavioral_runway >= 6:
        runway_status = "green"
    elif behavioral_runway >= 3:
        runway_status = "yellow"
    else:
        runway_status = "red"

    # ── §4.5 Resilience ───────────────────────────────────────────────────────
    monthly_fixed_exp = float(await db.fetchval("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
        WHERE user_id=$1
          AND EXTRACT(YEAR FROM date)=$2 AND EXTRACT(MONTH FROM date)=$3
          AND character='fixed'
    """, user_id, year, month))

    cur_expenses = float(await db.fetchval("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
        WHERE user_id=$1
          AND EXTRACT(YEAR FROM date)=$2 AND EXTRACT(MONTH FROM date)=$3
          AND op_type='expense'
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

    s_runway  = min(behavioral_runway / 6, 1.0)
    s_reserve = min(reserve_balance / max(3 * monthly_fixed_exp, 1), 1.0)
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
        SELECT c.category, c.character AS cat_char, SUM(t.amount) AS total
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        WHERE t.user_id=$1
          AND EXTRACT(YEAR  FROM t.date)=$2
          AND EXTRACT(MONTH FROM t.date)=$3
          AND t.op_type='expense'
        GROUP BY c.category, c.character
        ORDER BY total DESC
    """, user_id, year, month)

    categories = []
    for r in cat_rows:
        fact = float(r["total"])
        if r["cat_char"] == "flow" and d_left > 0:
            cat_daily = await db.fetch("""
                SELECT t.date, SUM(t.amount) AS dt
                FROM transactions t
                JOIN categories c ON t.category_id = c.id
                WHERE t.user_id=$1 AND t.date >= $2 AND t.date < $3 AND c.category=$4
                GROUP BY t.date
            """, user_id, today - timedelta(days=30), today, r["category"])
            cat_rate = robust_rate([float(x["dt"]) for x in cat_daily])
            forecast = fact + cat_rate * d_left
        else:
            forecast = fact
        categories.append({
            "category": r["category"],
            "character": r["cat_char"],
            "amount_fact": round(fact),
            "amount_forecast": round(forecast),
        })

    # ── Flow fact this month ──────────────────────────────────────────────────
    flow_fact = float(await db.fetchval("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
        WHERE user_id=$1
          AND EXTRACT(YEAR FROM date)=$2 AND EXTRACT(MONTH FROM date)=$3
          AND op_type='expense' AND character='flow'
    """, user_id, year, month))

    # ── Response ──────────────────────────────────────────────────────────────
    return {
        "as_of": today.isoformat(),
        "safe_to_spend": {
            "value":   round(sts),
            "low":     round(sts_low),
            "high":    round(sts_high),
            "status":  sts_status,
            "waterfall": {
                "b0":       round(B0),
                "i_remain": round(I_remain),
                "f_remain": round(F_remain),
                "v_remain": round(V_remain),
                "r_topup":  round(R_topup),
                "c_cushion": round(C_cushion),
            },
        },
        "net_capital": {
            "value":  round(net_capital),
            "status": "green" if net_capital >= 0 else "red",
        },
        "dsr": {
            "value":            round(dsr * 100, 1),
            "monthly_payments": round(monthly_payments),
            "status":           dsr_status,
        },
        "runway": {
            "behavioral_months": round(behavioral_runway, 1),
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
            "daily":            round(r_var),
            "daily_sigma":      round(sigma_day),
            "flow_fact_month":  round(flow_fact),
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

    # Balance at start of month
    accs = await account_balances(db, user_id)
    B0_now = sum(
        float(a["balance"]) for a in accs.values()
        if a["is_operational"] and not a["is_reserve"] and not a["is_cushion"]
    )

    # Fact daily flows (for days 1..d_now)
    daily_fact = await db.fetch("""
        SELECT date,
               SUM(CASE WHEN account_to   IN (SELECT name FROM accounts WHERE user_id=$1 AND is_operational=true AND is_reserve=false AND is_cushion=false) THEN amount ELSE 0 END)
             - SUM(CASE WHEN account_from IN (SELECT name FROM accounts WHERE user_id=$1 AND is_operational=true AND is_reserve=false AND is_cushion=false) THEN amount ELSE 0 END)
               AS net
        FROM transactions
        WHERE user_id=$1
          AND EXTRACT(YEAR FROM date)=$2 AND EXTRACT(MONTH FROM date)=$3
        GROUP BY date ORDER BY date
    """, user_id, year, month)

    # Build cumulative fact
    fact_by_day = {r["date"].day: float(r["net"]) for r in daily_fact}

    # Start-of-month balance (B0_now - cumulative net for the month so far)
    month_net_so_far = sum(fact_by_day.get(d, 0) for d in range(1, d_now + 1))
    B_month_start = B0_now - month_net_so_far

    r_var, sigma_day = await flow_daily_rate(db, user_id, today)

    # Planned schedule (remaining)
    plan_rows = await plan_remaining(db, user_id, year, month, today)
    plan_by_day: dict[int, float] = {}
    for r in plan_rows:
        day = r["date"].day
        amount = float(r["amount"])
        op = r.get("op_type", "")
        if op == "income":
            plan_by_day[day] = plan_by_day.get(day, 0) + amount
        elif op in ("expense", "debt_payment", "reserve_contribution"):
            plan_by_day[day] = plan_by_day.get(day, 0) - amount

    points = []
    running_fact = B_month_start
    running_forecast = B0_now
    running_plan = B0_now

    for d in range(1, days_in_month + 1):
        point: dict = {"day": d}

        if d <= d_now:
            running_fact += fact_by_day.get(d, 0)
            point["fact"] = round(running_fact)
        else:
            point["fact"] = None
            running_forecast -= r_var
            running_forecast += plan_by_day.get(d, 0)  # income side only affects forecast via plan
            days_ahead = d - d_now
            sigma = sigma_day * math.sqrt(days_ahead)
            point["forecast"] = round(running_forecast)
            point["low"]      = round(running_forecast - Z_80 * sigma)
            point["high"]     = round(running_forecast + Z_80 * sigma)

            running_plan += plan_by_day.get(d, 0)
            point["plan"] = round(running_plan)

        points.append(point)

    return {"month": f"{year}-{month:02d}", "points": points}


# ── /api/metrics/affordability — §4.8 Следующий шаг ─────────────────────────

def norm_cdf(x: float) -> float:
    return (1 + math.erf(x / math.sqrt(2))) / 2


async def monthly_free_cash_history(db, user_id: str, today: date, n_months: int = 6) -> list[float]:
    """Free cash per month for last n_months: income - expenses - reserve_contributions."""
    results = []
    for i in range(1, n_months + 1):
        m, y = today.month - i, today.year
        if m <= 0:
            m += 12; y -= 1
        inc = await db.fetchval("""
            SELECT COALESCE(SUM(amount), 0) FROM transactions
            WHERE user_id=$1 AND EXTRACT(YEAR FROM date)=$2 AND EXTRACT(MONTH FROM date)=$3
              AND op_type='income'
        """, user_id, y, m)
        exp = await db.fetchval("""
            SELECT COALESCE(SUM(amount), 0) FROM transactions
            WHERE user_id=$1 AND EXTRACT(YEAR FROM date)=$2 AND EXTRACT(MONTH FROM date)=$3
              AND op_type IN ('expense', 'debt_payment')
        """, user_id, y, m)
        res = await db.fetchval("""
            SELECT COALESCE(SUM(amount), 0) FROM transactions
            WHERE user_id=$1 AND EXTRACT(YEAR FROM date)=$2 AND EXTRACT(MONTH FROM date)=$3
              AND op_type='reserve_contribution'
        """, user_id, y, m)
        free = float(inc) - float(exp) - float(res)
        results.append(free)
    return results


def find_eta(remaining: float, monthly_fc: float, sigma_monthly: float, max_months: int = 60):
    """Returns (months_to_goal, confidence_pct) or None if not achievable."""
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

    # ── Monthly free cash history ─────────────────────────────────────────────
    history = await monthly_free_cash_history(db, user_id, today)
    avg_fc = statistics.mean(history) if history else 0.0
    sigma_fc = statistics.stdev(history) if len(history) >= 2 else 0.0

    # ── Goals ─────────────────────────────────────────────────────────────────
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
        target = float(r["target_amount"])
        current = float(r["current_balance"])
        remaining = max(0.0, target - current)
        done = current >= target
        pct = round(min(100, current / target * 100)) if target > 0 else 0

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
            "id":           r["id"],
            "name":         r["name"],
            "target":       round(target),
            "current":      round(current),
            "remaining":    round(remaining),
            "pct":          pct,
            "done":         done,
            "due_date":     due_date.isoformat() if due_date else None,
            "eta_months":   months_to_goal,
            "eta_label":    eta_label,
            "confidence":   confidence,
            "on_track":     on_track,
        })

    return {
        "as_of":          today.isoformat(),
        "monthly_fc_avg": round(avg_fc),
        "monthly_fc_sigma": round(sigma_fc),
        "goals":          goals,
    }
