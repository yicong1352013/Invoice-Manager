"""Authentication endpoints: login and current user info."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_access_token, get_current_user, verify_password
from app.database import get_db
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.user import LoginRequest, LoginResponse, UserInfo
from app.services.audit_service import log_audit, get_client_info

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/login", response_model=LoginResponse)
@limiter.limit("10/minute")
async def login(request: Request, body: LoginRequest, db: AsyncSession = Depends(get_db)):
    """用户名密码登录，返回 JWT 访问令牌。"""
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if not user.enabled:
        raise HTTPException(status_code=401, detail="账号已被停用，请联系管理员")

    token = create_access_token(user.username, user.role.value)
    client_info = get_client_info(request)
    await log_audit(
        db=db,
        entity_type="user",
        entity_id=user.id,
        action="login",
        user_id=user.username,
        ip_address=client_info.get("ip_address"),
        user_agent=client_info.get("user_agent"),
    )

    return LoginResponse(access_token=token, user=UserInfo.model_validate(user))


@router.get("/me", response_model=UserInfo)
async def get_me(current_user: User = Depends(get_current_user)):
    """获取当前登录用户信息。"""
    return UserInfo.model_validate(current_user)
