"""Authentication and authorization: password hashing, JWT tokens, FastAPI dependencies."""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)

# auto_error=False so a missing header flows into our 401 handling; the
# `token` query param covers iframe/img src which cannot send headers
_bearer_scheme = HTTPBearer(auto_error=False)

# Ephemeral fallback so deployments without SECRET_KEY stay usable;
# tokens then reset on every restart (a warning is logged once)
_ephemeral_secret: Optional[str] = None


def _get_secret_key() -> str:
    global _ephemeral_secret
    settings = get_settings()
    if settings.secret_key:
        return settings.secret_key
    if _ephemeral_secret is None:
        _ephemeral_secret = secrets.token_hex(32)
        logger.warning(
            "SECRET_KEY 未配置，使用临时密钥（重启后所有登录会话失效）。"
            "请在 backend/.env 中设置固定的 SECRET_KEY。"
        )
    return _ephemeral_secret


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(username: str, role: str) -> str:
    """Create a signed JWT for the given user."""
    settings = get_settings()
    payload = {
        "sub": username,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _get_secret_key(), algorithm="HS256")


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT. Raises jwt.PyJWTError on failure."""
    return jwt.decode(token, _get_secret_key(), algorithms=["HS256"])


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    token: Optional[str] = Query(None, description="访问令牌（供 iframe/img 等无法携带请求头的场景）"),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the current user from the Authorization header or ?token= query param."""
    raw_token = credentials.credentials if credentials else token
    if not raw_token:
        raise HTTPException(status_code=401, detail="未登录", headers={"WWW-Authenticate": "Bearer"})

    try:
        payload = decode_access_token(raw_token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="登录已过期或令牌无效，请重新登录")

    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="令牌无效，请重新登录")

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not user.enabled:
        raise HTTPException(status_code=401, detail="账号不存在或已被停用")

    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Require the current user to be an admin."""
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def can_access_invoice(user: User, invoice_owner: Optional[str]) -> bool:
    """Check whether a user may see/modify an invoice owned by `invoice_owner`."""
    if user.role == UserRole.ADMIN:
        return True
    return invoice_owner == user.username


def would_remove_last_admin(
    target_role: UserRole,
    target_enabled: bool,
    action_removes_admin: bool,
    other_enabled_admins: int,
) -> bool:
    """Pure guard: would acting on this user leave the system without an enabled admin?

    Args:
        target_role: role of the user being modified/deleted
        target_enabled: whether the target user is currently enabled
        action_removes_admin: whether the action demotes/disables/deletes admin power
            (demote, disable, or delete of an enabled admin)
        other_enabled_admins: count of OTHER enabled admins after the action would run
    """
    if target_role != UserRole.ADMIN or not target_enabled:
        return False
    if not action_removes_admin:
        return False
    return other_enabled_admins == 0
