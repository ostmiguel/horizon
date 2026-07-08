-- Per-user отображаемая валюта (моно-режим: суммы НЕ конвертируются, меняется
-- только символ). NULL/пусто трактуется фронтом как ₽ по умолчанию.
ALTER TABLE users ADD COLUMN IF NOT EXISTS currency TEXT;
