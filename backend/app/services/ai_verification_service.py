"""Shared verification persistence for interactive review and durable workflows."""

import asyncio

from sqlalchemy import delete, select

from app.models.invoice import AiVerification, OcrResult
from app.services.invoice_service import COMPARABLE_FIELDS, _llm_executor, run_ai_verify
from app.services.llm_service import LLMService
from app.services.provider_service import get_active_provider_row


class NoProviderError(ValueError):
    pass


async def verify_invoice(db, invoice):
    """Save the latest verdict without committing the caller's transaction."""
    provider = await get_active_provider_row(db)
    service = LLMService(provider)
    if not service.is_available:
        raise NoProviderError("未配置可用的LLM服务，请先在设置页配置并启用")
    ocr = (await db.execute(select(OcrResult).where(OcrResult.invoice_id == invoice.id))).scalar_one_or_none()
    values = {}
    for name in COMPARABLE_FIELDS:
        value = getattr(invoice, name)
        values[name] = value.isoformat() if hasattr(value, "isoformat") else str(value) if value is not None else None
    outcome = await asyncio.get_running_loop().run_in_executor(
        _llm_executor, run_ai_verify, service, values,
        ocr.raw_text if ocr else None, invoice.file_data, invoice.file_type,
    )
    verdict = outcome["verdict"]
    await db.execute(delete(AiVerification).where(AiVerification.invoice_id == invoice.id))
    verification = AiVerification(
        invoice_id=invoice.id, overall=verdict["overall"], summary=verdict["summary"],
        fields=verdict["fields"], mode=outcome["mode"],
        provider_name=provider.display_name or provider.name, model=provider.model,
        raw_response=outcome["content"],
    )
    db.add(verification)
    return {**outcome, "verification": verification}
