"""Seal a declared upload manifest before allowing a workflow to execute."""

import json

from fastapi import HTTPException
from sqlalchemy import select

from app.auth import can_access_invoice
from app.models.invoice import Invoice
from app.models.user import UserRole
from app.models.workflow import WorkflowRun, WorkflowRunItem


def validate_upload_keys(manifest, kind, files, raw_keys):
    try:
        keys = json.loads(raw_keys or "null")
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "上传文件标识必须是 JSON 数组") from exc
    if (not isinstance(keys, list) or len(keys) != len(files)
            or any(not isinstance(key, str) for key in keys)
            or len(set(keys)) != len(keys)):
        raise HTTPException(400, "文件和上传标识必须逐项对应且不能重复")
    declared = {item["id"]: item["name"] for item in manifest.get(kind, [])}
    if any(key not in declared or declared[key] != file.filename for key, file in zip(keys, files)):
        raise HTTPException(409, "上传文件与批次清单不一致，请核对文件名称和标识")
    return keys


async def prepare_workflow_upload(db, run_id, user, files, raw_keys, kind, invoice_id=None):
    if run_id is None:
        return None, [None] * len(files)
    run = (await db.execute(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update())).scalar_one_or_none()
    if not run or not can_access_invoice(user, run.owner):
        raise HTTPException(404, "工作流运行不存在")
    if run.state != "draft":
        raise HTTPException(409, "批次已提交，不能继续上传")
    keys = validate_upload_keys(run.upload_manifest, kind, files, raw_keys)
    if invoice_id is not None:
        item = (await db.execute(select(WorkflowRunItem.id).where(
            WorkflowRunItem.run_id == run.id, WorkflowRunItem.invoice_id == invoice_id,
        ))).scalar_one_or_none()
        if item is None:
            raise HTTPException(409, "附件必须关联本批次中的发票")
    return run, keys


def record_upload_results(run, keys, results):
    if run is None:
        return
    errors = dict(run.upload_errors or {})
    for key, result in zip(keys, results):
        if result.status == "success":
            errors.pop(key, None)
        else:
            errors[key] = result.message
    run.upload_errors = errors


async def prevent_workflow_invoice_deletion(db, invoice_ids):
    linked = (await db.execute(select(WorkflowRunItem.invoice_id).where(
        WorkflowRunItem.invoice_id.in_(invoice_ids),
    ).limit(1))).scalar_one_or_none()
    if linked is not None:
        raise HTTPException(409, "发票已关联工作流记录，不能删除；可取消尚未完成的批次")


async def lock_for_manual_reprocess(db, invoice_ids, user):
    query = select(WorkflowRun).join(WorkflowRunItem).join(Invoice).where(
        WorkflowRunItem.invoice_id.in_(invoice_ids),
        WorkflowRun.state.notin_(["completed", "cancelled", "delivery_failed"]),
    )
    if user.role != UserRole.ADMIN:
        query = query.where(Invoice.owner == user.username)
    runs = (await db.execute(query.order_by(WorkflowRun.id).with_for_update(of=WorkflowRun))).scalars().all()
    if any(run.state not in {"needs_review", "failed"} for run in runs):
        raise HTTPException(409, "发票正在工作流中处理，请等待批次暂停后再重新解析")
