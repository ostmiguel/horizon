-- ============================================================
-- Migration 001 — add is_reserve, is_cushion to accounts
--                 add expense_type to categories
-- Run once on the live DB, then update accounts manually
-- ============================================================

-- ── 1. Accounts: reserve and cushion flags ───────────────────
ALTER TABLE accounts
  ADD COLUMN IF NOT EXISTS is_reserve BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS is_cushion BOOLEAN NOT NULL DEFAULT false;

-- ── 2. Categories: expense type ──────────────────────────────
ALTER TABLE categories
  ADD COLUMN IF NOT EXISTS expense_type TEXT;

-- Populate from character:
--   'Фиксированный' → 'fixed'
--   everything else (Повседневный, Эпизодический, NULL) → 'variable'
UPDATE categories
SET expense_type = CASE
  WHEN character = 'Фиксированный' THEN 'fixed'
  ELSE 'variable'
END
WHERE expense_type IS NULL;

-- ── 3. Mark reserve accounts ─────────────────────────────────
-- Edit the list below to match your actual account names,
-- then run this block.
--
-- UPDATE accounts SET is_reserve = true
-- WHERE name IN (
--   'Резерв',
--   'Счет - Дом',
--   'Счет - Дочка',
--   'Счет - Здоровье',
--   'Счет - Путешествия'
-- );
--
-- UPDATE accounts SET is_cushion = true
-- WHERE name = 'Подушка';   -- если есть отдельный счёт подушки

-- ── 4. Verify ────────────────────────────────────────────────
-- SELECT name, account_type, is_reserve, is_cushion, include_in_balance
-- FROM accounts ORDER BY account_type, name;
--
-- SELECT category, subcategory, character, expense_type
-- FROM categories ORDER BY category, subcategory;
