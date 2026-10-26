"""Batch sealing and authorization must hold independently of the UI."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import Base
from app.models.attachment import Attachment
from app.models.invoice import Invoice, InvoiceStatus, OcrResult
from app.models.user import User, UserRole
from app.models.workflow import Delivery, ExportArtifact, ExportTemplate, NotificationChannel, WorkflowDefinition, WorkflowRun, WorkflowRunItem
from app.routers import workflows
from app.schemas.workflow import DeliveryResolution, ManualApproval, UploadManifest, WorkflowRunCreate, WorkflowSave


class AsyncDb:
    """Use isolated SQLite without adding a runtime async driver for unit tests."""

    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    async def get(self, model, key):
        return self.session.get(model, key)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def refresh(self, value, **kwargs):
        self.session.refresh(value, **kwargs)

    async def rollback(self):
        self.session.rollback()

    def add(self, value):
        self.session.add(value)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield AsyncDb(session)
    engine.dispose()


def user(name="alice", admin=False):
    return SimpleNamespace(username=name, role=UserRole.ADMIN if admin else UserRole.USER, enabled=True)


def seed(db, attachment=False):
    definition = WorkflowDefinition(name="test", config={}, enabled=True)
    invoice = Invoice(file_name="invoice.pdf", file_type="pdf", file_data=b"invoice", owner="alice")
    db.session.add_all([definition, invoice])
    db.session.flush()
    run = WorkflowRun(workflow_id=definition.id, name="batch", owner="alice", config_snapshot={},
                      upload_manifest={"invoices": [{"id": "invoice-1", "name": "invoice.pdf"}],
                                       "attachments": [{"id": "attachment-1", "name": "contract.pdf"}] if attachment else []},
                      uploaded_attachments={}, upload_errors={})
    db.session.add(run)
    db.session.flush()
    item = WorkflowRunItem(run_id=run.id, invoice_id=invoice.id, client_file_id="invoice-1")
    db.session.add(item)
    db.session.flush()
    return run, item, invoice


def test_manifest_rejects_cross_kind_duplicate_and_empty_invoice_list():
    with pytest.raises(ValidationError):
        UploadManifest(invoices=[{"id": "same", "name": "invoice"}], attachments=[{"id": "same", "name": "attachment"}])
    with pytest.raises(ValidationError):
        UploadManifest(invoices=[])


def test_workflow_rejects_duplicate_format_and_target():
    with pytest.raises(ValidationError):
        WorkflowSave(name="test", config={"formats": ["excel", "excel"]})
    with pytest.raises(ValidationError):
        WorkflowSave(name="test", config={"formats": ["pdf"], "deliveries": [{"channel_id": 1, "targets": ["a", "a"]}]})
    with pytest.raises(ValidationError):
        WorkflowSave(name="test", config={"formats": ["pdf"], "deliveries": [{"channel_id": 1, "targets": ["a"], "cc": ["a"]}]})


def test_manual_approval_requires_reviewed_source_hash_and_reason():
    with pytest.raises(ValidationError):
        ManualApproval(reason="已核对")
    with pytest.raises(ValidationError):
        ManualApproval(reason=" ", source_hash="0" * 64)


def test_ordinary_user_cannot_create_workflow():
    app = FastAPI()
    app.include_router(workflows.router)
    app.dependency_overrides[get_current_user] = lambda: user()
    with TestClient(app) as client:
        response = client.post("/workflows", json={"name": "test", "config": {"formats": ["pdf"]}})
    assert response.status_code == 403


def test_foreign_run_is_hidden_and_admin_can_access(db):
    run, _, _ = seed(db)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(workflows.get_accessible_run(db, run.id, user("bob")))
    assert exc.value.status_code == 404
    assert asyncio.run(workflows.get_accessible_run(db, run.id, user("admin", True))).id == run.id


def test_batch_waits_for_every_manifest_file_and_rejects_wrong_attachment(db):
    run, _, invoice = seed(db, attachment=True)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(workflows._check_uploads(db, run, user()))
    assert exc.value.status_code == 409
    other = Invoice(file_name="other.pdf", file_type="pdf", file_data=b"x", owner="alice")
    db.session.add(other)
    db.session.flush()
    attachment = Attachment(invoice_id=other.id, file_name="contract.pdf", file_type="pdf", file_data=b"contract")
    db.session.add(attachment)
    db.session.flush()
    run.uploaded_attachments = {"attachment-1": attachment.id}
    with pytest.raises(HTTPException):
        asyncio.run(workflows._check_uploads(db, run, user()))
    attachment.invoice_id = invoice.id
    db.session.flush()
    asyncio.run(workflows._check_uploads(db, run, user()))
    run.upload_errors = {"attachment-1": "failed"}
    with pytest.raises(HTTPException):
        asyncio.run(workflows._check_uploads(db, run, user()))


def test_batch_rejects_extra_invoice_even_when_expected_files_exist(db):
    run, _, invoice = seed(db)
    extra = Invoice(file_name="extra.pdf", file_type="pdf", file_data=b"x", owner="alice")
    db.session.add(extra)
    db.session.flush()
    db.session.add(WorkflowRunItem(run_id=run.id, invoice_id=extra.id, client_file_id="extra"))
    db.session.flush()
    with pytest.raises(HTTPException):
        asyncio.run(workflows._check_uploads(db, run, user()))


def test_download_revalidates_current_invoice_ownership(db):
    run, _, invoice = seed(db)
    artifact = ExportArtifact(run_id=run.id, kind="excel", file_name="result.xlsx", content_type="application/octet-stream",
                              file_data=b"data", sha256="0" * 64, size=4)
    db.session.add(artifact)
    db.session.flush()
    invoice.owner = "bob"
    db.session.flush()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(workflows.download_artifact(artifact.id, user(), db))
    assert exc.value.status_code == 403


def test_draft_seals_once_and_rejects_later_uploads(db, monkeypatch):
    run, _, _ = seed(db)

    async def ready(*args):
        return None

    monkeypatch.setattr(workflows, "_ready_run", ready)
    assert asyncio.run(workflows.start_run(run.id, user(), db))["state"] == "queued"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(workflows.require_draft_run(db, run.id, user()))
    assert exc.value.status_code == 409


@pytest.mark.parametrize("change", ["move", "delete"])
def test_sealing_keeps_attachment_bindings_even_if_original_is_changed(db, monkeypatch, change):
    run, _, invoice = seed(db, attachment=True)
    attachment = Attachment(invoice_id=invoice.id, file_name="contract.pdf", file_type="pdf", file_data=b"contract")
    db.session.add(attachment)
    db.session.flush()
    run.uploaded_attachments = {"attachment-1": attachment.id}
    attachment_id = attachment.id

    async def ready(*args):
        return None

    monkeypatch.setattr(workflows, "_ready_run", ready)
    asyncio.run(workflows.start_run(run.id, user(), db))
    assert run.config_snapshot["attachment_bindings"] == {str(attachment_id): invoice.id}
    if change == "move":
        other = Invoice(file_name="other.pdf", file_type="pdf", file_data=b"other", owner="alice")
        db.session.add(other)
        db.session.flush()
        attachment.invoice_id = other.id
    else:
        db.session.delete(attachment)
    db.session.commit()
    db.session.expire(run)
    assert run.config_snapshot["attachment_bindings"] == {str(attachment_id): invoice.id}


def test_unknown_delivery_requires_explicit_resolution_and_all_unknowns_resolved(db):
    run, _, _ = seed(db)
    run.state = "delivery_failed"
    first = Delivery(run_id=run.id, channel_id=1, target="a", part="success:message", state="unknown", attempts=[{"state": "inflight"}])
    second = Delivery(run_id=run.id, channel_id=1, target="b", part="success:message", state="unknown", attempts=[])
    db.session.add_all([first, second])
    db.session.flush()
    body = DeliveryResolution(action="confirm_sent", reason="已核对收件箱")
    result = asyncio.run(workflows.resolve_delivery(run.id, first.id, body, user(), db))
    assert result["state"] == "sent" and run.state == "delivery_failed"
    result = asyncio.run(workflows.resolve_delivery(run.id, second.id, DeliveryResolution(action="retry", reason="确认未收到"), user(), db))
    assert result["state"] == "pending" and run.state == "sending"
    assert second.receipt["retry_offset"] == len(second.attempts)
    assert first.attempts[0] == {"state": "inflight"}
    with pytest.raises(HTTPException):
        asyncio.run(workflows.resolve_delivery(run.id, first.id, body, user(), db))


def test_resume_cannot_race_manual_reprocessing(db, monkeypatch):
    run, _, invoice = seed(db)
    run.state = "needs_review"
    invoice.status = InvoiceStatus.PROCESSING
    db.session.flush()

    async def ready(*args):
        return None

    monkeypatch.setattr(workflows, "_ready_run", ready)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(workflows.resume_run(run.id, user(), db))
    assert exc.value.status_code == 409
    assert run.state == "needs_review"


def test_resume_retries_failed_ocr_item_without_losing_approved_items(db, monkeypatch):
    run, failed, _ = seed(db)
    run.state = "needs_review"
    failed.state, failed.error, failed.attempts = "failed", "OCR failure", {"ocr": 3, "verify": 1}
    invoice = Invoice(file_name="approved.pdf", file_type="pdf", file_data=b"approved", owner="alice")
    db.session.add(invoice)
    db.session.flush()
    approved = WorkflowRunItem(run_id=run.id, invoice_id=invoice.id, client_file_id="approved",
                               state="approved", source_hash="a" * 64, approval={"reason": "已核对"})
    db.session.add(approved)
    db.session.flush()

    async def ready(*args):
        return None

    monkeypatch.setattr(workflows, "_ready_run", ready)
    response = asyncio.run(workflows.resume_run(run.id, user(), db))
    assert response["state"] == "queued"
    assert failed.state == "pending" and failed.error is None
    assert failed.attempts["ocr"] == 0 and failed.attempts["verify"] == 0
    assert approved.state == "approved" and approved.source_hash == "a" * 64
    assert approved.approval == {"reason": "已核对"}


def test_enabled_workflow_requires_published_template_channel_and_ai(db, monkeypatch):
    template = ExportTemplate(name="template", draft_config={}, enabled=True)
    channel = NotificationChannel(name="mail", kind="email", config={}, enabled=True)
    db.session.add_all([template, channel])
    db.session.flush()
    config = {"formats": ["excel"], "template_id": template.id, "deliveries": [{"channel_id": channel.id, "targets": ["a@example.com"], "cc": ["b@example.com"]}]}
    with pytest.raises(HTTPException, match="Excel"):
        asyncio.run(workflows._configuration(db, config, True))
    template.published_config = {"columns": [{"field": "invoice_number"}]}
    db.session.flush()
    with pytest.raises(HTTPException, match="AI"):
        asyncio.run(workflows._configuration(db, config, True))

    async def provider(*args):
        return SimpleNamespace(enabled=True, api_key="test-only", model="test")

    monkeypatch.setattr(workflows, "get_active_provider_row", provider)
    validated, selected = asyncio.run(workflows._configuration(db, config, True))
    assert selected.id == template.id
    assert validated["deliveries"][0]["cc"] == ["b@example.com"]
    assert "success_body" in validated["messages"]
    channel.enabled = False
    db.session.flush()
    with pytest.raises(HTTPException, match="渠道"):
        asyncio.run(workflows._configuration(db, config, True))


def test_bot_rejects_email_cc_and_unknown_message_variables(db):
    channel = NotificationChannel(name="bot", kind="wecom_aibot", config={}, enabled=False)
    db.session.add(channel)
    db.session.flush()
    config = {"formats": ["pdf"], "deliveries": [{"channel_id": channel.id, "targets": ["user1"], "cc": ["mail@example.com"]}]}
    with pytest.raises(HTTPException, match="抄送"):
        asyncio.run(workflows._configuration(db, config, False))
    config["deliveries"] = []
    config["messages"] = {"success_body": "{{unknown}}"}
    with pytest.raises(HTTPException, match="变量"):
        asyncio.run(workflows._configuration(db, config, False))


def test_delivery_retry_uses_frozen_artifact_without_ai_or_live_template(db, monkeypatch):
    channel = NotificationChannel(name="mail", kind="email", config={}, enabled=True)
    db.session.add(channel)
    db.session.flush()
    config = {"formats": ["excel"], "template_id": 999,
              "deliveries": [{"channel_id": channel.id, "targets": ["a@example.com"]}],
              "template": {"published": "original"}}

    async def unexpected_ai(*args):
        pytest.fail("Sending an existing artifact must not require the AI provider")

    monkeypatch.setattr(workflows, "get_active_provider_row", unexpected_ai)
    validated, _ = asyncio.run(workflows._configuration(db, config, True, require_processing=False))
    assert validated["template"] == {"published": "original"}
    run, _, _ = seed(db)
    definition = db.session.get(WorkflowDefinition, run.workflow_id)
    definition.enabled = False
    run.state = "delivery_failed"
    run.frozen_at = datetime.utcnow()
    run.config_snapshot = config
    db.session.flush()
    asyncio.run(workflows._ready_run(db, run))
    run.state = "draft"
    with pytest.raises(HTTPException, match="停用"):
        asyncio.run(workflows._ready_run(db, run))


@pytest.fixture
def existing_selection(db, monkeypatch):
    from app.services.excel_template_service import default_config
    template = ExportTemplate(name="published", draft_config=default_config(), published_config=default_config(), version=1, enabled=True)
    channel = NotificationChannel(name="isolated", kind="email", config={}, enabled=True)
    db.session.add_all([template, channel])
    db.session.flush()
    definition = WorkflowDefinition(name="selection", enabled=True, config={
        "formats": ["excel"], "template_id": template.id,
        "deliveries": [{"channel_id": channel.id, "targets": ["test@example.invalid"]}],
        "include_attachments": True,
    })
    invoices = [Invoice(file_name=f"existing-{index}.pdf", file_type="pdf", file_data=b"original", owner=owner,
                        status=InvoiceStatus.CONFIRMED, invoice_number=f"0000000{index}",
                        buyer_name="人工修正买方", buyer_tax_id="001", seller_name="卖方", seller_tax_id="002",
                        issue_date=datetime(2026, 10, 24).date(), item_name="服务费", amount=100, tax_amount=13, total_with_tax=113)
                for index, owner in enumerate(["alice", "alice", "bob"], 1)]
    db.session.add_all([definition, *invoices, User(username="alice", password_hash="test-only", role=UserRole.USER, enabled=True)])
    db.session.flush()
    for invoice in invoices:
        db.session.add(OcrResult(invoice_id=invoice.id, raw_text="历史 OCR，当前字段可能已人工修正"))
        db.session.add(Attachment(invoice_id=invoice.id, file_name=f"contract-{invoice.id}.pdf", file_type="pdf", file_data=b"contract"))
    db.session.commit()

    async def provider(*args):
        return SimpleNamespace(enabled=True, api_key="test-only", model="isolated")
    monkeypatch.setattr(workflows, "get_active_provider_row", provider)
    return definition, invoices


def test_selected_rows_create_queued_run_without_reupload_or_overwriting_fields(db, existing_selection):
    definition, invoices = existing_selection
    body = WorkflowRunCreate(workflow_id=definition.id, name="列表执行", invoice_ids=[invoices[1].id, invoices[0].id])
    response = asyncio.run(workflows.create_run(body, user(), db))
    assert response["state"] == "queued"
    assert len(db.session.scalars(select(Invoice)).all()) == 3
    run = db.session.get(WorkflowRun, response["id"])
    items = db.session.scalars(select(WorkflowRunItem).where(WorkflowRunItem.run_id == run.id).order_by(WorkflowRunItem.id)).all()
    assert [item.invoice_id for item in items] == body.invoice_ids
    assert all(item.state == "recognized" for item in items)
    assert invoices[0].buyer_name == "人工修正买方"
    assert len(run.uploaded_attachments) == 2
    assert set(run.config_snapshot["attachment_bindings"].values()) == set(body.invoice_ids)
    assert len(run.upload_manifest["invoices"]) == 2
    assert len(run.upload_manifest["attachments"]) == 2
    assert run.config_snapshot["template_version"] == 1
    asyncio.run(workflows._check_uploads(db, run, user()))


@pytest.mark.parametrize("bad_id", ["foreign", "missing"])
def test_selection_rejects_any_unavailable_invoice_atomically(db, existing_selection, bad_id):
    definition, invoices = existing_selection
    invoice_id = invoices[2].id if bad_id == "foreign" else 999999
    body = WorkflowRunCreate(workflow_id=definition.id, name="不可越权", invoice_ids=[invoices[0].id, invoice_id])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(workflows.create_run(body, user(), db))
    assert exc.value.status_code == 404
    assert db.session.scalars(select(WorkflowRun)).all() == []
    assert db.session.scalars(select(WorkflowRunItem)).all() == []


@pytest.mark.parametrize("status", [InvoiceStatus.UPLOADED, InvoiceStatus.PROCESSING])
def test_selection_waits_for_ordinary_upload_recognition(db, existing_selection, status):
    definition, invoices = existing_selection
    invoices[0].status = status
    db.session.commit()
    body = WorkflowRunCreate(workflow_id=definition.id, name="等待识别", invoice_ids=[invoices[0].id])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(workflows.create_run(body, user(), db))
    assert exc.value.status_code == 409
    assert db.session.scalars(select(WorkflowRun)).all() == []


@pytest.mark.parametrize("state", ["queued", "needs_review", "delivery_failed"])
def test_selection_cannot_reuse_invoice_in_an_unfinished_run(db, existing_selection, state):
    definition, invoices = existing_selection
    previous = WorkflowRun(workflow_id=definition.id, name="existing", owner="alice", state=state, config_snapshot={})
    db.session.add(previous)
    db.session.flush()
    db.session.add(WorkflowRunItem(run_id=previous.id, invoice_id=invoices[0].id))
    db.session.commit()
    body = WorkflowRunCreate(workflow_id=definition.id, name="重复执行", invoice_ids=[invoices[0].id])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(workflows.create_run(body, user(), db))
    assert exc.value.status_code == 409
    assert len(db.session.scalars(select(WorkflowRun)).all()) == 1


def test_admin_selection_reuses_foreign_rows_but_failed_ocr_restarts_recognition(db, existing_selection):
    definition, invoices = existing_selection
    invoices[2].status = InvoiceStatus.FAILED
    db.session.commit()
    body = WorkflowRunCreate(workflow_id=definition.id, name="管理员执行", invoice_ids=[invoices[2].id])
    result = asyncio.run(workflows.create_run(body, user("admin", True), db))
    assert result["owner"] == "admin" and result["state"] == "queued"
    assert db.session.scalars(select(WorkflowRunItem)).one().state == "pending"


@pytest.mark.parametrize("fields", [
    {}, {"invoice_ids": []}, {"invoice_ids": [1, 1]}, {"invoice_ids": [0]},
    {"invoice_ids": list(range(1, 102))},
    {"invoice_ids": [1], "upload_manifest": {"invoices": [{"id": "a", "name": "a.pdf"}]}},
])
def test_run_create_requires_exactly_one_valid_source(fields):
    with pytest.raises(ValidationError):
        WorkflowRunCreate(workflow_id=1, name="source", **fields)


def test_selected_workflow_reuses_ocr_but_always_reviews_current_values(db, existing_selection, monkeypatch):
    from unittest.mock import AsyncMock
    from app.services import workflow_service, notification_service
    definition, invoices = existing_selection
    response = asyncio.run(workflows.create_run(WorkflowRunCreate(
        workflow_id=definition.id, name="现有发票执行闭环", invoice_ids=[invoices[0].id, invoices[1].id],
    ), user(), db))
    ocr = AsyncMock(side_effect=AssertionError("不能重复识别并覆盖人工修正"))
    reviewed = []
    async def verify(db, invoice):
        reviewed.append((invoice.id, invoice.buyer_name))
        return {"verdict": {"overall": "pass", "fields": []}}
    send = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr(workflow_service, "process_invoice", ocr)
    monkeypatch.setattr(workflow_service, "verify_invoice", verify)
    monkeypatch.setattr(notification_service, "deliver", send)
    run = db.session.get(WorkflowRun, response["id"])
    asyncio.run(workflow_service.process_run(db, run))
    assert run.state == "completed", run.error
    assert reviewed == [(invoices[0].id, "人工修正买方"), (invoices[1].id, "人工修正买方")]
    ocr.assert_not_awaited()
    send.assert_awaited_once()
    assert {row.kind for row in db.session.scalars(select(ExportArtifact))} == {"excel", "zip"}


def test_selection_without_ocr_requires_recognition_and_completed_run_allows_new_execution(db, existing_selection):
    definition, invoices = existing_selection
    db.session.delete(db.session.scalars(select(OcrResult).where(OcrResult.invoice_id == invoices[0].id)).one())
    previous = WorkflowRun(workflow_id=definition.id, name="finished", owner="alice", state="completed", config_snapshot={})
    db.session.add(previous)
    db.session.flush()
    db.session.add(WorkflowRunItem(run_id=previous.id, invoice_id=invoices[0].id))
    db.session.commit()
    result = asyncio.run(workflows.create_run(WorkflowRunCreate(
        workflow_id=definition.id, name="再次执行", invoice_ids=[invoices[0].id],
    ), user(), db))
    item = db.session.scalars(select(WorkflowRunItem).where(WorkflowRunItem.run_id == result["id"])).one()
    assert item.state == "pending" and result["state"] == "queued"


def test_selection_keeps_batch_file_limit_with_existing_attachments(db, existing_selection):
    definition, invoices = existing_selection
    db.session.add_all([Attachment(invoice_id=invoices[0].id, file_name=f"extra-{i}.pdf", file_type="pdf", file_data=b"x") for i in range(99)])
    db.session.commit()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(workflows.create_run(WorkflowRunCreate(workflow_id=definition.id, name="oversize", invoice_ids=[invoices[0].id]), user(), db))
    assert exc.value.status_code == 409
    assert db.session.scalars(select(WorkflowRun)).all() == []
