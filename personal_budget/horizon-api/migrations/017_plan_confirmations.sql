-- ── Подтверждения плановых платежей («Спросить о платеже») ───────────────────
-- Фиксирует факт по датированной плановой occurrence: правило (rule) или разовое
-- событие/пополнение резерва (plan-строка). Кредиты идут отдельно через
-- loan_schedule.is_paid, здесь их НЕ храним.
--   resolution: 'paid'   — подтверждено, создана операция (tx_id);
--               'manual' — уже внесено вручную, операцию не создаём;
--               'skip'   — «не в этом месяце».
-- title/amount/occ_date дублируются, чтобы список статусов показывал уже
-- разрешённые occurrence даже после удаления материализованной plan-строки.
-- Идемпотентно.

CREATE TABLE IF NOT EXISTS plan_confirmations (
  id          SERIAL PRIMARY KEY,
  user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,               -- 'rule' | 'plan'
  ref_id      BIGINT NOT NULL,             -- rule_id (kind=rule) | plan.id (kind=plan)
  year        INTEGER NOT NULL,
  month       INTEGER NOT NULL,
  occ_date    DATE,
  title       TEXT,
  amount      NUMERIC(14,2),
  resolution  TEXT NOT NULL,               -- 'paid' | 'manual' | 'skip'
  tx_id       BIGINT,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_conf
  ON plan_confirmations(user_id, kind, ref_id, year, month);
CREATE INDEX IF NOT EXISTS idx_plan_conf_user_ym
  ON plan_confirmations(user_id, year, month);
