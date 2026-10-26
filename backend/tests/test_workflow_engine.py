"""Workflow business invariants, isolated SQLite ledger and mocked external IO."""

import json
from datetime import date
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models.attachment import Attachment
from app.models.invoice import AiVerification, Invoice, InvoiceStatus, OcrResult
from app.models.user import User, UserRole
from app.models.workflow import Delivery, ExportArtifact, NotificationChannel, WorkflowDefinition, WorkflowRun, WorkflowRunItem
from app.services import workflow_service as service
from app.services.excel_template_service import default_config
from app.services.invoice_service import parse_verify_response


class AsyncTestSession:
    """Exercise actual SQLAlchemy persistence without a new async SQLite dependency."""
    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    async def get(self, model, key):
        return self.session.get(model, key)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def refresh(self, instance, **kwargs):
        self.session.refresh(instance, **kwargs)

    def add(self, instance):
        self.session.add(instance)


@pytest.fixture
def ledger():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        owner = User(username="alice", password_hash="unused", enabled=True, role=UserRole.USER)
        invoice = Invoice(file_name="test.pdf", file_type="pdf", file_data=b"original", owner="alice", status=InvoiceStatus.CONFIRMED,
                          invoice_number="00001234567890123456", issue_date=date(2026, 10, 24), buyer_name="Buyer", buyer_tax_id="001",
                          seller_name="Seller", seller_tax_id="002", item_name="Item", amount=Decimal("100"), tax_amount=Decimal("13"), total_with_tax=Decimal("113"))
        definition = WorkflowDefinition(name="test", enabled=True, config={})
        channel = NotificationChannel(name="email", kind="email", enabled=True, config={})
        session.add_all([owner, invoice, definition, channel])
        session.flush()
        run = WorkflowRun(workflow_id=definition.id, name="test run", owner="alice", state="queued", config_snapshot={
            "formats": ["excel"], "template": default_config(), "deliveries": [{"channel_id": channel.id, "targets": ["test@example.com"]}],
        })
        session.add(run)
        session.flush()
        item = WorkflowRunItem(run_id=run.id, invoice_id=invoice.id, state="recognized")
        session.add(item)
        session.commit()
        yield SimpleNamespace(db=AsyncTestSession(session), session=session, run=run, item=item, invoice=invoice, owner=owner, channel=channel)
    engine.dispose()


def pass_verdict():
    return {"overall": "pass", "summary": "ok", "fields": [{"field": name, "status": "correct", "corrected_value": None} for name in service.COMPARABLE_FIELDS]}


def mock_external(monkeypatch, verdict=None, delivery=None):
    import app.services.notification_service as notifications

    async def verify(db, invoice):
        return {"verdict": verdict or pass_verdict()}

    async def send(*args, **kwargs):
        return {"accepted": True}

    monkeypatch.setattr(service, "verify_invoice", verify)
    monkeypatch.setattr(notifications, "deliver", delivery or send)


@pytest.mark.asyncio
@pytest.mark.parametrize("event, label", [("review", "待人工处理"), ("failure", "处理失败")])
async def test_exception_notifications_never_claim_the_batch_completed(ledger, monkeypatch, event, label):
    subjects = []
    async def capture(channel, target, subject, *args, **kwargs):
        subjects.append(subject)
        return {"accepted": True}
    mock_external(monkeypatch, delivery=capture)
    await service._send(ledger.db, ledger.run, [ledger.item], event)
    assert len(subjects) == 1 and label in subjects[0] and "处理完成" not in subjects[0]


def test_money_gate_accepts_zero_and_rejects_nonfinite_and_mismatch(ledger):
    data = service.snapshot_invoice(ledger.invoice)
    data.update(amount="0.00", tax_amount="0.00", total_with_tax="0.00")
    assert service.validate_invoice(data) == []
    data["total_with_tax"] = "1.00"
    assert any("之和" in error for error in service.validate_invoice(data))
    data["total_with_tax"] = "NaN"
    assert any("格式" in error for error in service.validate_invoice(data))


@pytest.mark.parametrize("name,value", [("quantity", "NaN"), ("unit_price", "Infinity"), ("unit_price", "0.00001")])
def test_warning_only_fields_still_require_valid_storable_numbers(ledger, name, value):
    data = service.snapshot_invoice(ledger.invoice)
    data[name] = value
    assert any(name in error for error in service.validate_invoice(data))


def test_fingerprint_changes_for_raw_file_and_attachments(ledger):
    original = service.source_fingerprint(ledger.invoice)
    ledger.invoice.file_data = b"changed original"
    assert service.source_fingerprint(ledger.invoice) != original
    attachment = SimpleNamespace(id=1, invoice_id=ledger.invoice.id, file_name="a.pdf", file_data=b"a", category="other", note=None)
    first = service.source_fingerprint(ledger.invoice, [attachment])
    attachment.file_data = b"b"
    assert service.source_fingerprint(ledger.invoice, [attachment]) != first


def test_fingerprint_is_stable_across_database_decimal_scale(ledger):
    ledger.invoice.total_with_tax = Decimal("113")
    before = service.source_fingerprint(ledger.invoice)
    ledger.session.commit()
    ledger.session.refresh(ledger.invoice)
    assert str(ledger.invoice.total_with_tax) == "113.00"
    assert service.source_fingerprint(ledger.invoice) == before


@pytest.mark.asyncio
async def test_existing_confirmed_state_still_requires_current_ai_verdict(ledger, monkeypatch):
    verdict = pass_verdict()
    verdict.update(overall="issues", summary="unsure")
    verdict["fields"][0]["status"] = "unsure"
    mock_external(monkeypatch, verdict=verdict)
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "needs_review"
    assert ledger.session.scalars(select(ExportArtifact)).all() == []
    assert ledger.session.scalars(select(Delivery)).all() == []


@pytest.mark.asyncio
async def test_completed_run_reuses_frozen_file_and_never_resends_sent_delivery(ledger, monkeypatch):
    sent = []
    async def deliver(*args, **kwargs):
        sent.append(args)
        return {"accepted": True}
    mock_external(monkeypatch, delivery=deliver)
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "completed", ledger.run.error
    artifact = ledger.session.scalars(select(ExportArtifact)).one()
    original = artifact.file_data
    assert original.startswith(b"PK")
    assert len(sent) == 1
    ledger.invoice.total_with_tax = Decimal("999")
    ledger.run.state = "sending"
    ledger.session.commit()
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "completed"
    assert len(sent) == 1
    assert ledger.session.scalars(select(ExportArtifact)).one().file_data == original


@pytest.mark.asyncio
async def test_unknown_delivery_requires_explicit_resolution_and_is_not_retried(ledger, monkeypatch):
    from app.services.notification_service import DeliveryUnknown
    sent = []
    async def deliver(*args, **kwargs):
        sent.append(args)
        raise DeliveryUnknown("response lost")
    mock_external(monkeypatch, delivery=deliver)
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "delivery_failed", ledger.run.error
    delivery = ledger.session.scalars(select(Delivery)).one()
    assert delivery.state == "unknown"
    await service.retry_run(ledger.db, ledger.run)
    await ledger.db.commit()
    await service.process_run(ledger.db, ledger.run)
    assert len(sent) == 1
    assert delivery.state == "unknown"


@pytest.mark.asyncio
async def test_inflight_after_restart_becomes_unknown_without_network_send(ledger, monkeypatch):
    mock_external(monkeypatch)
    await service.process_run(ledger.db, ledger.run)
    delivery = ledger.session.scalars(select(Delivery)).one()
    delivery.state, ledger.run.state = "inflight", "sending"
    ledger.session.commit()
    async def forbidden(*args, **kwargs):
        pytest.fail("Recovered inflight delivery must not send again")
    mock_external(monkeypatch, delivery=forbidden)
    await service.process_run(ledger.db, ledger.run)
    assert delivery.state == "unknown"
    assert ledger.run.state == "delivery_failed"


@pytest.mark.asyncio
async def test_manual_approval_requires_current_source_and_hard_validation(ledger):
    ledger.run.state = "needs_review"
    source = service.source_fingerprint(ledger.invoice)
    ledger.invoice.total_with_tax = Decimal("114")
    with pytest.raises(ValueError, match="之和"):
        await service.approve_item(ledger.db, ledger.item, ledger.owner, "checked", source)
    ledger.invoice.total_with_tax = Decimal("113")
    ledger.invoice.buyer_name = "Changed"
    with pytest.raises(ValueError, match="刷新"):
        await service.approve_item(ledger.db, ledger.item, ledger.owner, "checked", source)
    await service.approve_item(ledger.db, ledger.item, ledger.owner, "checked", service.source_fingerprint(ledger.invoice))
    assert ledger.item.state == "approved"
    assert ledger.item.approval["reason"] == "checked"


@pytest.mark.asyncio
async def test_changed_manual_approval_cannot_freeze(ledger):
    ledger.item.state = "approved"
    ledger.item.data_snapshot = service.snapshot_invoice(ledger.invoice)
    ledger.item.source_hash = service.source_fingerprint(ledger.invoice)
    ledger.invoice.buyer_name = "Changed"
    ledger.session.commit()
    assert not await service._freeze(ledger.db, ledger.run, [ledger.item], ledger.owner)
    assert ledger.run.state == "needs_review"
    assert not ledger.run.frozen_at


@pytest.mark.asyncio
async def test_disabled_owner_cannot_send(ledger, monkeypatch):
    mock_external(monkeypatch)
    ledger.owner.enabled = False
    ledger.session.commit()
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "failed"
    assert "停用" in ledger.run.error
    assert not ledger.session.scalars(select(Delivery)).all()


@pytest.mark.asyncio
async def test_auto_correction_is_one_round_and_requires_reverification(ledger, monkeypatch):
    mock_external(monkeypatch)
    calls = []
    async def verify(db, invoice):
        calls.append(invoice.buyer_name)
        verdict = pass_verdict()
        if len(calls) == 1:
            verdict["overall"] = "issues"
            verdict["fields"] = [{"field": "buyer_name", "status": "incorrect", "corrected_value": "Correct Buyer", "reason": "原件"}]
        return {"verdict": verdict}
    monkeypatch.setattr(service, "verify_invoice", verify)
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "completed", ledger.run.error
    assert calls == ["Buyer", "Correct Buyer"]
    assert ledger.item.corrections[0]["old"] == "Buyer"
    assert ledger.item.data_snapshot["buyer_name"] == "Correct Buyer"


@pytest.mark.asyncio
@pytest.mark.parametrize("money_mismatch", [False, True])
async def test_shared_ai_policy_preserves_warnings_without_extra_calls_and_keeps_money_gate(ledger, monkeypatch, money_mismatch):
    from app.services import ai_verification_service as ai

    mock_external(monkeypatch)
    monkeypatch.setattr(service, "verify_invoice", ai.verify_invoice)
    ledger.invoice.item_name = "运输服务*客运服务费"
    ledger.invoice.quantity = Decimal("2.5")
    ledger.invoice.unit_price = Decimal("40")
    if money_mismatch:
        ledger.invoice.total_with_tax = Decimal("114")
    ledger.session.add(OcrResult(invoice_id=ledger.invoice.id, raw_text="原件识别文本"))
    ledger.session.commit()
    fields = [{"field": name, "status": "correct"} for name in service.COMPARABLE_FIELDS]
    changes = {"item_name": "*运输服务*客运服务费", "invoice_type": "电子发票（普通发票）", "quantity": "99"}
    for field in fields:
        if field["field"] in changes:
            field.update(status="incorrect", corrected_value=changes[field["field"]])
    raw = json.dumps({"overall": "issues", "fields": fields, "summary": "需要人工审核"})
    calls = []
    def complete(*args):
        calls.append(args)
        return raw
    fake = SimpleNamespace(is_available=True, supports_vision=lambda: False, verify_completion=complete)
    monkeypatch.setattr(ai, "LLMService", lambda provider: fake)
    monkeypatch.setattr(ai, "get_active_provider_row", AsyncMock(return_value=SimpleNamespace(display_name="Test", name="test", model="fake")))

    await service.process_run(ledger.db, ledger.run)

    assert len(calls) == 1
    assert not ledger.item.corrections
    assert ledger.invoice.quantity == Decimal("2.5")
    assert ledger.invoice.invoice_type is None
    assert ledger.invoice.item_name == "运输服务*客运服务费"
    saved = ledger.session.scalars(select(AiVerification)).one()
    assert saved.overall == "pass"
    assert saved.raw_response == raw
    assert saved.summary == "审核通过，含 2 项提示"
    assert saved.fields == ledger.item.verification["fields"]
    if money_mismatch:
        assert ledger.run.state == "needs_review"
        assert "之和" in ledger.item.error
        assert not ledger.session.scalars(select(ExportArtifact)).all()
        assert not ledger.session.scalars(select(Delivery)).all()
    else:
        assert ledger.run.state == "completed", ledger.run.error
        assert ledger.item.data_snapshot["quantity"] == "2.5000"
        assert ledger.session.scalars(select(ExportArtifact)).one()


@pytest.mark.asyncio
async def test_mixed_review_only_corrects_blockers_and_reverifies_once(ledger, monkeypatch):
    mock_external(monkeypatch)
    ledger.invoice.quantity = Decimal("2")
    ledger.session.commit()
    calls = []
    async def verify(db, invoice):
        calls.append(invoice.buyer_name)
        fields = [{"field": name, "status": "correct"} for name in service.COMPARABLE_FIELDS]
        for field in fields:
            if field["field"] == "quantity":
                field.update(status="incorrect", corrected_value="99")
            elif field["field"] == "buyer_name" and len(calls) == 1:
                field.update(status="incorrect", corrected_value="Correct Buyer")
        return {"verdict": parse_verify_response(json.dumps({"fields": fields}), service.snapshot_invoice(invoice))}
    monkeypatch.setattr(service, "verify_invoice", verify)

    await service.process_run(ledger.db, ledger.run)

    assert ledger.run.state == "completed", ledger.run.error
    assert calls == ["Buyer", "Correct Buyer"]
    assert [change["field"] for change in ledger.item.corrections] == ["buyer_name"]
    assert ledger.invoice.quantity == Decimal("2")
    assert ledger.item.verification["summary"] == "审核通过，含 1 项提示"


@pytest.mark.asyncio
async def test_ai_exception_pauses_each_invoice_without_losing_ledger(ledger, monkeypatch):
    mock_external(monkeypatch)
    monkeypatch.setattr(service.asyncio, "sleep", AsyncMock())
    async def verify(db, invoice):
        raise RuntimeError("service unavailable")
    monkeypatch.setattr(service, "verify_invoice", verify)
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "needs_review", ledger.run.error
    assert ledger.item.state == "needs_review"
    assert "unavailable" in ledger.item.error
    assert not ledger.session.scalars(select(ExportArtifact)).all()


@pytest.mark.asyncio
async def test_retryable_delivery_has_three_attempt_budget_and_preserves_history(ledger, monkeypatch):
    from app.services.notification_service import DeliveryRetryable
    monkeypatch.setattr(service.asyncio, "sleep", AsyncMock())
    calls = []
    async def deliver(*args, **kwargs):
        calls.append(args)
        raise DeliveryRetryable("not sent")
    mock_external(monkeypatch, delivery=deliver)
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "delivery_failed", ledger.run.error
    delivery = ledger.session.scalars(select(Delivery)).one()
    assert len(calls) == len(delivery.attempts) == 3
    await service.retry_run(ledger.db, ledger.run)
    await ledger.db.commit()
    await service.process_run(ledger.db, ledger.run)
    assert len(calls) == len(delivery.attempts) == 6


@pytest.mark.asyncio
async def test_bot_sends_summary_and_files_in_separate_tracked_deliveries(ledger, monkeypatch):
    sent = []
    async def deliver(*args, **kwargs):
        sent.append(args)
        return {"accepted": True}
    mock_external(monkeypatch, delivery=deliver)
    ledger.channel.kind = "wecom_aibot"
    ledger.session.commit()
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "completed", ledger.run.error
    assert len(sent) == 2
    assert sent[0][3] and sent[0][4] == []
    assert sent[1][3] == "" and len(sent[1][4]) == 1
    assert {d.part.split(":")[1] for d in ledger.session.scalars(select(Delivery))} == {"message", "file"}


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_check_fails", [False, True])
async def test_committed_cancel_cannot_be_overwritten_by_stale_worker(ledger, monkeypatch, owner_check_fails):
    mock_external(monkeypatch)
    original_owner = service._owner

    async def cancel_after_read(db, run):
        owner = await original_owner(db, run)
        with Session(ledger.session.get_bind()) as cancelling:
            cancelled = cancelling.get(WorkflowRun, run.id)
            cancelled.state = "cancelled"
            cancelling.commit()
        assert run.state == "queued"
        if owner_check_fails:
            raise ValueError("owner validation failed after cancellation")
        return owner

    monkeypatch.setattr(service, "_owner", cancel_after_read)
    await service.process_run(ledger.db, ledger.run)

    assert ledger.run.state == "cancelled", ledger.run.error
    assert not ledger.session.scalars(select(ExportArtifact)).all()
    assert not ledger.session.scalars(select(Delivery)).all()
    assert ledger.item.attempts == {}


@pytest.mark.asyncio
async def test_error_after_lease_transfer_cannot_fail_new_workers_run(ledger, monkeypatch):
    ledger.run.lease_owner = "first"
    ledger.session.commit()

    async def fail_after_transfer(db, run):
        with Session(ledger.session.get_bind()) as replacement:
            replacement.get(WorkflowRun, run.id).lease_owner = "second"
            replacement.commit()
        raise ValueError("validation failed after lease transfer")

    monkeypatch.setattr(service, "_owner", fail_after_transfer)
    await service.process_run(ledger.db, ledger.run)

    assert ledger.run.state == "queued"
    assert ledger.run.lease_owner == "second"
    assert ledger.run.error is None


@pytest.mark.asyncio
async def test_cancelled_during_ai_never_generates_or_sends(ledger, monkeypatch):
    mock_external(monkeypatch)
    async def verify(db, invoice):
        ledger.run.state = "cancelled"
        await db.commit()
        return {"verdict": pass_verdict()}
    monkeypatch.setattr(service, "verify_invoice", verify)
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "cancelled", ledger.run.error
    assert not ledger.session.scalars(select(ExportArtifact)).all()
    assert not ledger.session.scalars(select(Delivery)).all()


@pytest.mark.asyncio
async def test_crash_after_auto_correction_resumes_verification_without_second_correction(ledger, monkeypatch):
    mock_external(monkeypatch)
    ledger.invoice.buyer_name = "Already Corrected"
    ledger.item.corrections = [{"field": "buyer_name", "old": "Buyer", "new": "Already Corrected"}]
    ledger.item.attempts = {"verify": 1}
    ledger.run.state = "reviewing"
    ledger.session.commit()
    verdict = pass_verdict()
    verdict["overall"] = "issues"
    verdict["fields"] = [{"field": "buyer_name", "status": "incorrect", "corrected_value": "Second Correction"}]
    mock_external(monkeypatch, verdict=verdict)
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "needs_review"
    assert ledger.invoice.buyer_name == "Already Corrected"
    assert len(ledger.item.corrections) == 1


@pytest.mark.asyncio
async def test_ocr_failure_survives_rollback_and_exhausts_persistent_budget(ledger, monkeypatch):
    mock_external(monkeypatch)
    monkeypatch.setattr(service.asyncio, "sleep", AsyncMock())
    ledger.item.state = "pending"
    ledger.session.commit()
    calls = []
    async def fail(invoice_id, db):
        calls.append(invoice_id)
        await db.rollback()
        return False
    monkeypatch.setattr(service, "process_invoice", fail)
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "needs_review", ledger.run.error
    assert ledger.item.state == "failed"
    assert ledger.item.attempts["ocr"] == len(calls) == 3
    assert not ledger.session.scalars(select(ExportArtifact)).all()


@pytest.mark.asyncio
async def test_cc_has_independent_envelope_and_shared_headers(ledger, monkeypatch):
    calls = []
    async def deliver(*args, **kwargs):
        calls.append((args[1], kwargs))
        return {"accepted": True}
    mock_external(monkeypatch, delivery=deliver)
    config = dict(ledger.run.config_snapshot)
    config["deliveries"] = [{"channel_id": ledger.channel.id, "targets": ["to@example.com"], "cc": ["cc@example.com"]}]
    ledger.run.config_snapshot = config
    ledger.session.commit()
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "completed", ledger.run.error
    assert [target for target, _ in calls] == ["to@example.com", "cc@example.com"]
    assert all(headers == {"to_targets": ["to@example.com"], "cc_targets": ["cc@example.com"]} for _, headers in calls)


@pytest.mark.asyncio
async def test_archive_freezes_only_selected_attachments_with_safe_member_names(ledger, monkeypatch):
    mock_external(monkeypatch)
    included = Attachment(invoice_id=ledger.invoice.id, file_name="../../contract.pdf", file_type="pdf", file_data=b"contract")
    excluded = Attachment(invoice_id=ledger.invoice.id, file_name="private.pdf", file_type="pdf", file_data=b"private")
    ledger.session.add_all([included, excluded])
    ledger.session.flush()
    ledger.run.uploaded_attachments = {"selected": included.id}
    ledger.run.config_snapshot = {**ledger.run.config_snapshot, "include_attachments": True, "include_originals": True}
    ledger.session.commit()
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "completed", ledger.run.error
    archive = ledger.session.scalars(select(ExportArtifact).where(ExportArtifact.kind == "zip")).one()
    with ZipFile(BytesIO(archive.file_data)) as zipped:
        assert len(zipped.namelist()) == 2
        assert all(".." not in name and "private" not in name for name in zipped.namelist())
        assert b"contract" in [zipped.read(name) for name in zipped.namelist()]


@pytest.mark.asyncio
async def test_attachment_changed_after_approval_blocks_freeze(ledger):
    attachment = Attachment(invoice_id=ledger.invoice.id, file_name="contract.pdf", file_type="pdf", file_data=b"old")
    ledger.session.add(attachment)
    ledger.session.flush()
    ledger.run.uploaded_attachments = {"selected": attachment.id}
    ledger.run.config_snapshot = {**ledger.run.config_snapshot, "include_attachments": True}
    ledger.item.state = "approved"
    ledger.item.source_hash = service.source_fingerprint(ledger.invoice, [attachment])
    attachment.file_data = b"new"
    ledger.session.commit()
    assert not await service._freeze(ledger.db, ledger.run, [ledger.item], ledger.owner)
    assert ledger.run.state == "needs_review"


@pytest.mark.asyncio
async def test_lost_lease_does_not_overwrite_new_workers_run(ledger, monkeypatch):
    mock_external(monkeypatch)
    ledger.run.lease_owner = "first"
    ledger.session.commit()
    async def verify(db, invoice):
        ledger.run.lease_owner = "second"
        await db.commit()
        return {"verdict": pass_verdict()}
    monkeypatch.setattr(service, "verify_invoice", verify)
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "reviewing"
    assert ledger.run.lease_owner == "second"
    assert not ledger.session.scalars(select(ExportArtifact)).all()


@pytest.mark.asyncio
async def test_auxiliary_notification_exception_preserves_review_state(ledger, monkeypatch):
    verdict = pass_verdict()
    verdict["overall"] = "issues"
    mock_external(monkeypatch, verdict=verdict)
    ledger.run.config_snapshot = {**ledger.run.config_snapshot, "notify_review": True}
    ledger.session.commit()
    async def failed_notice(*args):
        raise RuntimeError("notification config unavailable")
    monkeypatch.setattr(service, "_send", failed_notice)
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "needs_review"
    assert "notification config" in ledger.run.attempts["review_notification_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["deleted", "moved"])
async def test_missing_or_reassociated_sealed_attachment_cannot_be_omitted(ledger, monkeypatch, changed):
    mock_external(monkeypatch)
    attachment = Attachment(invoice_id=ledger.invoice.id, file_name="contract.pdf", file_type="pdf", file_data=b"contract")
    ledger.session.add(attachment)
    ledger.session.flush()
    ledger.run.uploaded_attachments = {"selected": attachment.id}
    ledger.run.config_snapshot = {**ledger.run.config_snapshot, "include_attachments": True,
                                  "attachment_bindings": {str(attachment.id): ledger.invoice.id}}
    if changed == "deleted":
        ledger.session.delete(attachment)
    else:
        attachment.invoice_id = 98765
    ledger.session.commit()
    await service.process_run(ledger.db, ledger.run)
    assert ledger.run.state == "failed", ledger.run.error
    assert "附件" in ledger.run.error
    assert not ledger.session.scalars(select(ExportArtifact)).all()
