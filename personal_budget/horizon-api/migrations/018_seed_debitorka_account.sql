-- ── Системный счёт «Дебиторка» (Актив) — единый скелет для всех пользователей ──
-- «Мне должны»: общий пул-Актив, хранит остаток (сколько тебе должны). Операции:
--   дал в долг  — карта → Дебиторка (кэш −, требование +);
--   мне вернули — Дебиторка → карта (требование −, кэш +).
-- include_in_balance=false → НЕ в операционном балансе/«Свободно» (нельзя тратить),
-- но входит в чистый капитал (total_assets = сумма всех Актив). used_for_payment=
-- false → не предлагается как счёт оплаты расходов. Системный (rename-only,
-- неудаляемый — см. фронт). Идемпотентно: создаём только тем, у кого ещё нет.

INSERT INTO accounts
  (user_id, name, account_type, color, initial_balance, include_in_balance,
   is_reserve, is_cushion, used_for_payment)
SELECT u.id, 'Дебиторка', 'Актив', '#2AA7CC', 0, false, false, false, false
FROM users u
WHERE NOT EXISTS (
  SELECT 1 FROM accounts a WHERE a.user_id = u.id AND a.name = 'Дебиторка'
);
