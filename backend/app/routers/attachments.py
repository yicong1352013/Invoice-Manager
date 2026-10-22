"""Standalone attachment endpoints (re-association and edits)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import can_access_invoice, get_current_user
from app.database import get_db
from app.models.attachment import Attachment
from app.models.invoice import Invoice
from app.models.user import User
from app.schemas.attachment import AttachmentOut, AttachmentUpdateRequest

router = APIRouter()


@router.patch("/{attachment_id}", response_model=AttachmentOut)
async def update_attachment(
    attachment_id: int,
    body: AttachmentUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """修改附件（改关联发票 / 分类 / 备注）。改关联需要对两张发票都有权限。"""
    attachment = await db.get(Attachment, attachment_id)
    if not attachment:
        raise HTTPException(status_code=404, detail="附件不存在")

    # Current invoice access
    current_invoice = await db.get(Invoice, attachment.invoice_id)
    if not current_invoice or not can_access_invoice(current_user, current_invoice.owner):
        raise HTTPException(status_code=404, detail="附件不存在")

    if body.invoice_id is not None and body.invoice_id != attachment.invoice_id:
        target = await db.get(Invoice, body.invoice_id)
        if not target or not can_access_invoice(current_user, target.owner):
            raise HTTPException(status_code=404, detail="目标发票不存在")
        attachment.invoice_id = body.invoice_id

    if body.category is not None:
        attachment.category = body.category
    if body.note is not None:
        attachment.note = body.note

    await db.commit()
    await db.refresh(attachment)
    out = AttachmentOut.model_validate(attachment)
    out.file_size = len(attachment.file_data) if attachment.file_data else 0
    return out
