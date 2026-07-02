from fastapi import APIRouter, Request, HTTPException, Response
from pydantic import BaseModel
import httpx
import os
import secrets
import asyncpg
from urllib.parse import urlencode

from seed_user import seed_user_categories

router = APIRouter(prefix="/api/auth", tags=["auth"])

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
YANDEX_CLIENT_ID     = os.getenv("YANDEX_CLIENT_ID")
YANDEX_CLIENT_SECRET = os.getenv("YANDEX_CLIENT_SECRET")
MAILRU_CLIENT_ID     = os.getenv("MAILRU_CLIENT_ID")
MAILRU_CLIENT_SECRET = os.getenv("MAILRU_CLIENT_SECRET")
BASE_URL             = os.getenv("BASE_URL", "http://localhost:8000")

# Telegram-уведомления о новых регистрациях (best-effort, не блокируют вход)
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID")


async def notify_new_user(name: str, email: str, provider: str, total: int):
    """Шлёт в Telegram уведомление о новой регистрации. Best-effort: любые
    ошибки (нет токена, сеть, Telegram лежит) молча глотаем — регистрация важнее."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    text = (
        "🎉 Новый пользователь Horizon!\n\n"
        f"👤 {name or '—'}\n"
        f"✉️ {email or '—'}\n"
        f"🔑 {provider}\n\n"
        f"📊 Всего пользователей: {total}"
    )
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            )
    except Exception:
        pass

# ── Yandex OAuth ─────────────────────────────────────────────
@router.get("/yandex")
async def auth_yandex():
    state = secrets.token_urlsafe(32)
    params = urlencode({
        "client_id": YANDEX_CLIENT_ID,
        "redirect_uri": f"{BASE_URL}/api/auth/yandex/callback",
        "response_type": "code",
        "state": state,
        # Всегда показываем экран подтверждения с аккаунтом (иначе Яндекс молча
        # логинит активной сессией). На нём есть «Войти под другим аккаунтом» —
        # так пользователь с несколькими почтами Яндекса выбирает нужную.
        "force_confirm": "yes",
    })
    url = f"https://oauth.yandex.ru/authorize?{params}"
    from fastapi.responses import RedirectResponse
    r = RedirectResponse(url)
    r.set_cookie("oauth_state", state, httponly=True, samesite="lax", max_age=600, secure=True)
    return r

@router.get("/yandex/callback")
async def auth_yandex_callback(code: str, state: str, request: Request, response: Response):
    stored_state = request.cookies.get("oauth_state")
    if not stored_state or stored_state != state:
        raise HTTPException(400, "Invalid OAuth state")
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

    from fastapi.responses import RedirectResponse
    r = RedirectResponse(f"{BASE_URL}/?logged_in=1")
    r.set_cookie("session", session_token, httponly=True, samesite="lax", max_age=30*24*3600, secure=True)
    r.delete_cookie("oauth_state")
    return r

# ── Mail.ru OAuth (классический oauth.mail.ru, без PKCE) ─────
@router.get("/mailru")
async def auth_mailru():
    state = secrets.token_urlsafe(32)
    params = urlencode({
        "client_id": MAILRU_CLIENT_ID,
        "redirect_uri": f"{BASE_URL}/api/auth/mailru/callback",
        "response_type": "code",
        "scope": "userinfo",
        "state": state,
    })
    url = f"https://oauth.mail.ru/login?{params}"
    from fastapi.responses import RedirectResponse
    r = RedirectResponse(url)
    r.set_cookie("oauth_state", state, httponly=True, samesite="lax", max_age=600, secure=True)
    return r

@router.get("/mailru/callback")
async def auth_mailru_callback(code: str, state: str, request: Request, response: Response):
    stored_state = request.cookies.get("oauth_state")
    if not stored_state or stored_state != state:
        raise HTTPException(400, "Invalid OAuth state")
    db = request.state.db
    redirect_uri = f"{BASE_URL}/api/auth/mailru/callback"
    try:
        async with httpx.AsyncClient() as client:
            token_res = await client.post("https://oauth.mail.ru/token", data={
                "code": code,
                "client_id": MAILRU_CLIENT_ID,
                "client_secret": MAILRU_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            })
            tokens = token_res.json()
            if "access_token" not in tokens:
                raise HTTPException(400, f"Mail.ru token error ({token_res.status_code}): {str(tokens)[:300]}")
            user_res = await client.get(
                "https://oauth.mail.ru/userinfo",
                params={"access_token": tokens["access_token"]},
            )
            user_info = user_res.json()
        if "id" not in user_info:
            raise HTTPException(400, f"Mail.ru userinfo: {str(user_info)[:300]}")
        email = user_info.get("email")
        if not email:
            raise HTTPException(400, f"Mail.ru: профиль без email. Поля: {list(user_info.keys())}")
        name = user_info.get("name") or " ".join(
            x for x in [user_info.get("first_name"), user_info.get("last_name")] if x
        ).strip() or None
        user = await _get_or_create_user(db, "mailru", str(user_info["id"]), email, name)
        session_token = await _create_session(db, user["id"])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Mail.ru callback: {type(e).__name__}: {str(e)[:300]}")

    from fastapi.responses import RedirectResponse
    r = RedirectResponse(f"{BASE_URL}/?logged_in=1")
    r.set_cookie("session", session_token, httponly=True, samesite="lax", max_age=30*24*3600, secure=True)
    r.delete_cookie("oauth_state")
    return r

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
    # Fast path: found by (provider, provider_id)
    row = await db.fetchrow(
        "SELECT * FROM users WHERE provider=$1 AND provider_id=$2",
        provider, provider_id
    )
    if row:
        await db.execute("UPDATE users SET last_login=NOW() WHERE id=$1", row["id"])
        return dict(row)
    # New login or provider switch: upsert by email.
    # Also updates provider/provider_id so next login hits the fast path.
    # (xmax=0) = строка ВСТАВЛЕНА (новая регистрация), а не обновлена (смена провайдера).
    row = await db.fetchrow("""
        INSERT INTO users (email, name, provider, provider_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (email) DO UPDATE
          SET name=EXCLUDED.name,
              provider=EXCLUDED.provider,
              provider_id=EXCLUDED.provider_id,
              last_login=NOW()
        RETURNING *, (xmax = 0) AS _is_new
    """, email, name, provider, provider_id)
    is_new = bool(row["_is_new"])
    user = {k: v for k, v in dict(row).items() if k != "_is_new"}
    # Посев дефолтных категорий новому пользователю (идемпотентно — только если их нет).
    await seed_user_categories(db, user["id"])
    if is_new:
        total = await db.fetchval("SELECT COUNT(*) FROM users")
        await notify_new_user(user.get("name"), user.get("email"), provider, total)
    return user

async def _create_session(db, user_id) -> str:
    row = await db.fetchrow(
        "INSERT INTO sessions (user_id) VALUES ($1) RETURNING token",
        user_id
    )
    return row["token"]
