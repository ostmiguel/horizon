-- ============================================================
-- Migration 007 — надёжные помесячные правки/удаления правил.
--
-- pinned: плановая строка правила, правленная вручную «только этот месяц».
--   Материализация её НЕ пересоздаёт (иначе ручная правка слетала бы при
--   следующей перештамповке правил).
-- plan_rule_skips: «удалить только этот месяц» — материализация не воссоздаёт
--   строку правила в этом (год, месяц).
-- Идемпотентно.
-- ============================================================

ALTER TABLE plan ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS plan_rule_skips (
  user_id  UUID    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  rule_id  INTEGER NOT NULL REFERENCES plan_rules(id) ON DELETE CASCADE,
  year     INTEGER NOT NULL,
  month    INTEGER NOT NULL,
  PRIMARY KEY (rule_id, year, month)
);
CREATE INDEX IF NOT EXISTS idx_plan_rule_skips_user ON plan_rule_skips(user_id);
