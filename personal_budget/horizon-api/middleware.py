from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

PUBLIC_PATHS = {"/health", "/api/auth/google", "/api/auth/google/callback",
                "/api/auth/yandex", "/api/auth/yandex/callback", "/docs", "/openapi.json"}


def _unauth(detail: str):
    # ВАЖНО: HTTPException, поднятый внутри BaseHTTPMiddleware, не перехватывается
    # обработчиками FastAPI и отдаётся как 500. Поэтому возвращаем ответ напрямую.
    return JSONResponse({"detail": detail}, status_code=401)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Inject DB connection
        async with request.app.state.pool.acquire() as conn:
            request.state.db = conn

            # Skip auth for public paths
            if request.url.path in PUBLIC_PATHS:
                return await call_next(request)

            # Check session cookie
            token = request.cookies.get("session")
            if not token:
                return _unauth("Not authenticated")

            row = await conn.fetchrow("""
                SELECT u.* FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = $1 AND s.expires_at > NOW()
            """, token)

            if not row:
                return _unauth("Session expired")

            request.state.user = dict(row)
            request.state.user_id = row["id"]

            return await call_next(request)
