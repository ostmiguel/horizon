-- ============================================================
-- HORIZON — Multi-user Database Schema v2
-- PostgreSQL (without Supabase)
-- ============================================================


-- ── EXTENSIONS ──────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";


-- ── USERS ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  email         TEXT NOT NULL UNIQUE,
  name          TEXT,
  avatar_url    TEXT,
  provider      TEXT NOT NULL CHECK (provider IN ('google','yandex','email')),
  provider_id   TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  last_login    TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (provider, provider_id)
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);


-- ── ACCOUNTS ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS accounts (
  id                 SERIAL PRIMARY KEY,
  user_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name               TEXT NOT NULL,
  account_type       TEXT NOT NULL CHECK (account_type IN ('Актив','Пассив','Поток')),
  color              TEXT DEFAULT '#b0c4de',
  initial_balance    NUMERIC(14,2) DEFAULT 0,
  include_in_balance BOOLEAN DEFAULT true,
  is_active          BOOLEAN DEFAULT true,
  sort_order         INTEGER DEFAULT 0,
  created_at         TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_accounts_user_id ON accounts(user_id);


-- ── CATEGORIES ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS categories (
  id           SERIAL PRIMARY KEY,
  user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  group_name   TEXT NOT NULL,
  category     TEXT NOT NULL,
  subcategory  TEXT NOT NULL,
  character    TEXT,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_categories_user_id ON categories(user_id);


-- ── PARTICIPANTS ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS participants (
  id         SERIAL PRIMARY KEY,
  user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  color      TEXT DEFAULT '#4682b4',
  is_default BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT NOW()
);


-- ── TRANSACTIONS ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transactions (
  id             SERIAL PRIMARY KEY,
  user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  date           DATE NOT NULL,
  amount         NUMERIC(14,2) NOT NULL CHECK (amount > 0),
  account_from   TEXT NOT NULL,
  account_to     TEXT NOT NULL,
  category_id    INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  participant_id INTEGER REFERENCES participants(id) ON DELETE SET NULL,
  note           TEXT,
  source         TEXT DEFAULT 'manual',
  created_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transactions_user_id   ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_date       ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_user_date  ON transactions(user_id, date);


-- ── PLAN ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS plan (
  id             SERIAL PRIMARY KEY,
  user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  date           DATE NOT NULL,
  amount         NUMERIC(14,2) NOT NULL CHECK (amount > 0),
  account_from   TEXT NOT NULL,
  account_to     TEXT NOT NULL,
  category_id    INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  participant_id INTEGER REFERENCES participants(id) ON DELETE SET NULL,
  note           TEXT,
  source         TEXT DEFAULT 'manual',
  created_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plan_user_id   ON plan(user_id);
CREATE INDEX IF NOT EXISTS idx_plan_date       ON plan(date);
CREATE INDEX IF NOT EXISTS idx_plan_user_date  ON plan(user_id, date);


-- ── LOANS ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loans (
  id                SERIAL PRIMARY KEY,
  user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
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

CREATE INDEX IF NOT EXISTS idx_loans_user_id ON loans(user_id);


-- ── LOAN SCHEDULE ────────────────────────────────────────────
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

CREATE INDEX IF NOT EXISTS idx_loan_schedule_loan_id ON loan_schedule(loan_id);


-- ── RECURRING ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS recurring (
  id            SERIAL PRIMARY KEY,
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  amount        NUMERIC(14,2) NOT NULL CHECK (amount > 0),
  account_from  TEXT NOT NULL,
  account_to    TEXT NOT NULL,
  category_id   INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  note          TEXT,
  frequency     TEXT NOT NULL CHECK (frequency IN ('monthly','weekly','daily')),
  start_date    DATE NOT NULL,
  end_date      DATE,
  is_active     BOOLEAN DEFAULT true,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recurring_user_id ON recurring(user_id);


-- ── SESSIONS (для авторизации) ────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token         TEXT NOT NULL UNIQUE DEFAULT encode(gen_random_bytes(32), 'hex'),
  expires_at    TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '30 days',
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sessions_token   ON sessions(token);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);

-- Auto-delete expired sessions
CREATE OR REPLACE FUNCTION cleanup_sessions() RETURNS void AS $$
  DELETE FROM sessions WHERE expires_at < NOW();
$$ LANGUAGE SQL;


-- ============================================================
-- MIGRATION FROM SUPABASE (single-user → multi-user)
-- Run after creating a user account:
-- UPDATE accounts      SET user_id = '<your-uuid>';
-- UPDATE categories    SET user_id = '<your-uuid>';
-- UPDATE participants  SET user_id = '<your-uuid>';
-- UPDATE transactions  SET user_id = '<your-uuid>';
-- UPDATE plan          SET user_id = '<your-uuid>';
-- UPDATE loans         SET user_id = '<your-uuid>';
-- UPDATE recurring     SET user_id = '<your-uuid>';
-- ============================================================
