-- ============================================================
-- Migration 002 — переезд с Google на Яндекс
--
-- ЗАПУСКАТЬ ТОЛЬКО ОДИН РАЗ, после первого входа через Яндекс.
--
-- Шаг 0. Убедиться что пользователь уже вошёл через Яндекс
--         (иначе новой строки в users ещё нет).
-- ============================================================

-- ── Посмотреть текущих пользователей ────────────────────────
-- SELECT id, email, provider, provider_id, created_at
-- FROM users ORDER BY created_at;
--
-- Из вывода получить:
--   OLD_ID = UUID google-пользователя (084f18c0-927d-444e-a9d0-7313042d7163)
--   NEW_ID = UUID яндекс-пользователя (появится после первого входа)


-- ============================================================
-- СЦЕНАРИЙ A: email совпадает (ost.miguel@gmail.com в Яндексе)
-- ON CONFLICT уже переключил provider на 'yandex'.
-- Никакой миграции данных не нужно — всё под тем же user_id.
-- Достаточно проверить:
-- ============================================================
--
-- SELECT id, email, provider, provider_id FROM users WHERE email = 'ost.miguel@gmail.com';
-- Должно вернуть: provider='yandex'


-- ============================================================
-- СЦЕНАРИЙ B: email другой (например, ostmiguel@yandex.ru)
-- После первого Яндекс-входа создался новый пользователь.
-- Нужно перенести все данные и удалить старого.
-- Подставить OLD_ID и NEW_ID из вывода выше.
-- ============================================================

-- BEGIN;

-- SET LOCAL old_id = '084f18c0-927d-444e-a9d0-7313042d7163';  -- google user
-- SET LOCAL new_id = '<UUID нового яндекс-пользователя>';

-- UPDATE accounts      SET user_id = current_setting('new_id')::uuid WHERE user_id = current_setting('old_id')::uuid;
-- UPDATE categories    SET user_id = current_setting('new_id')::uuid WHERE user_id = current_setting('old_id')::uuid;
-- UPDATE participants  SET user_id = current_setting('new_id')::uuid WHERE user_id = current_setting('old_id')::uuid;
-- UPDATE transactions  SET user_id = current_setting('new_id')::uuid WHERE user_id = current_setting('old_id')::uuid;
-- UPDATE plan          SET user_id = current_setting('new_id')::uuid WHERE user_id = current_setting('old_id')::uuid;
-- UPDATE loans         SET user_id = current_setting('new_id')::uuid WHERE user_id = current_setting('old_id')::uuid;
-- UPDATE recurring     SET user_id = current_setting('new_id')::uuid WHERE user_id = current_setting('old_id')::uuid;
-- UPDATE goals         SET user_id = current_setting('new_id')::uuid WHERE user_id = current_setting('old_id')::uuid;
-- UPDATE sessions      SET user_id = current_setting('new_id')::uuid WHERE user_id = current_setting('old_id')::uuid;

-- -- Проверить что всё перенеслось:
-- SELECT 'accounts' AS tbl, count(*) FROM accounts WHERE user_id = current_setting('new_id')::uuid
-- UNION ALL SELECT 'transactions', count(*) FROM transactions WHERE user_id = current_setting('new_id')::uuid
-- UNION ALL SELECT 'loans',        count(*) FROM loans        WHERE user_id = current_setting('new_id')::uuid;

-- -- Удалить старого пользователя (CASCADE удалит остаточные сессии и т.п.)
-- DELETE FROM users WHERE id = current_setting('old_id')::uuid;

-- COMMIT;
