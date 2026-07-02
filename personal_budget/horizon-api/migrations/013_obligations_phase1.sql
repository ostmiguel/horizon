-- ── Консолидация Обязательств: Фаза 1 (фундамент) ────────────────────────────
-- Модель ledger-first: операции = источник истины, счёт «Обязательства» —
-- системный Пассив с балансом из операций. Идемпотентно.

-- 1) Тип обязательства: credit (график тело/процент) | debt (без графика).
--    (таблица loans создана вне миграций → ADD COLUMN IF NOT EXISTS)
ALTER TABLE loans ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'credit';

-- 2) Системный счёт «Обязательства» (Пассив) — автосоздать всем, у кого нет.
--    Баланс считается из операций; initial_balance = 0 (не ручной ввод).
--    У кого счёт уже есть (напр. владелец с ручным балансом) — НЕ трогаем;
--    его реконсиляция — отдельный осознанный шаг.
INSERT INTO accounts (user_id, name, account_type, color, initial_balance, include_in_balance, is_reserve, is_cushion)
SELECT u.id, 'Обязательства', 'Пассив', '#C0741A', 0, false, false, false
FROM users u
WHERE NOT EXISTS (
  SELECT 1 FROM accounts a WHERE a.user_id = u.id AND a.name = 'Обязательства'
);
