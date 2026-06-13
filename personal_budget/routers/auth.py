from fastapi import APIRouter, Request, HTTPException, Response
from pydantic import BaseModel
import httpx
import os
import asyncpg

router = APIRouter(prefix="/api/auth", tags=["auth"])

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
YANDEX_CLIENT_ID     = os.getenv("YANDEX_CLIENT_ID")
YANDEX_CLIENT_SECRET = os.getenv("YANDEX_CLIENT_SECRET")
BASE_URL             = os.getenv("BASE_URL", "http://localhost:8000")

# ── Google OAuth ─────────────────────────────────────────────
@router.get("/google")
async def auth_google():
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={BASE_URL}/api/auth/google/callback"
        "&response_type=code"
        "&scope=openid email profile"
    )
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)

@router.get("/google/callback")
async def auth_google_callback(code: str, request: Request, response: Response):
    db = request.state.db
    async with httpx.AsyncClient() as client:
        # Exchange code for token
        token_res = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": f"{BASE_URL}/api/auth/google/callback",
            "grant_type": "authorization_code",
        })
        tokens = token_res.json()
        # Get user info
        user_res = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        user_info = user_res.json()

    user = await _get_or_create_user(db, "google", user_info["sub"],
                                      user_info.get("email"), user_info.get("name"))
    session_token = await _create_session(db, user["id"])

    response.set_cookie("session", session_token, httponly=True, samesite="lax", max_age=30*24*3600)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"{BASE_URL}/?logged_in=1")

# ── Yandex OAuth ─────────────────────────────────────────────
@router.get("/yandex")
async def auth_yandex():
    url = (
        "https://oauth.yandex.ru/authorize"
        f"?client_id={YANDEX_CLIENT_ID}"
        f"&redirect_uri={BASE_URL}/api/auth/yandex/callback"
        "&response_type=code"
    )
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)

@router.get("/yandex/callback")
async def auth_yandex_callback(code: str, request: Request, response: Response):
    db = request.state.db
    async with httpx.AsyncClient() as client:
        token_res = await client.post("https://oauth.yandex.ru/token", data={
            "code": code,
            "client_id": YANDEX_CLIENT_ID,
            "client_secret": YANDEX_CLIENT_SECRET,
            "grant_type": "authorization_code",
        })
        tokens = token_res.json()
        user_res = await client.get(
            "https://login.yandex.ru/info?format=json",
            headers={"Authorization": f"OAuth {tokens['access_token']}"}
        )
        user_info = user_res.json()

    user = await _get_or_create_user(db, "yandex", str(user_info["id"]),
                                      user_info.get("default_email"), user_info.get("real_name"))
    session_token = await _create_session(db, user["id"])

    response.set_cookie("session", session_token, httponly=True, samesite="lax", max_age=30*24*3600)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"{BASE_URL}/?logged_in=1")

# ── Me & Logout ──────────────────────────────────────────────
@router.get("/me")
async def get_me(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {"id": str(user["id"]), "email": user["email"], "name": user["name"]}

@router.post("/logout")
async def logout(request: Request, response: Response):
    db = request.state.db
    token = request.cookies.get("session")
    if token:
        await db.execute("DELETE FROM sessions WHERE token=$1", token)
    response.delete_cookie("session")
    return {"ok": True}

# ── Helpers ──────────────────────────────────────────────────
async def _get_or_create_user(db, provider: str, provider_id: str, email: str, name: str):
    row = await db.fetchrow(
        "SELECT * FROM users WHERE provider=$1 AND provider_id=$2",
        provider, provider_id
    )
    if row:
        await db.execute("UPDATE users SET last_login=NOW() WHERE id=$1", row["id"])
        return dict(row)
    row = await db.fetchrow("""
        INSERT INTO users (email, name, provider, provider_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (email) DO UPDATE SET name=EXCLUDED.name, last_login=NOW()
        RETURNING *
    """, email, name, provider, provider_id)
    return dict(row)

async def _create_session(db, user_id) -> str:
    row = await db.fetchrow(
        "INSERT INTO sessions (user_id) VALUES ($1) RETURNING token",
        user_id
    )
    return row["token"]
