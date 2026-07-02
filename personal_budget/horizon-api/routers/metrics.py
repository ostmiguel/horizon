from fastapi import APIRouter, Request
from datetime import date, timedelta
from calendar import monthrange
import statistics
import math

from metrics_core import (
    Z_80, FLOW_CHARS, EPISODIC_CHARS, FIXED_CHARS,
    month_context, robust_rate, robust_sigma, _is_op, _is_rsv,
    account_balances, flow_daily_rate, monthly_income_sum,
    monthly_fixed_income_sum, monthly_expense_sum, plan_remaining, plan_window, safe_to_spend,
)
from plan_materialize import ensure_materialized, current_and_next_month


async def _ensure_plan_fresh(db, user_id):
    """Гарантирует, что план текущего и следующего месяца материализован из
    правил перед чтением (пилюли, прогноз, trough читают plan напрямую).
    Иначе «Бюджет» (рисует из правил) и Обзор/график (читают plan) расходятся —
    напр., фикс-расход в дату есть в правилах, но не вычитается в прогнозе."""
    for y, mo in current_and_next_month():
        await ensure_materialized(db, user_id, y, mo)

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


# ── /api/metrics ──────────────────────────────────────────────────────────────

@router.get("")
async def get_metrics(request: Request):
    user_id = request.state.user_id
    db = request.state.db

    await _ensure_plan_fresh(db, user_id)

    # Единый расчёт STS и его компонентов (см. metrics_core.safe_to_spend) — один источник истины с forecast_cron.
    m = await safe_to_spend(db, user_id)
    today = m["today"]; year = m["year"]; month = m["month"]
    d_now = m["d_now"]; d_left = m["d_left"]; days_in_month = m["days_in_month"]
    accs = m["accs"]; b0_accounts = m["b0_accounts"]
    B0 = m["B0"]; C_cushion = m["C_cushion"]
    reserve_balance = m["reserve_balance"]; liabilities = m["liabilities"]
    r_var = m["r_var"]; sigma_day = m["sigma_day"]
    V_remain = m["V_remain"]; sigma_remain = m["sigma_remain"]
    plan_rows = m["plan_rows"]; reserve_names = m["reserve_names"]
    liability_names = m["liability_names"]   # имена всех счетов-Пассивов (обязательства)
    I_remain = m["I_remain"]; F_remain = m["F_remain"]; R_topup = m["R_topup"]
    F_before = m["F_before"]; V_to_income = m["V_to_income"]; R_before = m["R_before"]
    next_income_date = m["next_income_date"]; days_to_income = m["days_to_income"]
    sts = m["sts"]; sts_low = m["sts_low"]; sts_high = m["sts_high"]; sts_status = m["sts_status"]

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

    # DSR считаем от ГАРАНТИРОВАННОГО (фиксированного) дохода: платежи по долгам
    # должны соотноситься со стабильным приходом (зарплата), а не со всем доходом,
    # который может включать разовые/нерегулярные поступления.
    dsr_income = await monthly_fixed_income_sum(db, user_id, year, month)
    if dsr_income == 0:
        prev_fixed = []
        for i in range(1, 4):
            m, y = month - i, year
            if m <= 0:
                m += 12; y -= 1
            prev_fixed.append(await monthly_fixed_income_sum(db, user_id, y, m))
        prev_fixed = [v for v in prev_fixed if v > 0]
        dsr_income = statistics.mean(prev_fixed) if prev_fixed else 0.0

    # Если фиксированный доход нигде не размечен — деградируем к общему доходу,
    # чтобы не показывать обманчивые 0%/«зелёный» при наличии долговых платежей.
    dsr_base = dsr_income if dsr_income > 0 else cur_income
    dsr = monthly_payments / dsr_base if dsr_base > 0 else 0.0
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
        if r["account_to"] in liability_names
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
          AND (account_to = 'Расход'
               OR account_to IN (SELECT name FROM accounts
                                 WHERE user_id=$1 AND account_type='Пассив' AND is_active=true))
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
          AND (t.account_to = 'Расход'
               OR t.account_to IN (SELECT name FROM accounts
                                   WHERE user_id=$1 AND account_type='Пассив' AND is_active=true))
        GROUP BY c.category, c.character, c.expense_type
        ORDER BY total DESC
    """, user_id, year, month)

    # Конверты (category_budgets) — план повседневных категорий (уровень КАТЕГОРИИ).
    env_rows = await db.fetch("""
        SELECT category, budget FROM category_budgets
        WHERE user_id=$1 AND year=$2 AND month=$3
    """, user_id, year, month)
    env_by_cat = {r["category"]: float(r["budget"]) for r in env_rows}
    env_seen: set[str] = set()  # категории, куда конверт уже учтён в amount_plan

    # Track which categories already have fact data
    seen_cat_chars: set[tuple] = set()
    categories = []
    CAT_WINDOW = 30

    for r in cat_rows:
        fact = float(r["total"])
        cat_remaining = 0.0  # поведенческий остаток до конца месяца (только повседневные)
        is_variable_everyday = (
            r["cat_expense_type"] == "variable"
            and r["cat_char"] not in EPISODIC_CHARS
        )
        # План категории за месяц = сумма материализованных плановых строк (правила +
        # график кредита), включая тело долга (Обязательства) и процент (Расход).
        # Правила — основа плана: есть план → показываем его, иначе поведение/факт.
        # Считаем по (категория, характер), чтобы фронт корректно суммировал плечи.
        plan_cat_total = float(await db.fetchval("""
            SELECT COALESCE(SUM(p.amount), 0)
            FROM plan p
            JOIN categories c ON p.category_id = c.id
            WHERE p.user_id=$1
              AND EXTRACT(YEAR  FROM p.date)=$2
              AND EXTRACT(MONTH FROM p.date)=$3
              AND c.category=$4 AND c.character=$5
              AND (p.account_to = 'Расход'
                   OR p.account_to IN (SELECT name FROM accounts
                                       WHERE user_id=$1 AND account_type='Пассив' AND is_active=true))
        """, user_id, year, month, r["category"], r["cat_char"]) or 0)

        if is_variable_everyday and d_left > 0:
            # §3.3: rate = total_in_window / window_days, MAD-фильтр выбросов
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
            pos = [x for x in cat_daily if x > 0]
            if len(pos) >= 2:
                med = statistics.median(pos)
                mad = statistics.median([abs(x - med) for x in pos])
                threshold = med + 3 * 1.4826 * mad
                cat_daily = [x if x <= threshold else 0.0 for x in cat_daily]
            cat_rate = sum(cat_daily) / CAT_WINDOW
            cat_remaining = cat_rate * d_left

        if plan_cat_total > 0:
            forecast = plan_cat_total          # основа — правила/график кредита
        elif is_variable_everyday:
            forecast = fact + cat_remaining    # поведенческий прогноз
        else:
            forecast = fact                    # fixed/episodic без правил — факт

        # План категории = плановые строки (правила/график) + конверт (для повседневной части).
        env_amt = 0.0
        if is_variable_everyday and r["category"] in env_by_cat and r["category"] not in env_seen:
            env_amt = env_by_cat[r["category"]]
            env_seen.add(r["category"])

        seen_cat_chars.add((r["category"], r["cat_char"]))
        categories.append({
            "category":        r["category"],
            "character":       r["cat_char"],
            "expense_type":    r["cat_expense_type"],
            "amount_fact":     round(fact),
            "amount_forecast": round(forecast),
            "amount_plan":     round(plan_cat_total + env_amt),
            "flow_remaining":  round(cat_remaining),
        })

    # Категории с планом (правила/график кредита), но без факта в этом месяце —
    # оба плеча: тело долга (Обязательства) и процент/расход (Расход).
    plan_extra_cats = await db.fetch("""
        SELECT c.category, c.character AS cat_char, c.expense_type AS cat_expense_type,
               SUM(p.amount) AS plan_total
        FROM plan p
        JOIN categories c ON p.category_id = c.id
        WHERE p.user_id=$1
          AND EXTRACT(YEAR  FROM p.date)=$2
          AND EXTRACT(MONTH FROM p.date)=$3
          AND (p.account_to = 'Расход'
               OR p.account_to IN (SELECT name FROM accounts
                                   WHERE user_id=$1 AND account_type='Пассив' AND is_active=true))
        GROUP BY c.category, c.character, c.expense_type
    """, user_id, year, month)
    for r in plan_extra_cats:
        if (r["category"], r["cat_char"]) not in seen_cat_chars:
            is_var = r["cat_expense_type"] == "variable" and r["cat_char"] not in EPISODIC_CHARS
            env_amt = 0.0
            if is_var and r["category"] in env_by_cat and r["category"] not in env_seen:
                env_amt = env_by_cat[r["category"]]
                env_seen.add(r["category"])
            categories.append({
                "category":        r["category"],
                "character":       r["cat_char"],
                "expense_type":    r["cat_expense_type"],
                "amount_fact":     0,
                "amount_forecast": round(float(r["plan_total"])),
                "amount_plan":     round(float(r["plan_total"]) + env_amt),
                "flow_remaining":  0,
            })

    # Конверты категорий, не попавших ни в факт, ни в план-строки (план без факта).
    for cat, bud in env_by_cat.items():
        if cat not in env_seen:
            env_seen.add(cat)
            categories.append({
                "category":        cat,
                "character":       "Повседневный",
                "expense_type":    "variable",
                "amount_fact":     0,
                "amount_forecast": round(bud),
                "amount_plan":     round(bud),
                "flow_remaining":  0,
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

    fixed_by_cat: dict[tuple, float] = {}
    for r in plan_rows:
        if r.get("account_to") == "Расход" and (
            r.get("cat_expense_type") == "fixed"
            or r.get("cat_character") in EPISODIC_CHARS
        ):
            cat  = r.get("cat_category") or "Расходы"
            kind = "episodic" if r.get("cat_character") in EPISODIC_CHARS else "fixed"
            key  = (cat, kind)
            fixed_by_cat[key] = fixed_by_cat.get(key, 0) + float(r["amount"])
        elif r.get("account_to") in liability_names:
            cat = r.get("cat_category") or r.get("account_to") or "Обязательства"
            key = (cat, "fixed")
            fixed_by_cat[key] = fixed_by_cat.get(key, 0) + float(r["amount"])
    fixed_items = [{"category": k[0], "type": k[1], "amount": round(v)}
                   for k, v in sorted(fixed_by_cat.items(), key=lambda x: -x[1])]

    reserve_topup_items: list[dict] = []
    topup_by_name: dict[str, float] = {}
    for r in plan_rows:
        if r.get("account_to") in reserve_names:
            name = r.get("account_to", "Резерв")
            topup_by_name[name] = topup_by_name.get(name, 0) + float(r["amount"])
    reserve_topup_items = [{"name": k, "amount": round(v)}
                           for k, v in sorted(topup_by_name.items(), key=lambda x: -x[1])]

    cushion_accounts_detail = [
        {"name": a["name"], "balance": round(float(a["balance"]))}
        for a in accs.values() if a.get("is_cushion")
    ]

    # Decompose the pill value V_remain (forecasted remaining flow) by category.
    # Per-category remaining = cat_rate × d_left, normalized so the parts sum to
    # V_remain exactly (r_var is a global robust rate, not the sum of cat rates).
    var_remaining = [
        {"category": c["category"], "amount": float(c.get("flow_remaining", 0))}
        for c in categories
        if c.get("expense_type") == "variable"
        and c.get("character") not in EPISODIC_CHARS
        and c.get("flow_remaining", 0) > 0
    ]
    sum_remaining = sum(x["amount"] for x in var_remaining)
    V_target = round(V_remain)
    if sum_remaining > 0 and V_target > 0:
        scale = V_remain / sum_remaining
        for x in var_remaining:
            x["amount"] *= scale
        var_remaining.sort(key=lambda x: -x["amount"])
        top = var_remaining[:5]
        variable_items = [{"category": x["category"], "amount": round(x["amount"])}
                          for x in top]
        rest = V_target - sum(it["amount"] for it in variable_items)
        if len(var_remaining) > 5 and rest > 0:
            variable_items.append({"category": "Прочие", "amount": rest})
        elif rest != 0 and variable_items:
            # absorb rounding residual into the largest item so parts sum to V_remain
            variable_items[0]["amount"] += rest
    else:
        variable_items = []

    # Месячный бюджет-конверт по категориям (additive для листа «Бюджет»;
    # формулы метрик не трогаем — просто кладём budget рядом с факт/прогноз).
    budget_rows = await db.fetch("""
        SELECT category AS cat, COALESCE(SUM(budget), 0) AS budget
        FROM category_budgets
        WHERE user_id=$1 AND year=$2 AND month=$3
        GROUP BY category
    """, user_id, year, month)
    budget_by_cat = {r["cat"]: float(r["budget"]) for r in budget_rows}
    for c in categories:
        c["budget"] = round(budget_by_cat.get(c["category"], 0))

    # ── Response ──────────────────────────────────────────────────────────────
    return {
        "as_of": today.isoformat(),
        "safe_to_spend": {
            "value":   round(sts),
            "low":     round(sts_low),
            "high":    round(sts_high),
            "status":  sts_status,
            "next_income_date": next_income_date.isoformat(),
            "days_to_income":   days_to_income,
            # Пилюли живут по ТЕКУЩЕМУ МЕСЯЦУ (что ещё впереди до конца месяца),
            # сбрасываются 1-го числа и обновляются по мере поступления фактов.
            # Это отдельный горизонт от «Свободно» (trough до след. дохода) —
            # см. F_before/V_to_income выше, они только для sts.
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
                "income_items":          income_items,
                "fixed_items":           fixed_items,
                "variable_items":        variable_items,
                "reserve_topup_items":   reserve_topup_items,
                "cushion_accounts":      cushion_accounts_detail,
                "v_daily_rate":          round(r_var),
                "d_left":                d_left,
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
            "income_base":      round(dsr_base),
            "income_kind":      "fixed" if dsr_income > 0 else "total",
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

async def _forecast_year(db, user_id, today, B0_now, r_var, reserve_names, liability_names):
    """Годовой прогноз — РЕАЛЬНАЯ траектория день-в-день на 365 дней.

    Источники (всё, что пользователь уже задал):
      • правила (plan_rules) — регулярные доходы/фикс.платежи/пополнения резервов,
        разворачиваются на каждый месяц по day_of_month;
      • разовые плановые события (plan, не из правил и не кредиты) — эпизодические
        покупки, отпуск и т.п. на конкретные даты;
      • график кредитов (loan_schedule) — тело+процент как отток в дату платежа
        (естественно учитывает завершение кредита — платежи просто кончаются);
      • повседневные — по конвертам месяца (category_budgets), распределённым
        равномерно по дням; если конверта на месяц нет — падаем на r_var.
    Точки — ежедневные, поэтому виден настоящий рельеф: зарплатные подъёмы,
    провалы под эпизодические траты, снижение нагрузки после закрытия кредитов."""
    horizon_end = today + timedelta(days=365)
    events: dict = {}   # дата → знаковая дельта (доход +, отток −)

    def add(d, amt):
        events[d] = events.get(d, 0.0) + amt

    # 1) Регулярные правила → раскладываем по месяцам
    rules = await db.fetch("""
        SELECT pr.amount, pr.account_from, pr.account_to, pr.day_of_month,
               c.expense_type AS et, c.character AS ch
        FROM plan_rules pr
        LEFT JOIN categories c ON pr.category_id = c.id
        WHERE pr.user_id=$1 AND pr.is_active=true
    """, user_id)
    cur = date(today.year, today.month, 1)
    for _ in range(14):   # текущий + 13 месяцев — покрывает 365 дней
        dim = monthrange(cur.year, cur.month)[1]
        for r in rules:
            dom = int(r["day_of_month"]) if r["day_of_month"] else 1
            d = date(cur.year, cur.month, max(1, min(dom, dim)))
            if d <= today or d > horizon_end:
                continue
            amt = float(r["amount"]); af = r["account_from"]; at = r["account_to"]
            if af == "Доход":
                add(d, amt)
            elif at == "Расход" or at in liability_names:
                is_var = (at == "Расход" and r["et"] == "variable" and r["ch"] not in EPISODIC_CHARS)
                if not is_var:
                    add(d, -amt)
            elif at in reserve_names:
                add(d, -amt)
        cur = date(cur.year + (1 if cur.month == 12 else 0), 1 if cur.month == 12 else cur.month + 1, 1)

    # 2) Разовые плановые события (эпизодические, ручные) — не из правил, не кредиты
    oneoffs = await db.fetch("""
        SELECT p.date, p.amount, p.account_from, p.account_to,
               c.expense_type AS et, c.character AS ch
        FROM plan p
        LEFT JOIN categories c ON p.category_id = c.id
        WHERE p.user_id=$1 AND p.date > $2 AND p.date <= $3
          AND p.source_rule_id IS NULL
          AND (p.source IS NULL OR p.source <> 'loan_schedule')
    """, user_id, today, horizon_end)
    for r in oneoffs:
        amt = float(r["amount"]); af = r["account_from"]; at = r["account_to"]
        if af == "Доход":
            add(r["date"], amt)
        elif at == "Расход" or at in liability_names:
            is_var = (at == "Расход" and r["et"] == "variable" and r["ch"] not in EPISODIC_CHARS)
            if not is_var:
                add(r["date"], -amt)
        elif at in reserve_names:
            add(r["date"], -amt)

    # 3) График кредитов — тело+процент как отток (завершение кредита учтено само)
    loan_rows = await db.fetch("""
        SELECT ls.date, ls.principal, ls.interest
        FROM loan_schedule ls JOIN loans l ON ls.loan_id = l.id
        WHERE l.user_id=$1 AND l.is_active=true AND ls.is_paid=false
          AND ls.date > $2 AND ls.date <= $3
    """, user_id, today, horizon_end)
    for r in loan_rows:
        add(r["date"], -(float(r["principal"] or 0) + float(r["interest"] or 0)))

    # 4) Повседневные по конвертам месяца (иначе r_var)
    budgets = await db.fetch("""
        SELECT year, month, COALESCE(SUM(budget), 0) AS total
        FROM category_budgets WHERE user_id=$1 GROUP BY year, month
    """, user_id)
    env_by_ym = {(b["year"], b["month"]): float(b["total"]) for b in budgets}

    # ── Проекция день-в-день ────────────────────────────────────────────────────
    points = [{"date": today.isoformat(), "forecast": round(B0_now)}]
    running = B0_now
    d = today + timedelta(days=1)
    while d <= horizon_end:
        dim = monthrange(d.year, d.month)[1]
        env = env_by_ym.get((d.year, d.month))
        daily_flow = (env / dim) if env is not None else r_var
        running -= daily_flow
        running += events.get(d, 0.0)
        points.append({"date": d.isoformat(), "forecast": round(running)})
        d += timedelta(days=1)

    # Низшая точка года (если она в будущем, а не сегодня) — маркер риска.
    min_pt = min(points, key=lambda p: p["forecast"])
    trough_value = trough_date = None
    if min_pt is not points[0]:
        min_pt["trough"] = True
        trough_value = min_pt["forecast"]; trough_date = min_pt["date"]

    monthly_net = round((points[-1]["forecast"] - B0_now) / 12)

    return {
        "today":            today.isoformat(),
        "range":            "year",
        "monthly_net":      monthly_net,
        "next_income_date": None,
        "trough_date":      trough_date,
        "trough_value":     trough_value,
        "safe_min":         0,
        "points":           points,
    }


@router.get("/forecast")
async def get_forecast(request: Request, range: str = "30"):
    user_id = request.state.user_id
    db = request.state.db

    await _ensure_plan_fresh(db, user_id)

    today, year, month, d_now, d_left, days_in_month, month_start, month_end = month_context()

    accs = await account_balances(db, user_id)
    op_names      = [a["name"] for a in accs.values() if _is_op(a)]
    reserve_names = {name for name, a in accs.items() if a.get("is_reserve") is True}
    liability_names = {name for name, a in accs.items() if a["account_type"] == "Пассив"}
    B0_now  = sum(float(a["balance"]) for a in accs.values() if _is_op(a))
    cushion = sum(float(a["balance"]) for a in accs.values() if a.get("is_cushion"))

    r_var, sigma_day = await flow_daily_rate(db, user_id, today)
    # Безопасный минимум: хотя бы недельный запас трат, или подушка, если она крупнее.
    safe_min = max(cushion, r_var * 7)

    # ── Горизонт «Год»: помесячный тренд по run-rate ───────────────────────────
    if range == "year":
        return await _forecast_year(db, user_id, today, B0_now, r_var, reserve_names, liability_names)

    # ── Следующий доход (план; может быть в следующем месяце) ──────────────────
    fut = await plan_window(db, user_id, today, today + timedelta(days=75))
    incomes = [r for r in fut if r.get("account_from") == "Доход"]
    next_income_date = min((r["date"] for r in incomes), default=month_end)
    days_to_income = max((next_income_date - today).days, 0)

    # ── Окно графика: сегодня ближе к центру, приоритет — ВПЕРЁД ────────────────
    # Сервис про будущее, поэтому будущего не меньше прошлого. Вперёд — минимум
    # 16 дней и как минимум до ближайшего дохода +2 (чтобы надир и скачок зарплаты
    # были в кадре). Прошлое добираем до ~30, но короче будущего. НЕ клампим к
    # началу месяца (иначе в первых числах окно схлопывается).
    end_date = max(today + timedelta(days=16), next_income_date + timedelta(days=2))
    fwd = (end_date - today).days
    past_days = max(6, min(14, 30 - fwd))
    start_date = today - timedelta(days=past_days)

    # ── Факт (операционный нетто) по датам окна ────────────────────────────────
    if op_names:
        daily_fact = await db.fetch("""
            SELECT date,
                   SUM(CASE WHEN account_to   = ANY($4::text[]) THEN amount ELSE 0 END)
                 - SUM(CASE WHEN account_from = ANY($4::text[]) THEN amount ELSE 0 END) AS net
            FROM transactions
            WHERE user_id=$1 AND date > $2 AND date <= $3
            GROUP BY date
        """, user_id, start_date, today, op_names)
    else:
        daily_fact = []
    fact_by_date = {r["date"]: float(r["net"]) for r in daily_fact}
    B_start = B0_now - sum(fact_by_date.values())   # баланс на начало окна

    # ── Плановые «фикс»-движения в окне прогноза (today, end_date] ─────────────
    # (повседневные уже в r_var; income добавляем — он поднимает линию после зарплаты)
    plan_fixed_by_date: dict = {}
    F_before = 0.0   # оттоки до даты дохода → для trough = «Свободно» (как в safe_to_spend)
    R_before = 0.0
    for r in fut:
        d = r["date"]
        if d <= today or d > end_date:
            continue
        amount = float(r["amount"]); af = r.get("account_from", ""); at = r.get("account_to", "")
        cat_et = r.get("cat_expense_type"); cat_ch = r.get("cat_character", "")
        is_var = (at == "Расход" and cat_et == "variable" and cat_ch not in EPISODIC_CHARS)
        if af == "Доход":
            plan_fixed_by_date[d] = plan_fixed_by_date.get(d, 0) + amount
        elif at == "Расход" or at in liability_names:
            if not is_var:
                plan_fixed_by_date[d] = plan_fixed_by_date.get(d, 0) - amount
                if d < next_income_date:
                    F_before += amount
        elif at in reserve_names:
            plan_fixed_by_date[d] = plan_fixed_by_date.get(d, 0) - amount
            if d < next_income_date:
                R_before += amount

    # trough = «Свободно» = КОНЕЦ дня ПЕРЕД доходом (в день зарплаты деньги
    # приходят, лишний день трат не считаем). Один день = одна точка на графике,
    # без синтетического пред-зарплатного дубля. Закрытая формула сходится с
    # точкой дня-перед-доходом (тот же расчёт, что sts в safe_to_spend).
    days_before = max(days_to_income - 1, 0)
    trough_value = round(B0_now - F_before - r_var * days_before - R_before)
    trough_day = (next_income_date - timedelta(days=1)) if days_to_income > 0 else today
    trough_date = trough_day.isoformat()

    # ── Точки по дням ──────────────────────────────────────────────────────────
    points = []
    running_fact = B_start
    running_fcst = B0_now
    d = start_date
    while d <= end_date:
        pt = {"date": d.isoformat()}
        if d < today:
            running_fact += fact_by_date.get(d, 0)
            pt["fact"] = round(running_fact)
        elif d == today:
            running_fact += fact_by_date.get(d, 0)   # → B0_now
            pt["fact"] = round(running_fact)
            pt["forecast"] = round(running_fcst)     # стыкуем линии без разрыва
        else:
            running_fcst -= r_var
            running_fcst += plan_fixed_by_date.get(d, 0)
            days_ahead = (d - today).days
            sigma = sigma_day * math.sqrt(days_ahead)
            pt["forecast"] = round(running_fcst)
            pt["low"]      = round(running_fcst - Z_80 * sigma)
            pt["high"]     = round(running_fcst + Z_80 * sigma)
        if d == trough_day and "forecast" in pt:
            pt["trough"] = True   # низшая точка «Свободно» — день перед доходом
        points.append(pt)
        d += timedelta(days=1)

    return {
        "today":            today.isoformat(),
        "next_income_date": next_income_date.isoformat(),
        "days_to_income":   days_to_income,
        "trough_date":      trough_date,
        "trough_value":     trough_value,
        "safe_min":         round(safe_min),
        "points":           points,
    }


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
