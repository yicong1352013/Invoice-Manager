"""System logs API: read-only audit trail view (admin only)."""

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: int
    action: str
    user_id: Optional[str] = None
    ip_address: Optional[str] = None
    details: Optional[str] = None
    new_value: Optional[dict] = None
    created_at: datetime


class AuditLogListResponse(BaseModel):
    items: List[AuditLogOut]
    total: int
    page: int
    page_size: int


@router.get("", response_model=AuditLogListResponse)
async def list_logs(
    entity_type: Optional[str] = Query(None, description="实体类型筛选"),
    action: Optional[str] = Query(None, description="动作筛选"),
    user_id: Optional[str] = Query(None, description="操作人筛选"),
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """查询系统操作日志（仅管理员，按时间倒序）。"""
    query = select(AuditLog)
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if action:
        query = query.where(AuditLog.action == action)
    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    if start_date:
        try:
            query = query.where(AuditLog.created_at >= datetime.strptime(start_date, "%Y-%m-%d"))
        except ValueError:
            raise HTTPException(status_code=400, detail="start_date 需要 YYYY-MM-DD 格式")
    if end_date:
        try:
            end_exclusive = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        except ValueError:
            raise HTTPException(status_code=400, detail="end_date 需要 YYYY-MM-DD 格式")
        query = query.where(AuditLog.created_at < end_exclusive)

    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    result = await db.execute(
        query.order_by(AuditLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [AuditLogOut.model_validate(row) for row in result.scalars().all()]
    return AuditLogListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/actions", response_model=List[str])
async def list_log_actions(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """日志筛选用的去重动作列表（仅管理员）。"""
    result = await db.execute(select(AuditLog.action).distinct().order_by(AuditLog.action))
    return list(result.scalars().all())
