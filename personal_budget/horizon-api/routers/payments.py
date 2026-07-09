"""
«Спросить о платеже» — сверка плановых датированных платежей с фактом.

Фиксирует общий footgun: как только дата планового платежа проходит, он выпадает
из плана (STS перестаёт вычитать), но ledger двигается только если внесена
реальная операция. Здесь пользователь подтверждает факт → создаём операцию →
«Свободно» перестаёт врать.

Охват (датированные дискретные движения):
  • правила-расходы (plan.source_rule_id) — аренда/подписки/связь;
  • разовые события и пополнение резерва (plan-строки, source_rule_id IS NULL);
  • платежи по кредиту (loan_schedule) — тело+проценты, статус в is_paid.
Доходы (account_from='Доход') и конверты повседневных сюда не входят.

Статус правил/событий храним в plan_confirmations; кредитов — в loan_schedule.is_paid.
При подтверждении/скипе плановая occurrence удаляется из plan (и не пере-
материализуется — см. plan_materialize), чтобы не задваивать с созданной операцией.
"""
from datetime import date, timedelta
# Алиас типа: поля, НАЗВАННЫЕ 'date', затеняют тип date (дефолт =None → Pydantic
# резолвит тип как NoneType → 422 «Input should be None»). Через алиас коллизии нет.
_Date = date
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/payments", tags=["payments"])

WINDOW_DAYS = 45   # не пилим о слишком старых платежах


def _title(row) -> str:
    # Движения резерва не имеют категории → даём осмысленное имя (иначе «Платёж»).
    if row.get("to_reserve"):
        return f"В резерв — {row.get('account_to')}"
    if row.get("from_reserve") and row.get("account_to") != "Расход":
        return f"Из резерва — {row.get('account_from')}"
    return (row.get("rule_name") or row.get("subcategory")
            or row.get("cat_cat") or row.get("note") or "Платёж")


@router.get("/pending")
async def pending(request: Request):
    """Плановые платежи текущего месяца с датой ≤ сегодня, ещё не разрешённые.
    Матчинг с операциями НЕ делаем — сверку факта пользователь проводит вручную
    (это и есть защита от ошибок). Кредиты входят: подтверждение создаёт операции
    (тело→счёт-Пассив + %→Расход), ставит is_paid и обновляет долг."""
    user_id = request.state.user_id
    db = request.state.db
    today = date.today()
    yy, mm = today.year, today.month   # только текущий месяц, дата ≤ сегодня

    plan_rows = await db.fetch("""
        SELECT p.id, p.date, p.amount, p.account_from, p.account_to, p.category_id,
               p.note, p.source_rule_id,
               pr.name AS rule_name,
               c.subcategory, c.category AS cat_cat,
               (COALESCE(a.is_reserve, false)  OR COALESCE(a.is_cushion, false))  AS to_reserve,
               (COALESCE(af.is_reserve, false) OR COALESCE(af.is_cushion, false)) AS from_reserve
        FROM plan p
        LEFT JOIN plan_rules pr ON p.source_rule_id = pr.id
        LEFT JOIN categories c  ON p.category_id = c.id
        LEFT JOIN accounts   a  ON a.user_id = p.user_id AND a.name = p.account_to
        LEFT JOIN accounts   af ON af.user_id = p.user_id AND af.name = p.account_from
        WHERE p.user_id = $1
          AND p.date <= $2
          AND EXTRACT(YEAR  FROM p.date)::int = $3
          AND EXTRACT(MONTH FROM p.date)::int = $4
          AND (p.source IS NULL OR p.source <> 'loan_schedule')
          AND p.account_from <> 'Доход'
          AND NOT EXISTS (
            SELECT 1 FROM plan_confirmations pc
            WHERE pc.user_id = p.user_id
              AND ( (pc.kind = 'rule' AND pc.ref_id = p.source_rule_id)
                 OR (pc.kind = 'plan' AND pc.ref_id = p.id) )
              AND pc.year = $3 AND pc.month = $4 )
        ORDER BY p.date
    """, user_id, today, yy, mm)

    loan_rows = await db.fetch("""
        SELECT l.id AS loan_id, l.name AS loan_name, l.account_name,
               ls.month_num, ls.date, ls.payment, ls.principal, ls.interest
        FROM loan_schedule ls
        JOIN loans l ON ls.loan_id = l.id
        WHERE l.user_id = $1 AND l.is_active = true
          AND ls.is_paid = false
          AND ls.date IS NOT NULL
          AND ls.date <= $2
          AND EXTRACT(YEAR  FROM ls.date)::int = $3
          AND EXTRACT(MONTH FROM ls.date)::int = $4
        ORDER BY ls.date
    """, user_id, today, yy, mm)

    items = []
    for r in plan_rows:
        d = dict(r)
        items.append({
            "kind": "rule" if d["source_rule_id"] else "plan",
            "ref_id": int(d["source_rule_id"] or d["id"]),
            "title": _title(d),
            "amount": float(d["amount"]),
            "date": d["date"].isoformat(),
            "account_from": d["account_from"],
            "account_to": d["account_to"],
            "category_id": d["category_id"],
            "is_reserve": bool(d["to_reserve"]),
        })
    for r in loan_rows:
        d = dict(r)
        items.append({
            "kind": "loan",
            "ref_id": int(d["loan_id"]),
            "month_num": int(d["month_num"]),
            "title": d["loan_name"],
            "amount": float(d["payment"] or 0),
            "principal": float(d["principal"] or 0),
            "interest": float(d["interest"] or 0),
            "date": d["date"].isoformat(),
            "account_to": d["account_name"],
            "is_reserve": False,
        })

    items.sort(key=lambda x: x["date"])
    return {"pending": items, "count": len(items)}


@router.get("/status")
async def status(request: Request, year: int, month: int):
    """Полный список плановых платежей месяца со статусами:
    paid | manual | skip | pending | upcoming."""
    user_id = request.state.user_id
    db = request.state.db
    today = date.today()

    out = []

    # Неразрешённые плановые строки месяца (правила/события/резерв, не кредиты, не доход)
    plan_rows = await db.fetch("""
        SELECT p.id, p.date, p.amount, p.account_from, p.account_to, p.source_rule_id,
               p.note, pr.name AS rule_name, c.subcategory, c.category AS cat_cat,
               (COALESCE(a.is_reserve, false)  OR COALESCE(a.is_cushion, false))  AS to_reserve,
               (COALESCE(af.is_reserve, false) OR COALESCE(af.is_cushion, false)) AS from_reserve
        FROM plan p
        LEFT JOIN plan_rules pr ON p.source_rule_id = pr.id
        LEFT JOIN categories c  ON p.category_id = c.id
        LEFT JOIN accounts   a  ON a.user_id = p.user_id AND a.name = p.account_to
        LEFT JOIN accounts   af ON af.user_id = p.user_id AND af.name = p.account_from
        WHERE p.user_id = $1
          AND EXTRACT(YEAR  FROM p.date)::int = $2
          AND EXTRACT(MONTH FROM p.date)::int = $3
          AND (p.source IS NULL OR p.source <> 'loan_schedule')
          AND p.account_from <> 'Доход'
          AND NOT EXISTS (
            SELECT 1 FROM plan_confirmations pc
            WHERE pc.user_id = p.user_id
              AND ( (pc.kind = 'rule' AND pc.ref_id = p.source_rule_id)
                 OR (pc.kind = 'plan' AND pc.ref_id = p.id) )
              AND pc.year = $2 AND pc.month = $3 )
        ORDER BY p.date
    """, user_id, year, month)
    for r in plan_rows:
        d = dict(r)
        st = "pending" if d["date"] <= today else "upcoming"
        out.append({
            "title": _title(d), "amount": float(d["amount"]),
            "date": d["date"].isoformat(),
            "status": st,
            "is_reserve": bool(d["to_reserve"]),
        })

    # Разрешённые (paid/manual/skip) — из журнала подтверждений
    conf = await db.fetch("""
        SELECT title, amount, occ_date, resolution
        FROM plan_confirmations
        WHERE user_id = $1 AND year = $2 AND month = $3
        ORDER BY occ_date
    """, user_id, year, month)
    for r in conf:
        d = dict(r)
        out.append({
            "title": d["title"] or "Платёж",
            "amount": float(d["amount"] or 0),
            "date": d["occ_date"].isoformat() if d["occ_date"] else None,
            "status": d["resolution"],
            "is_reserve": False,
        })

    # Кредиты месяца (статус из is_paid)
    loans = await db.fetch("""
        SELECT l.name AS loan_name, ls.date, ls.payment, ls.is_paid
        FROM loan_schedule ls
        JOIN loans l ON ls.loan_id = l.id
        WHERE l.user_id = $1 AND l.is_active = true
          AND ls.date IS NOT NULL
          AND EXTRACT(YEAR  FROM ls.date)::int = $2
          AND EXTRACT(MONTH FROM ls.date)::int = $3
        ORDER BY ls.date
    """, user_id, year, month)
    for r in loans:
        d = dict(r)
        st = "paid" if d["is_paid"] else ("pending" if d["date"] <= today else "upcoming")
        out.append({
            "title": d["loan_name"], "amount": float(d["payment"] or 0),
            "date": d["date"].isoformat(), "status": st, "is_reserve": False,
        })

    out.sort(key=lambda x: (x["date"] or ""))
    return {"items": out}


class ResolveBody(BaseModel):
    kind: str                        # 'rule' | 'plan' | 'loan'
    ref_id: int
    month_num: Optional[int] = None  # для kind='loan'
    occ_date: Optional[date] = None  # дата occurrence (таргетинг месяца для rule)
    resolution: str = "paid"         # 'paid' | 'manual'
    amount: Optional[float] = None
    date: Optional[_Date] = None
    account_from: Optional[str] = None
    account_to: Optional[str] = None
    category_id: Optional[int] = None
    note: Optional[str] = None


async def _insert_tx(db, user_id, d: date, amount, acc_from, acc_to, cat_id, note):
    row = await db.fetchrow("""
        INSERT INTO transactions
          (user_id, date, amount, account_from, account_to, category_id, participant_id, note, source)
        VALUES ($1,$2,$3,$4,$5,$6,NULL,$7,'manual') RETURNING id
    """, user_id, d, float(amount), acc_from, acc_to, cat_id, note)
    return int(row["id"])


@router.post("/confirm")
async def confirm(body: ResolveBody, request: Request):
    user_id = request.state.user_id
    db = request.state.db
    if body.resolution not in ("paid", "manual"):
        raise HTTPException(400, "resolution must be paid|manual")

    # ── Кредитный платёж ──────────────────────────────────────────────────────
    if body.kind == "loan":
        if body.month_num is None:
            raise HTTPException(400, "month_num required for loan")
        srow = await db.fetchrow("""
            SELECT ls.date, ls.payment, ls.principal, ls.interest, l.name, l.account_name
            FROM loan_schedule ls JOIN loans l ON ls.loan_id = l.id
            WHERE ls.loan_id=$1 AND ls.month_num=$2 AND l.user_id=$3
        """, body.ref_id, body.month_num, user_id)
        if not srow:
            raise HTTPException(404, "loan schedule row not found")
        d = body.date or srow["date"]
        liab = body.account_to or srow["account_name"] or "Обязательства"
        tx_id = None
        if body.resolution == "paid":
            card = body.account_from
            if not card:
                raise HTTPException(400, "account_from (карта) required for loan paid")
            cats = await db.fetch(
                "SELECT id, role FROM categories WHERE user_id=$1 AND role IN ('loan_principal','loan_interest')",
                user_id)
            role_cat = {r["role"]: r["id"] for r in cats}
            principal = float(srow["principal"] or 0)
            interest  = float(srow["interest"] or 0)
            if principal > 0:
                tx_id = await _insert_tx(db, user_id, d, principal, card, liab,
                                         role_cat.get("loan_principal"), body.note)
            if interest > 0:
                await _insert_tx(db, user_id, d, interest, card, "Расход",
                                 role_cat.get("loan_interest"), body.note)
        await db.execute(
            "UPDATE loan_schedule SET is_paid=true WHERE loan_id=$1 AND month_num=$2",
            body.ref_id, body.month_num)
        # синхронизировать current_balance по последней оплаченной строке
        last = await db.fetchrow("""
            SELECT balance FROM loan_schedule
            WHERE loan_id=$1 AND is_paid=true AND balance IS NOT NULL
            ORDER BY month_num DESC LIMIT 1
        """, body.ref_id)
        if last and last["balance"] is not None:
            await db.execute("UPDATE loans SET current_balance=$1 WHERE id=$2 AND user_id=$3",
                             last["balance"], body.ref_id, user_id)
        return {"ok": True, "tx_id": tx_id}

    # ── Правило / разовое событие ─────────────────────────────────────────────
    ot = body.occ_date        # дата occurrence — таргетируем нужный месяц правила
    if body.kind == "rule":
        occ = await db.fetchrow("""
            SELECT p.id, p.date, p.amount, p.account_from, p.account_to, p.category_id,
                   p.note, pr.name AS rule_name, c.subcategory, c.category AS cat_cat
            FROM plan p
            LEFT JOIN plan_rules pr ON p.source_rule_id = pr.id
            LEFT JOIN categories c  ON p.category_id = c.id
            WHERE p.user_id=$1 AND p.source_rule_id=$2
              AND ($3::date IS NULL OR (EXTRACT(YEAR FROM p.date)::int = EXTRACT(YEAR FROM $3::date)::int
                                    AND EXTRACT(MONTH FROM p.date)::int = EXTRACT(MONTH FROM $3::date)::int))
            ORDER BY p.date DESC LIMIT 1
        """, user_id, body.ref_id, ot)
    elif body.kind == "plan":
        occ = await db.fetchrow("""
            SELECT p.id, p.date, p.amount, p.account_from, p.account_to, p.category_id,
                   p.note, NULL::text AS rule_name, c.subcategory, c.category AS cat_cat,
                   (COALESCE(a.is_reserve, false)  OR COALESCE(a.is_cushion, false))  AS to_reserve,
                   (COALESCE(af.is_reserve, false) OR COALESCE(af.is_cushion, false)) AS from_reserve
            FROM plan p
            LEFT JOIN categories c ON p.category_id = c.id
            LEFT JOIN accounts a  ON a.user_id=p.user_id AND a.name=p.account_to
            LEFT JOIN accounts af ON af.user_id=p.user_id AND af.name=p.account_from
            WHERE p.user_id=$1 AND p.id=$2
        """, user_id, body.ref_id)
    else:
        raise HTTPException(400, "unknown kind")
    if not occ:
        raise HTTPException(404, "plan occurrence not found")
    o = dict(occ)

    occ_d    = ot or o["date"]                       # occurrence (для года/месяца ключа)
    d        = body.date or o["date"]                # дата операции
    amount   = body.amount if body.amount is not None else float(o["amount"])
    acc_from = body.account_from or o["account_from"]
    acc_to   = body.account_to or o["account_to"]
    cat_id   = body.category_id if body.category_id is not None else o["category_id"]
    note     = body.note if body.note is not None else o["note"]
    title    = _title(o)
    yy, mm   = occ_d.year, occ_d.month

    tx_id = None
    if body.resolution == "paid":
        tx_id = await _insert_tx(db, user_id, d, amount, acc_from, acc_to, cat_id, note)

    await db.execute("""
        INSERT INTO plan_confirmations
          (user_id, kind, ref_id, year, month, occ_date, title, amount, resolution, tx_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT (user_id, kind, ref_id, year, month) DO UPDATE SET
          occ_date=$6, title=$7, amount=$8, resolution=$9, tx_id=$10, created_at=NOW()
    """, user_id, body.kind, body.ref_id, yy, mm, occ_d, title, amount, body.resolution, tx_id)

    # снять материализованную occurrence, чтобы STS/прогноз не задваивали
    if body.kind == "rule":
        await db.execute("""
            DELETE FROM plan WHERE user_id=$1 AND source_rule_id=$2
              AND EXTRACT(YEAR FROM date)::int=$3 AND EXTRACT(MONTH FROM date)::int=$4
        """, user_id, body.ref_id, yy, mm)
    else:
        await db.execute("DELETE FROM plan WHERE user_id=$1 AND id=$2", user_id, body.ref_id)

    return {"ok": True, "tx_id": tx_id}


class SkipBody(BaseModel):
    kind: str
    ref_id: int
    date: Optional[_Date] = None
    title: Optional[str] = None
    amount: Optional[float] = None


@router.post("/skip")
async def skip(body: SkipBody, request: Request):
    """«Не в этом месяце» — для правил/событий помечаем skip и снимаем occurrence.
    Для кредитов скип — клиентский (не персистим: долг никуда не делся)."""
    user_id = request.state.user_id
    db = request.state.db
    if body.kind not in ("rule", "plan"):
        return {"ok": True}   # loan snooze — на клиенте
    d = body.date or date.today()
    yy, mm = d.year, d.month
    await db.execute("""
        INSERT INTO plan_confirmations
          (user_id, kind, ref_id, year, month, occ_date, title, amount, resolution, tx_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'skip',NULL)
        ON CONFLICT (user_id, kind, ref_id, year, month) DO UPDATE SET
          resolution='skip', created_at=NOW()
    """, user_id, body.kind, body.ref_id, yy, mm, d, body.title, body.amount)
    if body.kind == "rule":
        await db.execute("""
            DELETE FROM plan WHERE user_id=$1 AND source_rule_id=$2
              AND EXTRACT(YEAR FROM date)::int=$3 AND EXTRACT(MONTH FROM date)::int=$4
        """, user_id, body.ref_id, yy, mm)
    else:
        await db.execute("DELETE FROM plan WHERE user_id=$1 AND id=$2", user_id, body.ref_id)
    return {"ok": True}


class RescheduleBody(BaseModel):
    kind: str                        # 'rule' | 'plan' (кредиты — даты из графика)
    ref_id: int
    occ_date: Optional[date] = None  # исходная дата occurrence (месяц для rule)
    new_date: date


@router.post("/reschedule")
async def reschedule(body: RescheduleBody, request: Request):
    """«Перенести» — сдвигает дату плановой occurrence, операцию НЕ создаёт.
    Событие (kind='plan') — двигаем саму строку plan; правило (kind='rule') —
    ставим pinned-строку на новую дату (перештамповка её не тронет). После
    переноса платёж уходит из очереди и вернётся, когда наступит новая дата."""
    user_id = request.state.user_id
    db = request.state.db

    if body.kind == "plan":
        res = await db.execute(
            "UPDATE plan SET date=$1 WHERE user_id=$2 AND id=$3",
            body.new_date, user_id, body.ref_id)
        if res.endswith("0"):
            raise HTTPException(404, "plan row not found")
        return {"ok": True}

    if body.kind == "rule":
        occ = body.occ_date or body.new_date
        rule = await db.fetchrow("""
            SELECT amount, account_from, account_to, category_id
            FROM plan_rules WHERE id=$1 AND user_id=$2
        """, body.ref_id, user_id)
        if not rule:
            raise HTTPException(404, "rule not found")
        async with db.transaction():
            await db.execute("""
                DELETE FROM plan WHERE user_id=$1 AND source_rule_id=$2
                  AND EXTRACT(YEAR FROM date)::int=$3 AND EXTRACT(MONTH FROM date)::int=$4
            """, user_id, body.ref_id, occ.year, occ.month)
            await db.execute("""
                INSERT INTO plan
                  (user_id, date, amount, account_from, account_to, category_id, source, source_rule_id, pinned)
                VALUES ($1,$2,$3,$4,$5,$6,'plan_rule',$7,true)
            """, user_id, body.new_date, rule["amount"], rule["account_from"],
                 rule["account_to"], rule["category_id"], body.ref_id)
        return {"ok": True}

    raise HTTPException(400, "reschedule not supported for this kind")
