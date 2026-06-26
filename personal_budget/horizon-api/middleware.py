import os
import time
from collections import deque, defaultdict

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# /api/* эндпоинты, доступные без сессии (OAuth-флоу)
PUBLIC_API_PATHS = {
    "/api/auth/google", "/api/auth/google/callback",
    "/api/auth/yandex", "/api/auth/yandex/callback",
}

# ── CSRF: разрешённые источники изменяющих запросов ──────────────────────────
# При SameSite=Lax cookie не уходит в cross-site POST/PATCH/DELETE — основной CSRF
# уже закрыт. Доп. слой: на мутациях, если есть Origin и он не наш — отклоняем.
ALLOWED_ORIGINS = {
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS", "https://horizonapp.ru,https://www.horizonapp.ru"
    ).split(",") if o.strip()
}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# ── Rate limiting (in-process sliding window, без внешних зависимостей) ───────
# token-bucket/Redis из скилла избыточен для одного VPS — здесь скользящее окно
# в памяти процесса. Защита от brute-force/абьюза auth и от перегрузки API.
_BUCKETS: dict[str, deque] = defaultdict(deque)
AUTH_LIMIT, AUTH_WINDOW = 20, 300     # 20 запросов / 5 мин на IP к /api/auth/*
API_LIMIT,  API_WINDOW  = 600, 60     # 600 запросов / мин на IP к остальному /api/*


def _client_ip(request: Request) -> str:
    # За nginx реальный IP — в X-Forwarded-For / X-Real-IP.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    return request.client.host if request.client else "unknown"


def _rate_ok(key: str, limit: int, window: int):
    """Скользящее окно. Возвращает (allowed, retry_after_seconds)."""
    now = time.time()
    dq = _BUCKETS[key]
    cutoff = now - window
    while dq and dq[0] < cutoff:
        dq.popleft()
    if len(dq) >= limit:
        return False, int(dq[0] + window - now) + 1
    dq.append(now)
    # лёгкая защита от роста словаря: периодически чистим пустые корзины
    if len(_BUCKETS) > 20000:
        for k in [k for k, v in _BUCKETS.items() if not v]:
            _BUCKETS.pop(k, None)
    return True, 0


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # ── Rate limit + CSRF — только для API, до открытия соединения с БД ──
        if path.startswith("/api/"):
            ip = _client_ip(request)
            # Строгий лимит — только на инициацию входа/коллбэки OAuth.
            # /api/auth/me и /logout зовутся часто (каждая загрузка) → общий лимит.
            auth_strict = (path.startswith("/api/auth/")
                           and not path.endswith("/me")
                           and not path.endswith("/logout"))
            if auth_strict:
                ok, retry = _rate_ok(f"auth:{ip}", AUTH_LIMIT, AUTH_WINDOW)
            else:
                ok, retry = _rate_ok(f"api:{ip}", API_LIMIT, API_WINDOW)
            if not ok:
                return JSONResponse(
                    {"detail": "Слишком много запросов, попробуй позже"},
                    status_code=429, headers={"Retry-After": str(retry)},
                )
            # CSRF: на изменяющих запросах проверяем источник
            if request.method in MUTATING:
                origin = request.headers.get("origin")
                if origin and origin not in ALLOWED_ORIGINS:
                    return JSONResponse(
                        {"detail": "Недопустимый источник запроса"}, status_code=403)

        # Inject DB connection
        async with request.app.state.pool.acquire() as conn:
            request.state.db = conn

            # Защищаем ТОЛЬКО API (кроме auth-флоу). Статика, SPA (/), /health,
            # /docs — публичны: SPA сам обращается к /api/auth/me и решает, что показать.
            if not path.startswith("/api/") or path in PUBLIC_API_PATHS:
                return await call_next(request)

            token = request.cookies.get("session")
            if not token:
                # ВАЖНО: HTTPException внутри middleware отдаётся как 500 — возвращаем ответ напрямую
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)

            row = await conn.fetchrow("""
                SELECT u.* FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = $1 AND s.expires_at > NOW()
            """, token)

            if not row:
                return JSONResponse({"detail": "Session expired"}, status_code=401)

            request.state.user = dict(row)
            request.state.user_id = row["id"]

            return await call_next(request)
