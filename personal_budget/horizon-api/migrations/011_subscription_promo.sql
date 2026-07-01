-- ── Подписка / триал / промокоды ─────────────────────────────────────────────
-- Таблица users создана вне миграций, поэтому строго ADD COLUMN IF NOT EXISTS.
-- Идемпотентно (ON_ERROR_STOP на деплое, повторный прогон безопасен).

ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE users ADD COLUMN IF NOT EXISTS paid_until       TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_free_forever  BOOLEAN NOT NULL DEFAULT false;

-- Старт триала существующим пользователям — сейчас (их немного: владелец + тест).
UPDATE users SET trial_started_at = NOW() WHERE trial_started_at IS NULL;

-- ── Промокоды (общие коды с лимитом активаций) ───────────────────────────────
CREATE TABLE IF NOT EXISTS promo_codes (
  code            TEXT PRIMARY KEY,
  kind            TEXT NOT NULL DEFAULT 'free_forever',
  max_redemptions INTEGER NOT NULL,
  redeemed_count  INTEGER NOT NULL DEFAULT 0,
  is_active       BOOLEAN NOT NULL DEFAULT true,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Одна активация на пользователя (UNIQUE user_id) — промокод нельзя применить дважды.
CREATE TABLE IF NOT EXISTS promo_redemptions (
  id          SERIAL PRIMARY KEY,
  code        TEXT NOT NULL REFERENCES promo_codes(code),
  user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  redeemed_at TIMESTAMPTZ DEFAULT NOW(),
  CONSTRAINT promo_redemptions_user_unique UNIQUE (user_id)
);
CREATE INDEX IF NOT EXISTS idx_promo_redemptions_code ON promo_redemptions(code);

-- Первые 50 по коду SUMMER50 → пожизненный бесплатный доступ.
INSERT INTO promo_codes (code, kind, max_redemptions)
VALUES ('SUMMER50', 'free_forever', 50)
ON CONFLICT (code) DO NOTHING;
