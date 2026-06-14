from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import asyncpg
import os
from dotenv import load_dotenv

from middleware import AuthMiddleware
from routers import accounts, transactions, categories, loans, auth, metrics

load_dotenv()

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

@app.get("/health")
async def health():
    return {"status": "ok"}

# ── Static files — монтируем ПОСЛЕДНИМИ на / ──────────────────────────────────
# html=True → index.html для /, все остальные файлы по имени (/chart.js, /logo.svg…)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
