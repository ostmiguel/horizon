-- ============================================================
-- Migration 000 — BASELINE: объекты, созданные на проде вручную
-- (вне версионирования). Делает схему воспроизводимой на клоне/редеплое.
--
-- ⚠️ РЕКОНСТРУКЦИЯ ИЗ КОДА, не из pg_dump (на момент написания не было доступа
--    к боевой БД). Идемпотентно (IF NOT EXISTS) — на существующем проде НИЧЕГО
--    не меняет, на свежей БД создаёт недостающее.
--    Авторитетный дамп снять на сервере и сверить (см. deploy/README.md):
--      pg_dump --schema-only --no-owner --no-privileges "$DATABASE_URL" > schema.sql
--
-- Зависит от базовых таблиц users/accounts/categories (создаются из schema_v2.sql).
-- Прогоняется ПЕРВОЙ (по алфавиту) деплой-воркфлоу.
-- ============================================================

-- ── accounts: флаги (is_reserve/is_cushion дублируют 001 — безопасно) ─────────
ALTER TABLE accounts
  ADD COLUMN IF NOT EXISTS is_reserve     BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS is_cushion     BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS is_operational BOOLEAN;  -- legacy, кодом не используется

-- ── categories: expense_type (дублирует 001 — безопасно) ─────────────────────
ALTER TABLE categories
  ADD COLUMN IF NOT EXISTS expense_type TEXT;

-- ── goals ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS goals (
  id            SERIAL PRIMARY KEY,
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  target_amount NUMERIC(14,2) NOT NULL,
  account_id    INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
  due_date      DATE,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_goals_user_id ON goals(user_id);

-- ── plan_rules ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS plan_rules (
  id           SERIAL PRIMARY KEY,
  user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  amount       NUMERIC(14,2) NOT NULL,
  account_from TEXT NOT NULL,
  account_to   TEXT NOT NULL,
  category_id  INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  day_of_month INTEGER,
  is_active    BOOLEAN DEFAULT true,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_plan_rules_user_id ON plan_rules(user_id);

-- ── category_budgets ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS category_budgets (
  id          SERIAL PRIMARY KEY,
  user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  year        INTEGER NOT NULL,
  month       INTEGER NOT NULL,
  budget      NUMERIC(14,2) NOT NULL,
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  CONSTRAINT category_budgets_unique UNIQUE (user_id, category_id, year, month)
);
CREATE INDEX IF NOT EXISTS idx_category_budgets_user_id ON category_budgets(user_id);

-- ── forecast_snapshots ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS forecast_snapshots (
  id            SERIAL PRIMARY KEY,
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  snapshot_date DATE NOT NULL,
  month_end     DATE,
  b0            NUMERIC(14,2),
  i_remain      NUMERIC(14,2),
  f_remain      NUMERIC(14,2),
  v_remain      NUMERIC(14,2),
  sts_forecast  NUMERIC(14,2),
  actual_b0     NUMERIC(14,2),
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  CONSTRAINT forecast_snapshots_unique UNIQUE (user_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_forecast_snapshots_user_id ON forecast_snapshots(user_id);
