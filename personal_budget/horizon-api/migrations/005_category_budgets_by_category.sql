-- ============================================================
-- Migration 005 — конверты (category_budgets) на уровне КАТЕГОРИИ,
-- а не подкатегории. Подкатегории остаются только в транзакциях.
-- Идемпотентно.
-- ============================================================

-- 1) колонка категории
ALTER TABLE category_budgets ADD COLUMN IF NOT EXISTS category TEXT;

-- 2) бэкфилл из categories по старому category_id
UPDATE category_budgets cb
SET category = c.category
FROM categories c
WHERE cb.category_id = c.id
  AND (cb.category IS NULL OR cb.category = '');

-- 3) схлопнуть дубли (несколько подкатегорий одной категории за месяц) — сумма в одну строку
UPDATE category_budgets cb
SET budget = agg.total
FROM (
  SELECT user_id, category, year, month, SUM(budget) AS total, MIN(id) AS keep_id
  FROM category_budgets
  WHERE category IS NOT NULL
  GROUP BY user_id, category, year, month
) agg
WHERE cb.id = agg.keep_id;

DELETE FROM category_budgets cb
USING (
  SELECT user_id, category, year, month, MIN(id) AS keep_id
  FROM category_budgets
  WHERE category IS NOT NULL
  GROUP BY user_id, category, year, month
) k
WHERE cb.user_id = k.user_id AND cb.category = k.category
  AND cb.year = k.year AND cb.month = k.month
  AND cb.id <> k.keep_id;

-- 4) новый уникальный ключ по категории (старый — по category_id — убираем)
ALTER TABLE category_budgets DROP CONSTRAINT IF EXISTS category_budgets_unique;
DROP INDEX IF EXISTS category_budgets_unique;
CREATE UNIQUE INDEX IF NOT EXISTS category_budgets_cat_unique
  ON category_budgets (user_id, category, year, month);

-- 5) category_id больше не обязателен (оставляем для совместимости/истории)
ALTER TABLE category_budgets ALTER COLUMN category_id DROP NOT NULL;
