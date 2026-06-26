-- ============================================================
-- Migration 008 — развязка хардкода кредитов + системные категории.
--
-- categories.role — служебная метка категории, на которую ссылается код
--   (loan_principal / loan_interest). Код ищет категорию по role, а не по
--   захардкоженному id или имени. Системные категории нельзя удалять.
-- loans.account_from — счёт списания платежа по кредиту (раньше было
--   захардкожено 'Карта Тбанк'). Бэкфилл — первым активом пользователя.
-- Идемпотентно.
-- ============================================================

ALTER TABLE categories ADD COLUMN IF NOT EXISTS role TEXT;

-- Бэкфилл role для уже существующих кредитных категорий (по группе/имени,
-- без привязки к id). Для свежей БД — no-op; новым юзерам role ставит посев.
UPDATE categories SET role='loan_principal'
  WHERE role IS NULL AND group_name='Обязательства' AND category='Кредиты'
    AND subcategory='Кредиты - тело';
UPDATE categories SET role='loan_interest'
  WHERE role IS NULL AND group_name='Расходы' AND category='Кредиты'
    AND subcategory='Кредиты - процент';

ALTER TABLE loans ADD COLUMN IF NOT EXISTS account_from TEXT;

-- Бэкфилл счёта списания: первый актив пользователя (без хардкода имени).
UPDATE loans l SET account_from = (
  SELECT a.name FROM accounts a
  WHERE a.user_id = l.user_id AND a.account_type = 'Актив' AND a.is_active = true
  ORDER BY a.id LIMIT 1
)
WHERE l.account_from IS NULL;
