from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse, Response
from contextlib import asynccontextmanager
import asyncpg
import os

from middleware import AuthMiddleware, SecurityHeadersMiddleware
from routers import accounts, transactions, categories, loans, auth, metrics, goals, plan_rules, category_budgets, subscription, payments

DB_URL = os.getenv("DATABASE_URL")

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10)
    yield
    await app.state.pool.close()

app = FastAPI(title="Horizon API", lifespan=lifespan)

# ── Middleware ────────────────────────────────────────────────
# Порядок Starlette: добавленный ПОСЛЕДНИМ — внешний. SecurityHeaders должен
# оборачивать AuthMiddleware, чтобы навешивать заголовки и на его ранние ответы.
app.add_middleware(AuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

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
app.include_router(subscription.router)
app.include_router(payments.router)

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

# ── SEO / discovery: robots, sitemap, security.txt ───────────────────────────
SITE = "https://horizonapp.ru"

@app.get("/robots.txt")
async def robots_txt():
    body = (
        "User-agent: *\n"
        "Disallow: /api/\n"
        f"Sitemap: {SITE}/sitemap.xml\n"
    )
    return PlainTextResponse(body)

@app.get("/sitemap.xml")
async def sitemap_xml():
    urls = ["/", "/privacy", "/terms", "/consent"]
    items = "".join(f"  <url><loc>{SITE}{u}</loc></url>\n" for u in urls)
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{items}"
        "</urlset>\n"
    )
    return Response(content=body, media_type="application/xml")

@app.get("/.well-known/security.txt")
async def security_txt():
    body = (
        "Contact: mailto:hello@horizonapp.ru\n"
        "Expires: 2027-08-24T00:00:00.000Z\n"
        "Preferred-Languages: ru, en\n"
        f"Canonical: {SITE}/.well-known/security.txt\n"
    )
    return PlainTextResponse(body)

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
