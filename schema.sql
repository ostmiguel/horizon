-- ============================================================
-- PERSONAL BUDGET APP — Database Schema
-- ============================================================
-- Run this in Supabase SQL Editor to create the full structure
-- After running: add your accounts, categories, participants
-- ============================================================


-- ── EXTENSIONS ──────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";


-- ── TABLES ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS accounts (
  id               SERIAL PRIMARY KEY,
  name             TEXT NOT NULL UNIQUE,
  account_type     TEXT NOT NULL CHECK (account_type IN ('Актив', 'Пассив', 'Поток')),
  color            TEXT DEFAULT '#b0c4de',
  initial_balance  NUMERIC(14,2) DEFAULT 0,
  include_in_balance BOOLEAN DEFAULT true,
  is_active        BOOLEAN DEFAULT true,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS categories (
  id           SERIAL PRIMARY KEY,
  group_name   TEXT NOT NULL,
  category     TEXT NOT NULL,
  subcategory  TEXT NOT NULL,
  character    TEXT,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS participants (
  id         SERIAL PRIMARY KEY,
  name       TEXT NOT NULL,
  color      TEXT DEFAULT '#4682b4',
  is_default BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS transactions (
  id             SERIAL PRIMARY KEY,
  date           DATE NOT NULL,
  amount         NUMERIC(14,2) NOT NULL CHECK (amount > 0),
  account_from   TEXT NOT NULL,
  account_to     TEXT NOT NULL,
  category_id    INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  participant_id INTEGER REFERENCES participants(id) ON DELETE SET NULL,
  note           TEXT,
  use_in_forecast BOOLEAN DEFAULT true,
  source         TEXT DEFAULT 'manual',
  dow   SMALLINT GENERATED ALWAYS AS (EXTRACT(DOW FROM date)::SMALLINT) STORED,
  month SMALLINT GENERATED ALWAYS AS (EXTRACT(MONTH FROM date)::SMALLINT) STORED,
  year  SMALLINT GENERATED ALWAYS AS (EXTRACT(YEAR FROM date)::SMALLINT) STORED,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS plan (
  id             SERIAL PRIMARY KEY,
  date           DATE NOT NULL,
  amount         NUMERIC(14,2) NOT NULL CHECK (amount > 0),
  account_from   TEXT NOT NULL,
  account_to     TEXT NOT NULL,
  category_id    INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  participant_id INTEGER REFERENCES participants(id) ON DELETE SET NULL,
  note           TEXT,
  source         TEXT DEFAULT 'manual',
  dow   SMALLINT GENERATED ALWAYS AS (EXTRACT(DOW FROM date)::SMALLINT) STORED,
  month SMALLINT GENERATED ALWAYS AS (EXTRACT(MONTH FROM date)::SMALLINT) STORED,
  year  SMALLINT GENERATED ALWAYS AS (EXTRACT(YEAR FROM date)::SMALLINT) STORED,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS loans (
  id                SERIAL PRIMARY KEY,
  name              TEXT NOT NULL,
  initial_amount    NUMERIC(14,2) NOT NULL,
  current_balance   NUMERIC(14,2) NOT NULL,
  rate              NUMERIC(6,4) NOT NULL,
  monthly_payment   NUMERIC(14,2) NOT NULL,
  payment_number    INTEGER DEFAULT 1,
  total_payments    INTEGER NOT NULL,
  next_payment_date DATE,
  color             TEXT DEFAULT '#e24b4a',
  is_active         BOOLEAN DEFAULT true,
  created_at        TIMESTAMPTZ DEFAULT NOW(),
  updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS loan_schedule (
  id            SERIAL PRIMARY KEY,
  loan_id       INTEGER REFERENCES loans(id) ON DELETE CASCADE,
  month_num     INTEGER NOT NULL,
  date          DATE,
  payment       NUMERIC(14,2),
  principal     NUMERIC(14,2),
  interest      NUMERIC(14,2),
  balance       NUMERIC(14,2),
  extra_payment NUMERIC(14,2),
  is_paid       BOOLEAN DEFAULT false,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  CONSTRAINT loan_schedule_loan_month_unique UNIQUE (loan_id, month_num)
);


-- ── INDEXES ─────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_transactions_date          ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_year_month    ON transactions(year, month);
CREATE INDEX IF NOT EXISTS idx_transactions_account_from  ON transactions(account_from);
CREATE INDEX IF NOT EXISTS idx_transactions_account_to    ON transactions(account_to);
CREATE INDEX IF NOT EXISTS idx_transactions_category_id   ON transactions(category_id);

CREATE INDEX IF NOT EXISTS idx_plan_date          ON plan(date);
CREATE INDEX IF NOT EXISTS idx_plan_year_month    ON plan(year, month);

CREATE INDEX IF NOT EXISTS idx_loan_schedule_loan_id ON loan_schedule(loan_id);


-- ── VIEWS ───────────────────────────────────────────────────

CREATE OR REPLACE VIEW account_balance AS
SELECT
  a.name,
  a.account_type,
  a.color,
  a.include_in_balance,
  a.initial_balance,
  COALESCE(
    a.initial_balance
    + SUM(CASE WHEN t.account_to   = a.name THEN  t.amount ELSE 0 END)
    - SUM(CASE WHEN t.account_from = a.name THEN  t.amount ELSE 0 END),
    a.initial_balance
  ) AS current_balance
FROM accounts a
LEFT JOIN transactions t ON t.account_from = a.name OR t.account_to = a.name
GROUP BY a.id, a.name, a.account_type, a.color, a.include_in_balance, a.initial_balance
ORDER BY a.id;

CREATE OR REPLACE VIEW monthly_summary AS
SELECT
  year, month,
  SUM(CASE WHEN account_from = 'Доход'  THEN amount ELSE 0 END) AS income,
  SUM(CASE WHEN account_to   = 'Расход' THEN amount ELSE 0 END) AS expense,
  SUM(CASE WHEN account_from = 'Доход'  THEN amount ELSE 0 END)
  - SUM(CASE WHEN account_to = 'Расход' THEN amount ELSE 0 END) AS result
FROM transactions
GROUP BY year, month
ORDER BY year, month;

CREATE OR REPLACE VIEW category_expense_summary AS
SELECT
  c.group_name, c.category, c.subcategory,
  t.year, t.month,
  SUM(t.amount) AS total
FROM transactions t
JOIN categories c ON t.category_id = c.id
WHERE t.account_to = 'Расход'
GROUP BY c.group_name, c.category, c.subcategory, t.year, t.month;


-- ── SECURITY ────────────────────────────────────────────────

ALTER TABLE accounts      DISABLE ROW LEVEL SECURITY;
ALTER TABLE categories    DISABLE ROW LEVEL SECURITY;
ALTER TABLE participants  DISABLE ROW LEVEL SECURITY;
ALTER TABLE transactions  DISABLE ROW LEVEL SECURITY;
ALTER TABLE plan          DISABLE ROW LEVEL SECURITY;
ALTER TABLE loans         DISABLE ROW LEVEL SECURITY;
ALTER TABLE loan_schedule DISABLE ROW LEVEL SECURITY;

GRANT ALL ON accounts      TO anon, authenticated;
GRANT ALL ON categories    TO anon, authenticated;
GRANT ALL ON participants  TO anon, authenticated;
GRANT ALL ON transactions  TO anon, authenticated;
GRANT ALL ON plan          TO anon, authenticated;
GRANT ALL ON loans         TO anon, authenticated;
GRANT ALL ON loan_schedule TO anon, authenticated;

GRANT SELECT ON account_balance          TO anon, authenticated;
GRANT SELECT ON monthly_summary          TO anon, authenticated;
GRANT SELECT ON category_expense_summary TO anon, authenticated;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated;


-- ── DEFAULT DATA ─────────────────────────────────────────────

INSERT INTO accounts (name, account_type, color, initial_balance, include_in_balance) VALUES
  ('Карта (основная)', 'Актив',  '#4682B4', 0, true),
  ('Наличные',         'Актив',  '#888780', 0, true),
  ('Резерв',           'Актив',  '#B0C4DE', 0, false),
  ('Обязательства',    'Пассив', '#CD5C5C', 0, false),
  ('Доход',            'Поток',  '#6B8E23', 0, false),
  ('Расход',           'Поток',  '#FF4500', 0, false)
ON CONFLICT (name) DO NOTHING;

INSERT INTO participants (name, color, is_default) VALUES
  ('Я', '#4682B4', true)
ON CONFLICT DO NOTHING;

-- ============================================================
-- NEXT STEPS:
-- 1. Add your accounts in the app (References → Accounts)
-- 2. Add your categories or import from a template
-- 3. Set initial balances to your current account balances
-- 4. Start adding transactions
-- 5. Add loans in the Loans section if needed
-- ============================================================

CREATE TABLE IF NOT EXISTS recurring (
  id            SERIAL PRIMARY KEY,
  amount        NUMERIC(14,2) NOT NULL CHECK (amount > 0),
  account_from  TEXT NOT NULL,
  account_to    TEXT NOT NULL,
  category_id   INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  note          TEXT,
  frequency     TEXT NOT NULL CHECK (frequency IN ('monthly','weekly','daily')),
  start_date    DATE NOT NULL,
  end_date      DATE,
  day_of_month  INTEGER,
  is_active     BOOLEAN DEFAULT true,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE recurring DISABLE ROW LEVEL SECURITY;
GRANT ALL ON recurring TO anon, authenticated;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated;
