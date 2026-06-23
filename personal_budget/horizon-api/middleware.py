from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# /api/* эндпоинты, доступные без сессии (OAuth-флоу)
PUBLIC_API_PATHS = {
    "/api/auth/google", "/api/auth/google/callback",
    "/api/auth/yandex", "/api/auth/yandex/callback",
}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Inject DB connection
        async with request.app.state.pool.acquire() as conn:
            request.state.db = conn

            path = request.url.path
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
