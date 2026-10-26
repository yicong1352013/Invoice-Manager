"""Template administration and permission-scoped spreadsheet previews."""

from copy import deepcopy
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models.invoice import Invoice
from app.models.user import User, UserRole
from app.models.workflow import ExportTemplate, WorkflowDefinition, WorkflowRun
from app.schemas.export_template import (
    ExcelTemplateConfig, ExportTemplateCreate, ExportTemplatePreview,
    ExportTemplatePublish, ExportTemplateUpdate, FIELD_CATALOG,
)
from app.services.excel_template_service import (
    build_excel_export, build_export_filename, default_config, preview_excel, sample_invoices,
)

router = APIRouter()


def _out(template, admin=True):
    return {"id": template.id, "name": template.name, "description": template.description,
            "draft_config": template.draft_config if admin else None,
            "published_config": template.published_config, "version": template.version,
            "enabled": template.enabled, "updated_at": template.updated_at}


@router.get("")
async def list_templates(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    query = select(ExportTemplate).order_by(ExportTemplate.id)
    admin = user.role == UserRole.ADMIN
    if not admin:
        query = query.where(ExportTemplate.enabled.is_(True), ExportTemplate.version > 0)
    templates = (await db.execute(query)).scalars().all()
    return [_out(template, admin) for template in templates]


@router.get("/fields")
async def template_fields(_user: User = Depends(get_current_user)):
    return {"fields": FIELD_CATALOG, "default_config": default_config()}


async def _preview_invoices(body, db, user):
    if not body.invoice_ids:
        return sample_invoices()
    query = select(Invoice).where(Invoice.id.in_(body.invoice_ids))
    if user.role != UserRole.ADMIN:
        query = query.where(Invoice.owner == user.username)
    invoices = (await db.execute(query)).scalars().all()
    if len(invoices) != len(body.invoice_ids):
        raise HTTPException(status_code=404, detail="部分发票不存在或无权访问")
    by_id = {invoice.id: invoice for invoice in invoices}
    return [by_id[identifier] for identifier in body.invoice_ids]


@router.post("/preview")
async def preview_template(body: ExportTemplatePreview, db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    invoices = await _preview_invoices(body, db, user)
    try:
        return preview_excel(invoices, body.config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/sample")
async def download_sample(body: ExportTemplatePreview, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    invoices = await _preview_invoices(body, db, user)
    try:
        output = build_excel_export(invoices, body.config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    filename = quote(build_export_filename(body.config, run_name="样例"))
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})


@router.post("", status_code=201)
async def create_template(body: ExportTemplateCreate, db: AsyncSession = Depends(get_db),
                          _admin: User = Depends(require_admin)):
    template = ExportTemplate(name=body.name, description=body.description,
                              draft_config=body.config.model_dump(), enabled=body.enabled, version=0)
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return _out(template)


async def _locked_template(db, template_id):
    template = await db.scalar(select(ExportTemplate).where(ExportTemplate.id == template_id).with_for_update())
    if not template:
        raise HTTPException(status_code=404, detail="模板不存在")
    return template


@router.put("/{template_id}")
async def update_template(template_id: int, body: ExportTemplateUpdate, db: AsyncSession = Depends(get_db),
                          _admin: User = Depends(require_admin)):
    template = await _locked_template(db, template_id)
    for name, value in body.model_dump(exclude_unset=True).items():
        if value is None:
            raise HTTPException(status_code=422, detail="模板属性不能设置为 null")
        setattr(template, "draft_config" if name == "config" else name, value)
    await db.commit()
    await db.refresh(template)
    return _out(template)


@router.post("/{template_id}/publish")
async def publish_template(template_id: int, body: ExportTemplatePublish, db: AsyncSession = Depends(get_db),
                           _admin: User = Depends(require_admin)):
    template = await _locked_template(db, template_id)
    if template.version != body.expected_version:
        raise HTTPException(status_code=409, detail="模板版本已变化，请刷新后再发布")
    template.published_config = deepcopy(ExcelTemplateConfig.model_validate(template.draft_config).model_dump())
    template.version += 1
    await db.commit()
    await db.refresh(template)
    return _out(template)


@router.delete("/{template_id}", status_code=204)
async def delete_template(template_id: int, db: AsyncSession = Depends(get_db),
                          _admin: User = Depends(require_admin)):
    template = await _locked_template(db, template_id)
    if template.version > 0:
        raise HTTPException(status_code=409, detail="已发布模板只能停用，不能删除")
    definitions = (await db.execute(select(WorkflowDefinition.config))).scalars().all()
    snapshots = (await db.execute(select(WorkflowRun.config_snapshot))).scalars().all()
    if any(config.get("template_id") == template_id for config in [*definitions, *snapshots]):
        raise HTTPException(status_code=409, detail="模板已被工作流引用，不能删除")
    await db.delete(template)
    await db.commit()
