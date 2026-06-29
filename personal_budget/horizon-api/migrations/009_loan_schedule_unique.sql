-- Migration 009 — loan_schedule: гарантировать таблицу и UNIQUE(loan_id, month_num).
-- Таблица создавалась вне репозитория (её не было ни в одной миграции). Все записи
-- идут через INSERT ... ON CONFLICT (loan_id, month_num), который БЕЗ уникального
-- индекса падает → отметка «оплачено» в графике кредита не сохранялась.
-- Идемпотентно: существующую таблицу не трогаем, только добавляем недостающий индекс.

CREATE TABLE IF NOT EXISTS loan_schedule (
  id            SERIAL PRIMARY KEY,
  loan_id       INTEGER NOT NULL REFERENCES loans(id) ON DELETE CASCADE,
  month_num     INTEGER NOT NULL,
  date          DATE,
  payment       NUMERIC(14,2),
  principal     NUMERIC(14,2),
  interest      NUMERIC(14,2),
  balance       NUMERIC(14,2),
  extra_payment NUMERIC(14,2),
  is_paid       BOOLEAN DEFAULT false
);

CREATE UNIQUE INDEX IF NOT EXISTS loan_schedule_loan_month_uniq
  ON loan_schedule (loan_id, month_num);
