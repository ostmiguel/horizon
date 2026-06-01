from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

PUBLIC_PATHS = {"/health", "/api/auth/google", "/api/auth/google/callback",
                "/api/auth/yandex", "/api/auth/yandex/callback", "/docs", "/openapi.json"}

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
                raise HTTPException(401, "Not authenticated")

            row = await conn.fetchrow("""
                SELECT u.* FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = $1 AND s.expires_at > NOW()
            """, token)

            if not row:
                raise HTTPException(401, "Session expired")

            request.state.user = dict(row)
            request.state.user_id = row["id"]

        return await call_next(request)
