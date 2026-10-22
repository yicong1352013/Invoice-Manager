from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.routers import (
    attachments as attachments_router,
    auth as auth_router,
    health,
    invoices,
    settings as settings_router,
    users as users_router,
)
from app.rate_limit import limiter

settings = get_settings()


async def _seed_admin() -> None:
    """Create the initial admin account from env vars (idempotent)."""
    from sqlalchemy import select

    from app.auth import hash_password
    from app.database import async_session_maker
    from app.models.user import User, UserRole

    if not settings.admin_username or not settings.admin_password:
        return

    async with async_session_maker() as db:
        existing = await db.execute(
            select(User).where(User.username == settings.admin_username)
        )
        if existing.scalar_one_or_none():
            return
        db.add(User(
            username=settings.admin_username,
            password_hash=hash_password(settings.admin_password),
            role=UserRole.ADMIN,
            enabled=True,
        ))
        await db.commit()


async def _startup_init() -> None:
    """Create tables (new ones only) and seed initial data."""
    from app.database import Base, async_session_maker, engine
    from app.models.attachment import Attachment  # noqa: F401
    from app.models.provider import AppSetting, LlmProvider  # noqa: F401
    from app.models.user import User  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _seed_admin()
    # Seed preset LLM providers + one-time legacy .env migration
    from app.services.provider_service import ensure_provider_seeded
    async with async_session_maker() as db:
        await ensure_provider_seeded(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _startup_init()
    # The mounted MCP sub-app owns session managers that must start with the
    # process; FastAPI does not run mounted apps' lifespans automatically.
    mcp_asgi = app.state.mcp_asgi_app
    async with getattr(mcp_asgi.mcp_asgi_app, "lifespan", _noop_lifespan())(app):
        yield


@asynccontextmanager
async def _noop_lifespan():
    yield


app = FastAPI(
    title="发票管理系统",
    description="Invoice Manager API - 发票上传、解析、管理系统",
    version="1.0.0",
    lifespan=lifespan,
)

# Add rate limiter to app state
app.state.limiter = limiter


# Custom rate limit exceeded handler with Chinese message
@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": f"请求过于频繁，请稍后再试。限制: {exc.detail}"}
    )

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:15173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health.router, prefix="/api", tags=["Health"])
app.include_router(auth_router.router, prefix="/api/auth", tags=["Auth"])
app.include_router(users_router.router, prefix="/api/users", tags=["Users"])
app.include_router(invoices.router, prefix="/api/invoices", tags=["Invoices"])
app.include_router(attachments_router.router, prefix="/api/attachments", tags=["Attachments"])
app.include_router(settings_router.router, prefix="/api/settings", tags=["Settings"])

# MCP endpoint (streamable HTTP, gated on mcp_enabled + token in settings).
# Mounted at /api with internal path /mcp => agents connect to /api/mcp.
from app.mcp_server import create_mcp_asgi_app  # noqa: E402
_mcp_asgi = create_mcp_asgi_app()
app.state.mcp_asgi_app = _mcp_asgi
app.mount("/api", _mcp_asgi)


@app.get("/")
async def root():
    return {"message": "发票管理系统 API", "version": "1.0.0"}
