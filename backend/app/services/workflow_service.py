"""Checkpointed invoice workflows; a delivery ledger is the source of truth."""

import asyncio
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import PurePosixPath
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import select

from app.auth import can_access_invoice
from app.models.attachment import Attachment
from app.models.invoice import Invoice, InvoiceStatus
from app.models.user import User
from app.models.workflow import Delivery, ExportArtifact, NotificationChannel, WorkflowRun, WorkflowRunItem
from app.services.ai_verification_service import verify_invoice
from app.services.audit_service import log_audit_no_commit
from app.services.invoice_service import COMPARABLE_FIELDS, CRITICAL_FIELDS, process_invoice

ACTIVE_STATES = ("queued", "recognizing", "reviewing", "exporting", "sending")
NUMERIC_FIELDS = {"quantity", "unit_price", "amount", "tax_amount", "total_with_tax"}


class LeaseLost(RuntimeError):
    pass


def snapshot_invoice(invoice):
    result = {}
    for name in ["id", *COMPARABLE_FIELDS, "status", "owner", "file_name", "file_type", "created_at"]:
        value = getattr(invoice, name, None)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            scale = 4 if name in {"quantity", "unit_price"} else 2
            value = format(value.quantize(Decimal(10) ** -scale), "f")
        elif isinstance(value, InvoiceStatus):
            value = value.value
        result[name] = value
    return result


def source_fingerprint(invoice, attachments=None):
    data = snapshot_invoice(invoice)
    data["file_sha256"] = hashlib.sha256(invoice.file_data).hexdigest()
    data["attachments"] = [
        {"id": a.id, "invoice_id": a.invoice_id, "name": a.file_name,
         "sha256": hashlib.sha256(a.file_data).hexdigest(), "category": a.category, "note": a.note}
        for a in sorted(attachments or [], key=lambda a: a.id)
    ]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_invoice(data):
    errors = [f"缺少必填字段：{name}" for name in CRITICAL_FIELDS if data.get(name) is None or str(data[name]).strip() == ""]
    try:
        if data.get("issue_date"):
            date.fromisoformat(str(data["issue_date"]))
    except ValueError:
        errors.append("开票日期格式无效")
    numbers = {}
    for name in NUMERIC_FIELDS:
        if data.get(name) is None:
            continue
        try:
            value = Decimal(str(data[name]))
            scale = 4 if name in {"quantity", "unit_price"} else 2
            if not value.is_finite() or abs(value) >= Decimal(10) ** (12 - scale) or value != value.quantize(Decimal(10) ** -scale):
                raise ValueError()
            numbers[name] = value
        except (ValueError, InvalidOperation):
            errors.append(f"数值格式或范围无效：{name}")
    if all(k in numbers for k in ("amount", "tax_amount", "total_with_tax")):
        if abs(numbers["amount"] + numbers["tax_amount"] - numbers["total_with_tax"]) > Decimal("0.01"):
            errors.append("不含税金额与税额之和不等于价税合计")
    for name in COMPARABLE_FIELDS:
        column = Invoice.__table__.columns[name]
        limit = getattr(column.type, "length", None)
        value = data.get(name)
        if limit and value is not None and (not isinstance(value, str) or len(value) > limit):
            errors.append(f"文本格式或长度无效：{name}")
    return errors


async def _attachments(db, run, invoice_id):
    if not run.config_snapshot.get("include_attachments"):
        return []
    ids = list((run.uploaded_attachments or {}).values())
    result = list((await db.execute(select(Attachment).where(
        Attachment.invoice_id == invoice_id, Attachment.id.in_(ids)
    ).order_by(Attachment.id).with_for_update())).scalars())
    bindings = run.config_snapshot.get("attachment_bindings", {})
    expected = {int(key) for key, owner_id in bindings.items() if owner_id == invoice_id}
    if bindings and {attachment.id for attachment in result} != expected:
        raise ValueError("批次附件已删除或重新关联，请核对上传清单")
    return result


async def _validate_attachment_bindings(db, run):
    expected = run.config_snapshot.get("attachment_bindings", {})
    if not expected:
        return
    rows = list((await db.execute(select(Attachment).where(Attachment.id.in_([int(key) for key in expected])).with_for_update())).scalars())
    if {str(row.id): row.invoice_id for row in rows} != expected:
        raise ValueError("批次附件已删除或重新关联，请核对上传清单")


async def _owner(db, run):
    owner = (await db.execute(select(User).where(User.username == run.owner).execution_options(populate_existing=True))).scalar_one_or_none()
    if owner is None or not owner.enabled:
        raise ValueError("批次发起人已停用或不存在")
    return owner


async def _invoice(db, item, owner, lock=False):
    query = select(Invoice).where(Invoice.id == item.invoice_id).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    invoice = (await db.execute(query)).scalar_one_or_none()
    if invoice is None or not can_access_invoice(owner, invoice.owner):
        raise ValueError("发票不存在或批次发起人已无权访问")
    return invoice


async def approve_item(db, item, user, reason, expected_source_hash=None):
    if not reason or not reason.strip():
        raise ValueError("人工确认必须填写原因")
    run = (await db.execute(select(WorkflowRun).where(WorkflowRun.id == item.run_id).with_for_update())).scalar_one()
    if run.frozen_at or run.state != "needs_review":
        raise ValueError("当前批次不允许人工确认")
    invoice = await _invoice(db, item, user, lock=True)
    await _invoice(db, item, await _owner(db, run))
    data = snapshot_invoice(invoice)
    errors = validate_invoice(data)
    if errors:
        raise ValueError("；".join(errors))
    item.data_snapshot = data
    item.source_hash = source_fingerprint(invoice, await _attachments(db, run, invoice.id))
    if expected_source_hash and expected_source_hash != item.source_hash:
        raise ValueError("发票或附件已修改，请刷新后确认")
    item.approval = {"user": user.username, "reason": reason.strip(), "at": datetime.utcnow().isoformat(), "source_hash": item.source_hash}
    item.state, item.error = "approved", None
    await log_audit_no_commit(db, "workflow_run", run.id, "manual_approve", user_id=user.username,
                              new_value={"invoice_id": invoice.id, **item.approval})


def _apply_corrections(invoice, verdict):
    proposed = snapshot_invoice(invoice)
    changes = []
    for field in verdict["fields"]:
        name, value = field["field"], field.get("corrected_value")
        if (name not in COMPARABLE_FIELDS or field["status"] != "incorrect"
                or field.get("severity", "blocking") != "blocking"
                or value is None or str(value).strip() == ""):
            continue
        proposed[name] = str(value).strip()
        changes.append({"field": name, "old": snapshot_invoice(invoice)[name], "new": proposed[name], "reason": field.get("reason")})
    # Reject the whole suggested patch before changing any persistent value.
    if validate_invoice(proposed):
        return []
    for change in changes:
        name, value = change["field"], change["new"]
        if name in NUMERIC_FIELDS:
            value = Decimal(value)
        elif name == "issue_date":
            value = date.fromisoformat(value)
        setattr(invoice, name, value)
    return changes


async def _review(db, run, item, owner):
    invoice = await _invoice(db, item, owner)
    attachments = await _attachments(db, run, invoice.id)
    before = source_fingerprint(invoice, attachments)
    if item.state == "approved" and item.source_hash == before:
        return
    item.approval = None
    attempts = dict(item.attempts or {})
    if attempts.get("verify", 0) >= 3:
        item.state, item.error = "needs_review", "AI 审核已达到重试上限，请人工处理"
        await db.commit()
        return
    attempts["verify"] = attempts.get("verify", 0) + 1
    item.attempts = attempts
    await db.commit()
    outcome = await verify_invoice(db, invoice)
    if await _cancelled(db, run):
        await db.rollback()
        return
    invoice = await _invoice(db, item, owner, lock=True)
    if source_fingerprint(invoice, await _attachments(db, run, invoice.id)) != before:
        item.state, item.error = "needs_review", "审核期间发票或附件已修改，请重新审核"
        await db.commit()
        return
    item.verification = outcome["verdict"]
    if outcome["verdict"]["overall"] != "pass" and not item.corrections:
        changes = _apply_corrections(invoice, outcome["verdict"])
        if changes:
            item.corrections = changes
            await log_audit_no_commit(db, "workflow_run", run.id, "auto_correct", user_id=run.owner,
                                      new_value={"invoice_id": invoice.id, "changes": changes})
            await db.commit()
            before = source_fingerprint(invoice, await _attachments(db, run, invoice.id))
            outcome = await verify_invoice(db, invoice)
            if await _cancelled(db, run):
                await db.rollback()
                return
            invoice = await _invoice(db, item, owner, lock=True)
            if source_fingerprint(invoice, await _attachments(db, run, invoice.id)) != before:
                item.state, item.error = "needs_review", "修正复核期间数据已修改"
                await db.commit()
                return
            item.verification = outcome["verdict"]
    errors = validate_invoice(snapshot_invoice(invoice))
    passed = outcome["verdict"]["overall"] == "pass" and not errors
    item.state = "approved" if passed else "needs_review"
    item.error = "；".join(errors) if errors else None if passed else "AI 审核仍有关键字段不符或存疑"
    item.data_snapshot = snapshot_invoice(invoice)
    item.source_hash = source_fingerprint(invoice, await _attachments(db, run, invoice.id))
    await db.commit()


def _typed_snapshot(data):
    data = dict(data)
    for name in NUMERIC_FIELDS:
        if data.get(name) is not None:
            data[name] = Decimal(data[name])
    if data.get("issue_date"):
        data["issue_date"] = date.fromisoformat(data["issue_date"])
    if data.get("created_at"):
        data["created_at"] = datetime.fromisoformat(data["created_at"])
    if data.get("status"):
        data["status"] = InvoiceStatus(data["status"])
    return SimpleNamespace(**data)


def _artifact(run, kind, name, content_type, content):
    return ExportArtifact(run_id=run.id, kind=kind, file_name=name, content_type=content_type,
                          file_data=content, sha256=hashlib.sha256(content).hexdigest(), size=len(content))


async def _freeze(db, run, items, owner):
    if await _cancelled(db, run, lock=True):
        return False
    # Match mutation endpoints: lock invoices before their attachments.
    for item in sorted(items, key=lambda item: item.invoice_id):
        await _invoice(db, item, owner, lock=True)
    await _validate_attachment_bindings(db, run)
    archive = BytesIO()
    config = run.config_snapshot
    with ZipFile(archive, "w", ZIP_DEFLATED) as zipped:
        for item in items:
            invoice = await _invoice(db, item, owner, lock=True)
            attachments = await _attachments(db, run, invoice.id)
            if item.state != "approved" or item.source_hash != source_fingerprint(invoice, attachments):
                item.state, item.approval = "needs_review", None
                item.error = "审核后数据发生变化，需重新审核"
                run.state = "needs_review"
                await db.commit()
                return False
            if config.get("include_originals"):
                zipped.writestr(f"{invoice.id}/original/{PurePosixPath(invoice.file_name.replace(chr(92), '/')).name}", invoice.file_data)
            for attachment in attachments:
                name = PurePosixPath(attachment.file_name.replace(chr(92), '/')).name
                zipped.writestr(f"{invoice.id}/attachments/{attachment.id}_{name}", attachment.file_data)
    if config.get("include_originals") or config.get("include_attachments"):
        db.add(_artifact(run, "zip", f"invoice_{run.id}_sources.zip", "application/zip", archive.getvalue()))
    run.frozen_at, run.state = datetime.utcnow(), "exporting"
    await db.commit()
    return True


async def _export(db, run, items):
    from app.services.excel_template_service import build_excel_export, build_export_filename
    from app.services.export_service import build_pdf_export, build_word_export, compute_totals
    rows = [_typed_snapshot(item.data_snapshot) for item in items]
    existing = set((await db.execute(select(ExportArtifact.kind).where(ExportArtifact.run_id == run.id))).scalars())
    builders = {"word": (build_word_export, "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                "pdf": (build_pdf_export, "pdf", "application/pdf")}
    for kind in run.config_snapshot["formats"]:
        if await _cancelled(db, run):
            return
        if kind in existing:
            continue
        if kind == "excel":
            result = build_excel_export(rows, run.config_snapshot["template"])
            ext, mime = "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            builder, ext, mime = builders[kind]
            result = builder(rows, compute_totals(rows))
        content = result.getvalue() if hasattr(result, "getvalue") else result
        name = build_export_filename(run.config_snapshot["template"], run_id=run.id, run_name=run.name, extension=ext) if kind == "excel" else f"invoice_{run.id}.{ext}"
        db.add(_artifact(run, kind, name, mime, content))
        await db.commit()
    if not await _cancelled(db, run, lock=True):
        run.state = "sending"
        await db.commit()


async def _cancelled(db, run, lock=False):
    await db.refresh(run, with_for_update=lock)
    expected = getattr(run, "_worker_lease_owner", None)
    if expected and run.lease_owner != expected:
        raise LeaseLost("批次执行租约已转移")
    return run.state == "cancelled"


async def _send(db, run, items, event="success"):
    from app.config import get_settings
    from app.services.notification_service import DEFAULT_MESSAGES, DeliveryRejected, DeliveryRetryable, deliver, render_message
    artifacts = list((await db.execute(select(ExportArtifact).where(ExportArtifact.run_id == run.id).order_by(ExportArtifact.id))).scalars()) if event == "success" else []
    context = {"run_id": run.id, "run_name": run.name, "invoice_count": len(items),
               "total_with_tax": str(sum((Decimal(i.data_snapshot.get("total_with_tax") or "0") for i in items if i.data_snapshot), Decimal(0))),
               "artifact_names": "、".join(a.file_name for a in artifacts), "completed_at": datetime.utcnow().isoformat(),
               "run_url": f"{getattr(get_settings(), 'public_base_url', '').rstrip('/')}/workflows/runs/{run.id}",
               "error": run.error or "", "review_count": sum(i.state != "approved" for i in items), "failed_count": sum(i.state == "failed" for i in items)}
    messages = {**DEFAULT_MESSAGES, **run.config_snapshot.get("messages", {})}
    subject_pattern = messages["success_subject"] if event == "success" else {
        "review": "【发票待人工处理】{{run_name}}",
        "failure": "【发票处理失败】{{run_name}}",
    }[event]
    subject = render_message(subject_pattern, context)
    body = render_message(messages["success_body" if event == "success" else f"{event}_body"], context)
    failed = False
    for specification in run.config_snapshot["deliveries"]:
        channel = await db.get(NotificationChannel, specification["channel_id"])
        targets = [*specification["targets"], *specification.get("cc", [])]
        for target in targets:
            parts = [("message", [])] + [(f"file:{a.id}", [a]) for a in artifacts] if channel and channel.kind == "wecom_aibot" else [("envelope", artifacts)]
            for part, files in parts:
                part = f"{event}:{part}"
                delivery = (await db.execute(select(Delivery).where(Delivery.run_id == run.id, Delivery.channel_id == specification["channel_id"], Delivery.target == target, Delivery.part == part))).scalar_one_or_none()
                if delivery is None:
                    delivery = Delivery(run_id=run.id, channel_id=specification["channel_id"], target=target, part=part, state="pending", attempts=[])
                    db.add(delivery)
                    await db.commit()
                if delivery.state == "sent":
                    continue
                if delivery.state == "inflight":
                    delivery.state, delivery.error = "unknown", "执行中断，渠道接收结果未知；请人工确认后决定是否重发"
                    if delivery.attempts:
                        delivery.attempts = [*delivery.attempts[:-1], {**delivery.attempts[-1], "state": "unknown", "error": delivery.error}]
                    await db.commit()
                if delivery.state in {"unknown", "failed"}:
                    failed = True
                    continue
                retry_offset = (delivery.receipt or {}).get("retry_offset", 0)
                while len(delivery.attempts or []) - retry_offset < 3:
                    if await _cancelled(db, run, lock=True):
                        return
                    owner = await _owner(db, run)
                    for item in items:
                        await _invoice(db, item, owner)
                    if channel is not None:
                        await db.refresh(channel)
                    if channel is None or not channel.enabled:
                        delivery.state, delivery.error = "failed", "通知渠道不存在或已停用"
                        await db.commit()
                        break
                    delivery.state = "inflight"
                    delivery.attempts = [*(delivery.attempts or []), {"at": datetime.utcnow().isoformat()}]
                    await db.commit()
                    try:
                        headers = {"to_targets": specification["targets"], "cc_targets": specification.get("cc", [])} if channel.kind == "email" else {}
                        receipt = await deliver(channel, target, subject, body if not part.startswith(f"{event}:file:") else "",
                                                [{"name": a.file_name, "content_type": a.content_type, "data": a.file_data} for a in files], **headers)
                    except DeliveryRetryable as exc:
                        delivery.state, delivery.error = "pending", str(exc)[:500]
                    except DeliveryRejected as exc:
                        delivery.state, delivery.error = "failed", str(exc)[:500]
                    except Exception as exc:
                        # An unexpected post-dispatch exception cannot prove non-delivery.
                        delivery.state, delivery.error = "unknown", str(exc)[:500]
                    else:
                        delivery.state, delivery.receipt, delivery.error = "sent", receipt, None
                    delivery.attempts = [*delivery.attempts[:-1], {**delivery.attempts[-1], "state": delivery.state,
                                          "error": delivery.error, "receipt": delivery.receipt if delivery.state == "sent" else None}]
                    await db.commit()
                    if delivery.state != "pending":
                        break
                    if len(delivery.attempts) - retry_offset < 3:
                        await asyncio.sleep(len(delivery.attempts) - retry_offset)
                if delivery.state != "sent":
                    failed = True
                    if delivery.state == "pending":
                        delivery.state = "failed"
                        await db.commit()
    if event == "success" and not await _cancelled(db, run, lock=True):
        run.state = "delivery_failed" if failed else "completed"
        run.completed_at = None if failed else datetime.utcnow()
        run.error = "部分投递失败或结果未知，请查看投递记录" if failed else None
        await db.commit()


async def _notify_event(db, run, items, event):
    run_id = run.id
    try:
        await _send(db, run, items, event)
    except LeaseLost:
        raise
    except Exception as exc:
        await db.rollback()
        run = await db.get(WorkflowRun, run_id)
        run.attempts = {**(run.attempts or {}), f"{event}_notification_error": str(exc)[:500]}
        await db.commit()


async def process_run(db, run):
    """Advance one run; caller holds the worker's database-wide advisory lock."""
    run_id = run.id
    run._worker_lease_owner = run.lease_owner
    items = list((await db.execute(select(WorkflowRunItem).where(WorkflowRunItem.run_id == run_id).order_by(WorkflowRunItem.id))).scalars())
    try:
        owner = await _owner(db, run)
        if not items:
            raise ValueError("批次中没有发票")
        if not run.frozen_at:
            if await _cancelled(db, run, lock=True):
                return
            await _validate_attachment_bindings(db, run)
            run.state = "recognizing"
            await db.commit()
            item_ids = [item.id for item in items]
            for item_id in item_ids:
                run = await db.get(WorkflowRun, run_id)
                if await _cancelled(db, run):
                    return
                item = await db.get(WorkflowRunItem, item_id)
                owner = await _owner(db, run)
                invoice = await _invoice(db, item, owner)
                if item.state == "pending":
                    while (item.attempts or {}).get("ocr", 0) < 3:
                        if await _cancelled(db, run):
                            return
                        attempts = dict(item.attempts or {})
                        attempts["ocr"] = attempts.get("ocr", 0) + 1
                        item.attempts = attempts
                        await db.commit()
                        invoice_id = invoice.id
                        ok = await process_invoice(invoice_id, db)
                        run = await db.get(WorkflowRun, run_id)
                        item = await db.get(WorkflowRunItem, item_id)
                        invoice = await db.get(Invoice, invoice_id)
                        if ok:
                            item.state, item.error = "recognized", None
                            break
                        if attempts["ocr"] < 3:
                            await asyncio.sleep(attempts["ocr"])
                    if item.state == "pending":
                        item.state, item.error = "failed", "OCR 识别已达到重试上限，请人工重试"
                    await db.commit()
            if await _cancelled(db, run, lock=True):
                return
            run.state = "reviewing"
            await db.commit()
            for item_id in item_ids:
                run = await db.get(WorkflowRun, run_id)
                if await _cancelled(db, run):
                    return
                item = await db.get(WorkflowRunItem, item_id)
                owner = await _owner(db, run)
                if item.state == "failed":
                    continue
                while True:
                    try:
                        await _review(db, run, item, owner)
                        break
                    except LeaseLost:
                        raise
                    except Exception as exc:
                        await db.rollback()
                        item = await db.get(WorkflowRunItem, item_id)
                        retry = not isinstance(exc, ValueError) and (item.attempts or {}).get("verify", 0) < 3
                        item.state, item.error = "recognized" if retry else "needs_review", str(exc)[:500]
                        await db.commit()
                        run = await db.get(WorkflowRun, run_id)
                        if not retry or await _cancelled(db, run):
                            break
                        await asyncio.sleep((item.attempts or {}).get("verify", 1))
                        owner = await _owner(db, run)
            items = list((await db.execute(select(WorkflowRunItem).where(WorkflowRunItem.run_id == run_id).order_by(WorkflowRunItem.id))).scalars())
            if await _cancelled(db, run, lock=True):
                return
            if any(item.state != "approved" for item in items):
                run.state, run.error = "needs_review", "存在识别失败或待人工审核的发票"
                await db.commit()
                if run.config_snapshot.get("notify_review"):
                    await _notify_event(db, run, items, "review")
                return
            if not await _freeze(db, run, items, await _owner(db, run)):
                return
        if await _cancelled(db, run):
            return
        if run.state in {"queued", "exporting", "recognizing", "reviewing"}:
            await _export(db, run, items)
        if not await _cancelled(db, run):
            await _send(db, run, items)
    except LeaseLost:
        await db.rollback()
    except Exception as exc:
        await db.rollback()
        run = await db.get(WorkflowRun, run_id)
        try:
            cancelled = await _cancelled(db, run, lock=True)
        except LeaseLost:
            await db.rollback()
            return
        if not cancelled:
            run.state, run.error = "failed", str(exc)[:500]
            await db.commit()
            if run.config_snapshot.get("notify_failure"):
                try:
                    items = list((await db.execute(select(WorkflowRunItem).where(WorkflowRunItem.run_id == run_id))).scalars())
                    await _notify_event(db, run, items, "failure")
                except Exception:
                    await db.rollback()


async def retry_run(db, run):
    """An explicit retry authorizes new attempts, never unknown delivery replay."""
    if run.state not in {"failed", "delivery_failed", "needs_review"}:
        raise ValueError("当前批次不可重试")
    if run.frozen_at:
        deliveries = list((await db.execute(select(Delivery).where(Delivery.run_id == run.id))).scalars())
        for delivery in deliveries:
            if delivery.state == "failed":
                delivery.state, delivery.error = "pending", None
                delivery.receipt = {**(delivery.receipt or {}), "retry_offset": len(delivery.attempts or [])}
        run.state = "sending" if run.state == "delivery_failed" else "queued"
    else:
        for item in (await db.execute(select(WorkflowRunItem).where(WorkflowRunItem.run_id == run.id))).scalars():
            if item.state == "failed":
                item.state = "pending"
            if item.state in {"pending", "needs_review"}:
                item.attempts = {**(item.attempts or {}), "verify": 0, "ocr": 0}
                item.error = None
        run.state = "queued"
    run.error, run.next_attempt_at = None, None
