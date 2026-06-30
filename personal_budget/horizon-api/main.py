from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import asyncpg
import os

from middleware import AuthMiddleware
from routers import accounts, transactions, categories, loans, auth, metrics, goals, plan_rules, category_budgets

DB_URL = os.getenv("DATABASE_URL")

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10)
    yield
    await app.state.pool.close()

app = FastAPI(title="Horizon API", lifespan=lifespan)

# ── Middleware ────────────────────────────────────────────────
app.add_middleware(AuthMiddleware)

# ── Routers (регистрируем ДО статики, чтобы /api/* не перехватывалось) ───────
app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(categories.router)
app.include_router(loans.router)
app.include_router(metrics.router)
app.include_router(goals.router)
app.include_router(plan_rules.router)
app.include_router(category_budgets.router)

@app.get("/health")
async def health():
    return {"status": "ok"}

# ── Юр-страницы (публичные, без авторизации) — ДО монтирования статики ─────────
@app.get("/privacy")
async def privacy_page():
    return FileResponse("static/privacy.html")

@app.get("/terms")
async def terms_page():
    return FileResponse("static/terms.html")

@app.get("/consent")
async def consent_page():
    return FileResponse("static/consent.html")

# ── Лендинг (превью по прямой ссылке) ─────────────────────────────────────────
@app.get("/welcome")
async def welcome_page():
    return FileResponse("static/landing.html")

# ── Главная: развязка по сессии (ДО монтирования статики) ─────────────────────
# Залогинен → приложение (index.html). Разлогинен → лендинг. request.state.db
# проставляется middleware и для не-/api путей.
@app.get("/")
async def root(request: Request):
    token = request.cookies.get("session")
    if token:
        row = await request.state.db.fetchrow(
            "SELECT 1 FROM sessions WHERE token=$1 AND expires_at > NOW()", token
        )
        if row:
            return FileResponse("static/index.html")
    return FileResponse("static/landing.html")

# ── Static files — монтируем ПОСЛЕДНИМИ на / ──────────────────────────────────
# html=True → index.html для /, все остальные файлы по имени (/chart.js, /logo.svg…)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
