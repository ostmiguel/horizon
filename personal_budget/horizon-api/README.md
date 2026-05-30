# Horizon API

Python FastAPI backend для приложения Horizon.

## Структура

```
horizon-api/
├── main.py              # FastAPI app, lifespan, роутеры
├── middleware.py        # Auth middleware (сессии)
├── requirements.txt     # Зависимости
├── .env.example         # Пример переменных окружения
├── routers/
│   ├── auth.py          # OAuth Google + Яндекс
│   ├── accounts.py      # Счета
│   ├── transactions.py  # Транзакции и план
│   ├── categories.py    # Категории
│   ├── loans.py         # Кредиты и графики
│   └── recurring.py     # Повторяющиеся платежи
└── static/
    └── index.html       # Фронтенд (budget-v2.html → переименовать)
```

## Запуск локально

```bash
# 1. Создай виртуальное окружение
python3 -m venv venv
source venv/bin/activate

# 2. Установи зависимости
pip install -r requirements.txt

# 3. Создай .env файл
cp .env.example .env
# Заполни DATABASE_URL и OAuth ключи

# 4. Применить схему БД
psql $DATABASE_URL < ../schema_v2.sql

# 5. Запустить
uvicorn main:app --reload --port 8000
```

## API эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | /api/auth/google | Войти через Google |
| GET | /api/auth/yandex | Войти через Яндекс |
| GET | /api/auth/me | Текущий пользователь |
| POST | /api/auth/logout | Выйти |
| GET | /api/accounts | Список счетов с балансами |
| POST | /api/accounts | Создать счёт |
| PATCH | /api/accounts/{id} | Обновить счёт |
| DELETE | /api/accounts/{id} | Удалить счёт |
| GET | /api/transactions | Список транзакций (?year=&month=&plan=) |
| POST | /api/transactions | Создать транзакцию (?plan=true) |
| PATCH | /api/transactions/{id} | Обновить (?plan=true) |
| DELETE | /api/transactions/{id} | Удалить (?plan=true) |
| GET | /api/categories | Список категорий |
| POST | /api/categories | Создать категорию |
| PATCH | /api/categories/{id} | Обновить |
| DELETE | /api/categories/{id} | Удалить |
| GET | /api/loans | Список кредитов |
| POST | /api/loans | Создать кредит |
| GET | /api/loans/{id}/schedule | График платежей |
| POST | /api/loans/{id}/schedule | Сохранить график |
| PATCH | /api/loans/{id}/schedule/{month} | Обновить строку |
| GET | /api/recurring | Повторяющиеся платежи |
| POST | /api/recurring | Создать + сгенерировать план |
| DELETE | /api/recurring/{id} | Удалить |

## Деплой на сервер

```bash
# На сервере
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Nginx конфиг и systemd unit будут добавлены отдельно.
