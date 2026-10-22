"""Admin-only user management endpoints."""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_password, require_admin, would_remove_last_admin
from app.database import get_db
from app.models.user import User, UserRole
from app.rate_limit import limiter
from app.schemas.user import UserCreateRequest, UserInfo, UserUpdateRequest
from app.services.audit_service import log_audit_no_commit, get_client_info

logger = logging.getLogger(__name__)
router = APIRouter()


async def _count_other_enabled_admins(db: AsyncSession, exclude_user_id: int) -> int:
    """Count enabled admins other than the given user."""
    query = select(func.count()).select_from(User).where(
        User.role == UserRole.ADMIN,
        User.enabled.is_(True),
        User.id != exclude_user_id,
    )
    return await db.scalar(query) or 0


@router.get("", response_model=List[UserInfo])
async def list_users(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """获取用户列表（仅管理员）。"""
    result = await db.execute(select(User).order_by(User.id))
    users = result.scalars().all()
    return [UserInfo.model_validate(u) for u in users]


@router.post("", response_model=UserInfo)
@limiter.limit("20/minute")
async def create_user(
    request: Request,
    body: UserCreateRequest,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """创建用户（仅管理员）。"""
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"用户名已存在: {body.username}")

    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        enabled=True,
    )
    db.add(user)
    await db.flush()

    client_info = get_client_info(request)
    await log_audit_no_commit(
        db=db,
        entity_type="user",
        entity_id=user.id,
        action="create",
        user_id=_admin.username,
        new_value={"username": user.username, "role": user.role.value, "enabled": True},
        ip_address=client_info.get("ip_address"),
        user_agent=client_info.get("user_agent"),
    )
    await db.commit()
    await db.refresh(user)
    return UserInfo.model_validate(user)


@router.patch("/{user_id}", response_model=UserInfo)
async def update_user(
    user_id: int,
    body: UserUpdateRequest,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """更新用户角色/启用状态/密码（仅管理员）。"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    old_values = {"role": user.role.value, "enabled": user.enabled}
    demotes_admin = body.role is not None and body.role != UserRole.ADMIN and user.role == UserRole.ADMIN
    disables_admin = body.enabled is False and user.role == UserRole.ADMIN and user.enabled

    if demotes_admin or disables_admin:
        others = await _count_other_enabled_admins(db, user.id)
        if would_remove_last_admin(user.role, user.enabled, True, others):
            raise HTTPException(status_code=400, detail="不能停用或降级最后一个启用的管理员")

    if body.role is not None:
        user.role = body.role
    if body.enabled is not None:
        user.enabled = body.enabled
    if body.password is not None:
        user.password_hash = hash_password(body.password)

    await log_audit_no_commit(
        db=db,
        entity_type="user",
        entity_id=user.id,
        action="update",
        user_id=_admin.username,
        old_value=old_values,
        new_value={
            "role": user.role.value,
            "enabled": user.enabled,
            "password_changed": body.password is not None,
        },
    )
    await db.commit()
    await db.refresh(user)
    return UserInfo.model_validate(user)


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除用户（仅管理员；不能删除自己，不能删除最后一个启用的管理员）。"""
    if user_id == _admin.id:
        raise HTTPException(status_code=400, detail="不能删除当前登录的账号")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    others = await _count_other_enabled_admins(db, user.id)
    if would_remove_last_admin(user.role, user.enabled, True, others):
        raise HTTPException(status_code=400, detail="不能删除最后一个启用的管理员")

    username = user.username
    await db.delete(user)
    await log_audit_no_commit(
        db=db,
        entity_type="user",
        entity_id=user.id,
        action="delete",
        user_id=_admin.username,
        old_value={"username": username, "role": user.role.value},
    )
    await db.commit()
    return {"message": f"已删除用户 {username}"}
