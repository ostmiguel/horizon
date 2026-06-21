-- ============================================================
-- Migration 003 — связь plan ↔ plan_rules для материализации
-- Идемпотентно.
-- ============================================================

ALTER TABLE plan
  ADD COLUMN IF NOT EXISTS source_rule_id INTEGER REFERENCES plan_rules(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_plan_source_rule_id ON plan(source_rule_id);
