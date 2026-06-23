-- ============================================================
-- Migration 006 — нормализация character в 3 значения:
--   Повседневный / Фиксированный / Эпизодический.
-- Приоритет: эпизод → фикс → повседневный (как и выводит код сейчас).
-- expense_type НЕ трогаем (дропнем отдельной миграцией после перевода кода).
-- Идемпотентно.
-- ============================================================

UPDATE categories SET character = CASE
  WHEN character IN ('Эпизодический', 'episodic')            THEN 'Эпизодический'
  WHEN character IN ('Фиксированный', 'fixed')
       OR expense_type = 'fixed'                              THEN 'Фиксированный'
  ELSE 'Повседневный'
END;

-- expense_type держим синхронным с нормализованным character
UPDATE categories SET expense_type =
  CASE WHEN character = 'Фиксированный' THEN 'fixed' ELSE 'variable' END;
