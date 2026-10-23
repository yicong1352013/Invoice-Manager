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
    logs as logs_router,
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


# Idempotent column patches for tables created before these fields existed.
# create_all only creates NEW tables; ALTER ... IF NOT EXISTS upgrades old ones.
_SCHEMA_PATCHES = [
    "ALTER TABLE llm_providers ADD COLUMN IF NOT EXISTS api_mode VARCHAR(10) NOT NULL DEFAULT 'chat'",
    "ALTER TABLE llm_providers ADD COLUMN IF NOT EXISTS temperature DOUBLE PRECISION",
    "ALTER TABLE llm_providers ADD COLUMN IF NOT EXISTS max_output_tokens INTEGER",
    # PG enum for InvoiceStatus. The model maps SQLEnum(InvoiceStatus) by
    # python NAME (rows store "REVIEWING"; Chinese labels live at the JSON
    # layer), so the new value is added as its name 'FAILED'. A stray
    # '解析失败' label from an earlier wrong patch stays in the type — PG
    # cannot drop enum values — but nothing ever binds it.
    "ALTER TYPE invoicestatus ADD VALUE IF NOT EXISTS 'FAILED'",
]


async def _recover_stuck_processing() -> None:
    """Reset invoices stuck in 解析中 (server died mid-recognition).

    Without this, a backend restart during processing leaves invoices in
    解析中 forever. They become 解析失败 with an audit note so the reason is
    visible on the logs page and 重新解析 retries them.
    """
    from sqlalchemy import select

    from app.database import async_session_maker
    from app.models.audit_log import AuditLog
    from app.models.invoice import Invoice, InvoiceStatus

    async with async_session_maker() as db:
        result = await db.execute(select(Invoice).where(Invoice.status == InvoiceStatus.PROCESSING))
        stuck = result.scalars().all()
        for inv in stuck:
            inv.status = InvoiceStatus.FAILED
            db.add(AuditLog(
                entity_type="invoice",
                entity_id=inv.id,
                action="process_failed",
                new_value={"error": "服务重启导致识别中断", "interrupted": True},
                details="识别失败: 服务重启导致识别中断，请重新解析",
            ))
        if stuck:
            await db.commit()
            print(f"[startup] reset {len(stuck)} stuck 解析中 invoice(s) to 解析失败")


async def _startup_init() -> None:
    """Create tables (new ones only), patch existing tables, and seed data."""
    from sqlalchemy import text
    from app.database import Base, async_session_maker, engine
    from app.models.attachment import Attachment  # noqa: F401
    from app.models.provider import AppSetting, LlmProvider  # noqa: F401
    from app.models.user import User  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Each patch runs in its own transaction: a failing statement aborts its
    # PG transaction, and a shared one would roll back the earlier patches.
    for ddl in _SCHEMA_PATCHES:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(ddl))
        except Exception as e:  # noqa: BLE001 - keep startup alive on patch failure
            print(f"[startup] schema patch skipped ({ddl.split('ADD COLUMN ')[0]}...): {e}")
    await _seed_admin()
    # Seed preset LLM providers + one-time legacy .env migration
    from app.services.provider_service import ensure_provider_seeded
    async with async_session_maker() as db:
        await ensure_provider_seeded(db)
    await _recover_stuck_processing()


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
app.include_router(logs_router.router, prefix="/api/logs", tags=["Logs"])

# MCP endpoint (streamable HTTP, gated on mcp_enabled + token in settings).
# Mounted at /api with internal path /mcp => agents connect to /api/mcp.
from app.mcp_server import create_mcp_asgi_app  # noqa: E402
_mcp_asgi = create_mcp_asgi_app()
app.state.mcp_asgi_app = _mcp_asgi
app.mount("/api", _mcp_asgi)


@app.get("/")
async def root():
    return {"message": "发票管理系统 API", "version": "1.0.0"}
