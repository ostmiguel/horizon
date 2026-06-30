-- Migration 010 — снять users_provider_check.
-- Ограничение users_provider_check создавалось вне репозитория со старым списком
-- провайдеров (google/yandex) и блокировало вход через Mail.ru
-- ("violates check constraint users_provider_check", provider='mailru').
-- `provider` задаётся сервером в OAuth-хендлерах (не пользовательский ввод),
-- поэтому CHECK не нужен — снимаем, разблокируя mailru и будущие провайдеры.
-- Идемпотентно.

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_provider_check;
