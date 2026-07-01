-- ── Приватный промокод (владелец + свои) + пожизненный доступ владельцу ───────
-- Отдельный код со своим лимитом — НЕ расходует 50 слотов целевого SUMMER50.
-- Идемпотентно (ON CONFLICT / UPDATE).

INSERT INTO promo_codes (code, kind, max_redemptions)
VALUES ('LIKEABOSS', 'free_forever', 25)
ON CONFLICT (code) DO NOTHING;

-- Владелец — пожизненный доступ напрямую, без расхода промо-слотов.
UPDATE users SET is_free_forever = true
WHERE id = '60703af0-df79-484a-b965-45d5662083c6';
