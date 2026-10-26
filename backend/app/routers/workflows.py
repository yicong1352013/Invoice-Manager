"""Workflow configuration, sealed upload batches and their delivery ledger."""

from copy import deepcopy
from datetime import datetime
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import can_access_invoice, get_current_user, require_admin
from app.database import get_db
from app.models.attachment import Attachment
from app.models.invoice import Invoice, InvoiceStatus, OcrResult
from app.models.user import User, UserRole
from app.models.workflow import Delivery, ExportArtifact, ExportTemplate, NotificationChannel, WorkflowDefinition, WorkflowRun, WorkflowRunItem
from app.schemas.workflow import DeliveryResolution, ManualApproval, WorkflowRunCreate, WorkflowSave
from app.services.audit_service import log_audit_no_commit
from app.services.provider_service import get_active_provider_row

router = APIRouter()


def _record(row, exclude=()):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns if column.name not in exclude}


async def get_accessible_run(db, run_id, user, lock=False):
    query = select(WorkflowRun).where(WorkflowRun.id == run_id)
    if lock:
        query = query.with_for_update()
    run = (await db.execute(query)).scalar_one_or_none()
    if run is None or (user.role != UserRole.ADMIN and run.owner != user.username):
        raise HTTPException(404, "批次不存在")
    return run


async def require_draft_run(db, run_id, user):
    run = await get_accessible_run(db, run_id, user, lock=True)
    if run.state != "draft":
        raise HTTPException(409, "批次已提交，不能修改上传清单")
    return run


async def _items(db, run, user):
    items = list((await db.execute(select(WorkflowRunItem).where(WorkflowRunItem.run_id == run.id).order_by(WorkflowRunItem.id))).scalars())
    ids = [item.invoice_id for item in items]
    invoices = {invoice.id: invoice for invoice in (await db.execute(select(Invoice).where(Invoice.id.in_(ids)))).scalars()}
    if any(item.invoice_id not in invoices or not can_access_invoice(user, invoices[item.invoice_id].owner) for item in items):
        raise HTTPException(403, "批次中的发票已不存在或无权访问")
    return items, invoices


async def _check_processing(db, run, user):
    items, invoices = await _items(db, run, user)
    if not run.frozen_at and any(invoice.status == InvoiceStatus.PROCESSING for invoice in invoices.values()):
        raise HTTPException(409, "发票正在重新解析，请等待完成后再恢复工作流")
    return items, invoices


async def _require_ai(db):
    provider = await get_active_provider_row(db)
    if provider is None or not provider.enabled or not provider.api_key or not provider.model:
        raise HTTPException(422, "工作流需要已启用并配置凭据和模型的 AI 服务")


async def _configuration(db, config, require_ready, require_processing=True):
    from app.schemas.notification import validate_target
    from app.services.notification_service import DEFAULT_MESSAGES, MESSAGE_VARIABLES, render_message

    config = deepcopy(config)
    config["messages"] = {**DEFAULT_MESSAGES, **config.get("messages", {})}
    try:
        for template in config["messages"].values():
            render_message(template, {key: "示例" for key in MESSAGE_VARIABLES})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    template = None
    if config.get("template_id") and require_processing:
        template = await db.get(ExportTemplate, config["template_id"])
        if template is None:
            raise HTTPException(422, "Excel 模板不存在")
    if require_ready and require_processing and "excel" in config["formats"] and (template is None or not template.enabled or not template.published_config):
        raise HTTPException(422, "请先选择已启用且已发布的 Excel 模板")
    if require_ready and not config["deliveries"]:
        raise HTTPException(422, "请配置至少一个通知渠道和收件目标")
    for destination in config["deliveries"]:
        channel = await db.get(NotificationChannel, destination["channel_id"])
        if channel is None or (require_ready and not channel.enabled):
            raise HTTPException(422, "通知渠道不存在或未启用")
        if destination.get("cc") and channel.kind != "email":
            raise HTTPException(422, "只有电子邮件渠道支持抄送")
        try:
            destination["targets"] = [validate_target(channel.kind, target) for target in destination["targets"]]
            destination["cc"] = [validate_target(channel.kind, target) for target in destination.get("cc", [])]
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        targets = destination["targets"] + destination["cc"]
        if len(set(targets)) != len(targets):
            raise HTTPException(422, "收件目标不能重复")
    if require_ready and require_processing:
        await _require_ai(db)
    return config, template


async def _audit(db, user, entity_id, action, detail=None, entity_type="workflow_run"):
    await log_audit_no_commit(db, entity_type, entity_id, action, user_id=user.username, new_value=detail)


@router.get("/workflows/options")
async def workflow_options(user: User = Depends(get_current_user)):
    from app.services.notification_service import DEFAULT_MESSAGES, MESSAGE_VARIABLES
    return {"default_messages": DEFAULT_MESSAGES, "message_variables": MESSAGE_VARIABLES}


@router.get("/workflows")
async def list_workflows(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    query = select(WorkflowDefinition).order_by(WorkflowDefinition.id.desc())
    if user.role != UserRole.ADMIN:
        query = query.where(WorkflowDefinition.enabled.is_(True))
    return [_record(row) for row in (await db.execute(query)).scalars()]


@router.post("/workflows")
async def create_workflow(body: WorkflowSave, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    config, _ = await _configuration(db, body.config.model_dump(), body.enabled)
    row = WorkflowDefinition(name=body.name, enabled=body.enabled, config=config, version=1)
    db.add(row)
    await db.flush()
    await _audit(db, user, row.id, "create", {"name": row.name, "enabled": row.enabled}, "workflow")
    await db.commit()
    await db.refresh(row)
    return _record(row)


@router.put("/workflows/{workflow_id}")
async def update_workflow(workflow_id: int, body: WorkflowSave, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(WorkflowDefinition).where(WorkflowDefinition.id == workflow_id).with_for_update())).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "工作流不存在")
    config, _ = await _configuration(db, body.config.model_dump(), body.enabled)
    row.name, row.enabled, row.config = body.name, body.enabled, config
    row.version += 1
    await _audit(db, user, row.id, "update", {"name": row.name, "enabled": row.enabled, "version": row.version}, "workflow")
    await db.commit()
    await db.refresh(row)
    return _record(row)


@router.post("/workflow-runs")
async def create_run(body: WorkflowRunCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    workflow = await db.get(WorkflowDefinition, body.workflow_id)
    if workflow is None or not workflow.enabled:
        raise HTTPException(422, "工作流不存在或未启用")
    config, template = await _configuration(db, workflow.config, True)
    config.update(workflow_version=workflow.version, workflow_name=workflow.name,
                  template=deepcopy(template.published_config) if template else None,
                  template_version=template.version if template else None)
    selected, recognized = [], set()
    uploaded_attachments = {}
    if body.invoice_ids is not None:
        query = select(Invoice.id, Invoice.owner, Invoice.status, Invoice.file_name).where(
            Invoice.id.in_(body.invoice_ids),
        ).order_by(Invoice.id).with_for_update(of=Invoice)
        if user.role != UserRole.ADMIN:
            query = query.where(Invoice.owner == user.username)
        available = {invoice.id: invoice for invoice in (await db.execute(query)).all()}
        if len(available) != len(body.invoice_ids):
            raise HTTPException(404, "所选发票不存在或无权访问，请刷新列表")
        selected = [available[identifier] for identifier in body.invoice_ids]
        if any(invoice.status in {InvoiceStatus.UPLOADED, InvoiceStatus.PROCESSING} for invoice in selected):
            raise HTTPException(409, "所选发票尚未完成识别，请等待识别结束后再执行工作流")
        active = (await db.execute(select(WorkflowRunItem.id).join(WorkflowRun).where(
            WorkflowRunItem.invoice_id.in_(body.invoice_ids),
            WorkflowRun.state.notin_(["completed", "cancelled"]),
        ).limit(1))).scalar_one_or_none()
        if active is not None:
            raise HTTPException(409, "所选发票已关联未结束的工作流批次，请先处理或取消原批次")
        attachments = (await db.execute(select(Attachment.id, Attachment.invoice_id, Attachment.file_name).where(
            Attachment.invoice_id.in_(body.invoice_ids),
        ).order_by(Attachment.id).with_for_update(of=Attachment))).all()
        if len(selected) + len(attachments) > 100:
            raise HTTPException(409, "每批最多关联 100 个发票及附件文件，请减少所选发票数量")
        recognized = set((await db.execute(select(OcrResult.invoice_id).where(
            OcrResult.invoice_id.in_(body.invoice_ids),
        ))).scalars())
        manifest = {
            "invoices": [{"id": f"invoice-{invoice.id}", "name": invoice.file_name} for invoice in selected],
            "attachments": [{"id": f"attachment-{attachment.id}", "name": attachment.file_name} for attachment in attachments],
        }
        uploaded_attachments = {f"attachment-{attachment.id}": attachment.id for attachment in attachments}
        config["attachment_bindings"] = {str(attachment.id): attachment.invoice_id for attachment in attachments}
    else:
        manifest = body.upload_manifest.model_dump()
    run = WorkflowRun(workflow_id=workflow.id, name=body.name, owner=user.username,
                      state="queued" if selected else "draft", config_snapshot=config, upload_manifest=manifest,
                      uploaded_attachments=uploaded_attachments, upload_errors={})
    db.add(run)
    await db.flush()
    for invoice in selected:
        state = "recognized" if invoice.id in recognized and invoice.status != InvoiceStatus.FAILED else "pending"
        db.add(WorkflowRunItem(run_id=run.id, invoice_id=invoice.id,
                               client_file_id=f"invoice-{invoice.id}", state=state))
    await _audit(db, user, run.id, "create", {"workflow_id": workflow.id, "name": run.name, "invoice_ids": body.invoice_ids})
    if selected:
        await _audit(db, user, run.id, "start", {"source": "invoice_selection", "invoice_ids": body.invoice_ids})
    await db.commit()
    await db.refresh(run)
    return _record(run, ("lease_owner", "lease_until"))


@router.get("/workflow-runs")
async def list_runs(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    query = select(WorkflowRun).order_by(WorkflowRun.id.desc()).limit(100)
    if user.role != UserRole.ADMIN:
        query = query.where(WorkflowRun.owner == user.username)
    return [_record(run, ("lease_owner", "lease_until")) for run in (await db.execute(query)).scalars()]


@router.get("/workflow-runs/{run_id}")
async def get_run(run_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.workflow_service import source_fingerprint, snapshot_invoice
    run = await get_accessible_run(db, run_id, user)
    items, invoices = await _items(db, run, user)
    attachments = []
    if run.config_snapshot.get("include_attachments"):
        attachments = list((await db.execute(select(Attachment).where(Attachment.id.in_(list((run.uploaded_attachments or {}).values()))))).scalars())
    result = _record(run, ("lease_owner", "lease_until"))
    result["items"] = [{**_record(item), "invoice": snapshot_invoice(invoices[item.invoice_id]),
                        "current_source_hash": source_fingerprint(invoices[item.invoice_id], [a for a in attachments if a.invoice_id == item.invoice_id])} for item in items]
    artifact_columns = [column for column in ExportArtifact.__table__.columns if column.name != "file_data"]
    result["artifacts"] = [dict(row) for row in (await db.execute(select(*artifact_columns).where(ExportArtifact.run_id == run.id).order_by(ExportArtifact.id))).mappings()]
    result["deliveries"] = [_record(row) for row in (await db.execute(select(Delivery).where(Delivery.run_id == run.id).order_by(Delivery.id))).scalars()]
    return result


async def _check_uploads(db, run, user):
    items, invoices = await _items(db, run, user)
    expected = {entry["id"] for entry in run.upload_manifest.get("invoices", [])}
    uploaded = {item.client_file_id for item in items}
    expected_attachments = {entry["id"] for entry in run.upload_manifest.get("attachments", [])}
    attachment_map = run.uploaded_attachments or {}
    if not expected or uploaded != expected or len(items) != len(expected) or set(attachment_map) != expected_attachments or run.upload_errors:
        raise HTTPException(409, "上传清单尚未全部成功，请处理失败或缺失文件后提交")
    attachment_ids = list(attachment_map.values())
    attachments = list((await db.execute(select(Attachment).where(Attachment.id.in_(attachment_ids)))).scalars())
    if len(attachments) != len(attachment_ids) or any(attachment.invoice_id not in invoices for attachment in attachments):
        raise HTTPException(409, "附件不存在、重复或未关联本批次发票")
    return {str(attachment.id): attachment.invoice_id for attachment in attachments}


async def _ready_run(db, run):
    if run.state == "draft":
        workflow = await db.get(WorkflowDefinition, run.workflow_id)
        if workflow is None or not workflow.enabled:
            raise HTTPException(409, "工作流已停用")
        await _configuration(db, run.config_snapshot, True)
    else:
        # Sealed runs retain the published configuration; cancel stops an existing run.
        await _configuration(db, run.config_snapshot, True, require_processing=False)
        if not run.frozen_at:
            await _require_ai(db)


@router.post("/workflow-runs/{run_id}/start")
async def start_run(run_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    run = await require_draft_run(db, run_id, user)
    await _ready_run(db, run)
    attachment_bindings = await _check_uploads(db, run, user)
    run.config_snapshot = {**run.config_snapshot, "attachment_bindings": attachment_bindings}
    run.state, run.error = "queued", None
    await _audit(db, user, run.id, "start")
    await db.commit()
    return {"id": run.id, "state": run.state}


@router.post("/workflow-runs/{run_id}/resume")
async def resume_run(run_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.workflow_service import retry_run
    run = await get_accessible_run(db, run_id, user, lock=True)
    if run.state != "needs_review" or run.frozen_at:
        raise HTTPException(409, "当前批次不允许恢复审核")
    await _ready_run(db, run)
    await _check_processing(db, run, user)
    await retry_run(db, run)
    await _audit(db, user, run.id, "resume")
    await db.commit()
    return {"id": run.id, "state": run.state}


@router.post("/workflow-runs/{run_id}/retry")
async def retry_run_endpoint(run_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.workflow_service import retry_run
    run = await get_accessible_run(db, run_id, user, lock=True)
    if run.state not in {"failed", "delivery_failed"}:
        raise HTTPException(409, "当前批次不允许重试")
    await _ready_run(db, run)
    await _check_processing(db, run, user)
    try:
        await retry_run(db, run)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await _audit(db, user, run.id, "retry")
    await db.commit()
    return {"id": run.id, "state": run.state}


@router.post("/workflow-runs/{run_id}/cancel")
async def cancel_run(run_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    run = await get_accessible_run(db, run_id, user, lock=True)
    if run.state in {"completed", "cancelled"}:
        raise HTTPException(409, "批次已经结束")
    run.state = "cancelled"
    run.next_attempt_at = None
    await _audit(db, user, run.id, "cancel")
    await db.commit()
    return {"id": run.id, "state": run.state}


@router.post("/workflow-runs/{run_id}/items/{item_id}/approve")
async def approve_run_item(run_id: int, item_id: int, body: ManualApproval, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.workflow_service import approve_item
    run = await get_accessible_run(db, run_id, user, lock=True)
    item = (await db.execute(select(WorkflowRunItem).where(WorkflowRunItem.id == item_id, WorkflowRunItem.run_id == run.id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "批次发票不存在")
    try:
        await approve_item(db, item, user, body.reason, expected_source_hash=body.source_hash)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await db.commit()
    return {"id": item.id, "state": item.state}


@router.post("/workflow-runs/{run_id}/deliveries/{delivery_id}/resolve")
async def resolve_delivery(run_id: int, delivery_id: int, body: DeliveryResolution, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    run = await get_accessible_run(db, run_id, user, lock=True)
    if run.state not in {"sending", "delivery_failed"}:
        raise HTTPException(409, "当前批次不允许确认投递结果")
    await _items(db, run, user)
    delivery = (await db.execute(select(Delivery).where(Delivery.id == delivery_id, Delivery.run_id == run.id).with_for_update())).scalar_one_or_none()
    if delivery is None:
        raise HTTPException(404, "投递记录不存在")
    if delivery.state != "unknown":
        raise HTTPException(409, "只有结果待确认的投递允许人工处理")
    delivery.attempts = [*(delivery.attempts or []), {"action": body.action, "reason": body.reason, "user": user.username, "at": datetime.utcnow().isoformat()}]
    delivery.state = "sent" if body.action == "confirm_sent" else "pending"
    if body.action == "retry":
        delivery.receipt = {**(delivery.receipt or {}), "retry_offset": len(delivery.attempts)}
    delivery.error = None
    await db.flush()
    unknown = (await db.execute(select(Delivery.id).where(Delivery.run_id == run.id, Delivery.state == "unknown").limit(1))).scalar_one_or_none()
    if unknown is None:
        run.state, run.error, run.next_attempt_at = "sending", None, None
    await _audit(db, user, run.id, "resolve_delivery", {"delivery_id": delivery.id, "action": body.action, "reason": body.reason})
    await db.commit()
    return {"id": delivery.id, "state": delivery.state, "run_state": run.state}


@router.get("/workflow-artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    run_id = (await db.execute(select(ExportArtifact.run_id).where(ExportArtifact.id == artifact_id))).scalar_one_or_none()
    if run_id is None:
        raise HTTPException(404, "导出文件不存在")
    run = await get_accessible_run(db, run_id, user)
    await _items(db, run, user)
    artifact = await db.get(ExportArtifact, artifact_id)
    return StreamingResponse(BytesIO(artifact.file_data), media_type=artifact.content_type,
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(artifact.file_name, safe='')}",
                                      "Content-Length": str(artifact.size), "Cache-Control": "no-store"})
