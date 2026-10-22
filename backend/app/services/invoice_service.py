"""Invoice processing service: OCR-first pipeline with LLM fallback."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.invoice import Invoice, OcrResult, LlmResult, ParsingDiff, InvoiceStatus
from app.services.ocr_service import get_ocr_service, get_field_extractor
from app.services.llm_service import LLMService
from app.services.provider_service import get_active_provider_row
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Separate thread pools for OCR and LLM to maximize throughput
# OCR is CPU-bound (image processing), LLM is I/O-bound (API calls)
# Using separate pools prevents one from blocking the other
_ocr_executor = ThreadPoolExecutor(max_workers=settings.ocr_max_workers)
_llm_executor = ThreadPoolExecutor(max_workers=settings.llm_max_workers)
logger.info(f"Initialized thread pools: OCR={settings.ocr_max_workers}, LLM={settings.llm_max_workers}")

# Fields to compare between OCR and LLM
COMPARABLE_FIELDS = [
    'invoice_number',
    'issue_date',
    'buyer_name',
    'buyer_tax_id',
    'seller_name',
    'seller_tax_id',
    'item_name',
    'total_with_tax',
    'amount',
    'tax_amount',
    'tax_rate',
]

# Fields that must all be present for the OCR result to count as sufficient
# (same set that drives auto-confirm, so a sufficient OCR result can go
# straight to CONFIRMED without paying for an LLM call)
CRITICAL_FIELDS = [
    'invoice_number',
    'issue_date',
    'total_with_tax',
    'buyer_name',
    'buyer_tax_id',
    'seller_name',
    'seller_tax_id',
    'item_name',
]


@dataclass
class OcrOutcome:
    """Normalized result of one OCR engine run."""
    engine: str                                   # 'local' | 'baidu'
    raw_text: str
    confidence: Optional[float]                   # None when engine gives no score
    fields: Dict[str, Any] = field(default_factory=dict)


def _reset_extracted_fields(invoice: Invoice) -> None:
    """Reset extracted fields to avoid stale values on reprocess."""
    for field_name in COMPARABLE_FIELDS:
        setattr(invoice, field_name, None)


def _run_ocr(
    engine: str,
    baidu_api_key: Optional[str],
    baidu_secret_key: Optional[str],
    file_data: bytes,
    file_type: str,
) -> OcrOutcome:
    """Run the selected OCR engine in a worker thread."""
    if engine == "baidu":
        if not baidu_api_key or not baidu_secret_key:
            raise RuntimeError("百度云 OCR 凭据未配置，请先在设置页填写")
        from app.services.baidu_ocr_service import BaiduCloudOCRService
        service = BaiduCloudOCRService(baidu_api_key, baidu_secret_key)
        if file_type == 'pdf':
            raw_text, fields = service.process_pdf(file_data)
        else:
            raw_text, fields = service.process_image(file_data)
        return OcrOutcome(engine="baidu", raw_text=raw_text, confidence=None, fields=fields)

    ocr_service = get_ocr_service()
    extractor = get_field_extractor()
    if file_type == 'pdf':
        raw_text, confidence, ocr_lines = ocr_service.process_pdf(file_data)
    else:
        raw_text, confidence, ocr_lines = ocr_service.process_image(file_data)
    fields = extractor.extract_fields(raw_text, ocr_lines)
    return OcrOutcome(engine="local", raw_text=raw_text, confidence=confidence, fields=fields)


def _ocr_sufficient(outcome: OcrOutcome, accept_confidence: float) -> bool:
    """OCR-first gate: complete critical fields + acceptable confidence.

    Engines without a confidence score (Baidu structured API) are judged by
    field completeness alone.
    """
    if any(not outcome.fields.get(name) for name in CRITICAL_FIELDS):
        return False
    if outcome.confidence is None:
        return True
    return outcome.confidence >= accept_confidence


def _run_llm_fallback(
    llm_service: LLMService,
    ocr_raw_text: str,
    file_data: bytes,
    file_type: str,
) -> Dict[str, Any]:
    """LLM fallback when OCR is insufficient.

    Vision-capable providers read the image directly; text-only providers
    (e.g. cheap domestic chat models) parse the OCR raw text instead.
    """
    if not llm_service.is_available:
        return {}

    if llm_service.supports_vision():
        return _run_llm_vision(llm_service, file_data, file_type)

    return llm_service.parse_invoice_from_text(ocr_raw_text)


def _run_llm_vision(llm_service: LLMService, file_data: bytes, file_type: str) -> Dict[str, Any]:
    """Run LLM vision parsing (image input) in a worker thread."""
    if not llm_service.is_available or not llm_service.supports_vision():
        return {}

    # Determine MIME type
    mime_map = {
        'pdf': 'application/pdf',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png',
    }
    mime_type = mime_map.get(file_type, 'image/png')

    # For PDF, we need to convert first page to image
    if file_type == 'pdf':
        try:
            from pdf2image import convert_from_bytes
            from io import BytesIO

            # Use higher DPI (300) for better text recognition in PDFs
            # Some PDFs have text rendered as graphics which need higher resolution
            images = convert_from_bytes(file_data, dpi=300, first_page=1, last_page=1)
            if images:
                # Convert PIL Image to bytes with high quality
                buffer = BytesIO()
                images[0].save(buffer, format='PNG', optimize=False)
                file_data = buffer.getvalue()
                mime_type = 'image/png'
                logger.info(f"PDF converted to image: {images[0].size[0]}x{images[0].size[1]} pixels, {len(file_data)} bytes")
            else:
                logger.warning("Failed to convert PDF to image for LLM vision")
                return {}
        except Exception as e:
            logger.error(f"PDF to image conversion failed: {e}")
            return {}

    return llm_service.parse_invoice_from_image(file_data, mime_type)


def _has_meaningful_fields(fields: Dict[str, Any]) -> bool:
    """Check if parsed fields contain any meaningful values."""
    if not fields:
        return False
    for value in fields.values():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


async def process_invoice(invoice_id: int, db: AsyncSession) -> bool:
    """Process an invoice with the OCR-first pipeline.

    1. Run the selected OCR engine (local PaddleOCR or Baidu Cloud).
    2. If the OCR result is sufficient (all critical fields + confidence),
       finish without any LLM call (zero API cost).
    3. Otherwise fall back to the LLM: vision models read the image directly,
       text-only models parse the OCR raw text.
    4. When both sources produced results, compare them; conflicts or missing
       critical fields send the invoice to REVIEWING (unchanged semantics).

    Args:
        invoice_id: ID of the invoice to process
        db: Database session

    Returns:
        True if processing succeeded, False otherwise
    """
    from app.models.provider import (
        SETTING_BAIDU_OCR_API_KEY,
        SETTING_BAIDU_OCR_SECRET_KEY,
        SETTING_OCR_ENGINE,
    )
    from app.services.provider_service import get_setting

    try:
        # Get invoice
        query = select(Invoice).where(Invoice.id == invoice_id)
        result = await db.execute(query)
        invoice = result.scalar_one_or_none()

        if not invoice:
            logger.error(f"Invoice {invoice_id} not found")
            return False

        # Delete existing OCR, LLM, and diff results for reprocessing
        await db.execute(delete(ParsingDiff).where(ParsingDiff.invoice_id == invoice_id))
        await db.execute(delete(LlmResult).where(LlmResult.invoice_id == invoice_id))
        await db.execute(delete(OcrResult).where(OcrResult.invoice_id == invoice_id))
        logger.info(f"Cleared existing processing results for invoice {invoice_id}")

        # Resolve engine + LLM service from DB settings before hitting threads
        engine = await get_setting(db, SETTING_OCR_ENGINE, "local") or "local"
        baidu_key = await get_setting(db, SETTING_BAIDU_OCR_API_KEY)
        baidu_secret = await get_setting(db, SETTING_BAIDU_OCR_SECRET_KEY)
        llm_service = LLMService(await get_active_provider_row(db))

        loop = asyncio.get_running_loop()

        # Step 1: OCR (CPU-bound pool)
        outcome = await loop.run_in_executor(
            _ocr_executor, _run_ocr, engine, baidu_key, baidu_secret,
            invoice.file_data, invoice.file_type,
        )
        logger.info(
            f"OCR ({outcome.engine}) completed: {len(outcome.fields)} fields, "
            f"confidence={outcome.confidence}"
        )

        # Step 2: LLM fallback only when OCR is insufficient (I/O-bound pool)
        llm_fields: Dict[str, Any] = {}
        if _ocr_sufficient(outcome, settings.ocr_accept_confidence):
            logger.info(f"Invoice {invoice_id}: OCR sufficient, skipping LLM (zero API cost)")
        elif llm_service.is_available:
            llm_fields = await loop.run_in_executor(
                _llm_executor, _run_llm_fallback, llm_service,
                outcome.raw_text, invoice.file_data, invoice.file_type,
            )
        else:
            logger.info(f"Invoice {invoice_id}: OCR insufficient and no LLM configured")

        has_llm = _has_meaningful_fields(llm_fields)
        logger.info(f"LLM fallback completed (has_llm={has_llm})")

        # Save OCR result
        ocr_result = OcrResult(
            invoice_id=invoice_id,
            raw_text=outcome.raw_text,
            invoice_number=outcome.fields.get('invoice_number'),
            issue_date=outcome.fields.get('issue_date'),
            buyer_name=outcome.fields.get('buyer_name'),
            buyer_tax_id=outcome.fields.get('buyer_tax_id'),
            seller_name=outcome.fields.get('seller_name'),
            seller_tax_id=outcome.fields.get('seller_tax_id'),
            item_name=outcome.fields.get('item_name'),
            total_with_tax=outcome.fields.get('total_with_tax'),
            amount=outcome.fields.get('amount'),
            tax_amount=outcome.fields.get('tax_amount'),
            tax_rate=outcome.fields.get('tax_rate'),
        )
        db.add(ocr_result)

        # Save LLM result if available
        if has_llm:
            llm_result = LlmResult(
                invoice_id=invoice_id,
                invoice_number=llm_fields.get('invoice_number'),
                issue_date=llm_fields.get('issue_date'),
                buyer_name=llm_fields.get('buyer_name'),
                buyer_tax_id=llm_fields.get('buyer_tax_id'),
                seller_name=llm_fields.get('seller_name'),
                seller_tax_id=llm_fields.get('seller_tax_id'),
                item_name=llm_fields.get('item_name'),
                total_with_tax=llm_fields.get('total_with_tax'),
                amount=llm_fields.get('amount'),
                tax_amount=llm_fields.get('tax_amount'),
                tax_rate=llm_fields.get('tax_rate'),
            )
            db.add(llm_result)

        # Compare OCR and LLM results, create diffs
        final_fields, diffs = _compare_and_resolve(outcome.fields, llm_fields, has_llm)

        # Clear extracted fields so missing values don't keep stale data
        _reset_extracted_fields(invoice)

        # Save parsing diffs
        for diff in diffs:
            parsing_diff = ParsingDiff(
                invoice_id=invoice_id,
                field_name=diff['field_name'],
                ocr_value=diff['ocr_value'],
                llm_value=diff['llm_value'],
                final_value=diff['final_value'],
                source=diff['source'],
                resolved=0 if diff['needs_review'] else 1,
            )
            db.add(parsing_diff)

        # Update invoice with final data
        _update_invoice_from_fields(invoice, final_fields)

        # Set status based on whether review is needed (unchanged semantics)
        has_conflicts = any(d['needs_review'] for d in diffs)
        missing_fields = [f for f in CRITICAL_FIELDS if not final_fields.get(f)]
        missing_critical = bool(missing_fields)
        if missing_critical:
            logger.warning(f"Invoice {invoice_id} missing critical fields: {missing_fields}")

        needs_review = has_conflicts or missing_critical
        if needs_review:
            invoice.status = InvoiceStatus.REVIEWING
        else:
            invoice.status = InvoiceStatus.CONFIRMED

        await db.commit()
        logger.info(f"Invoice {invoice_id} processed successfully (needs_review={needs_review})")
        return True

    except Exception as e:
        logger.error(f"Failed to process invoice {invoice_id}: {e}")
        await db.rollback()
        return False


def _compare_and_resolve(
    ocr_fields: Dict[str, Any],
    llm_fields: Dict[str, Any],
    has_llm: bool
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Compare OCR and LLM results, resolve differences.

    Args:
        ocr_fields: Fields extracted by OCR
        llm_fields: Fields extracted by LLM
        has_llm: Whether LLM results are available

    Returns:
        Tuple of (final_fields, diffs_list)
    """
    final_fields = {}
    diffs = []

    for field_name in COMPARABLE_FIELDS:
        ocr_value = _normalize_value(ocr_fields.get(field_name))
        llm_value = _normalize_value(llm_fields.get(field_name)) if has_llm else None

        # Determine final value and whether review is needed
        if not has_llm:
            # No LLM available, use OCR value directly
            final_value = ocr_value
            source = 'ocr'
            needs_review = False
        elif _values_are_equal(field_name, ocr_value, llm_value):
            # Values match (including numeric equivalence like 330.00 == 330)
            final_value = ocr_value or llm_value
            source = 'matched'
            needs_review = False
        elif ocr_value and llm_value:
            # Both have values but they differ - needs manual review
            final_value = None  # Leave blank for manual review
            source = 'conflict'
            needs_review = True
        elif llm_value and not ocr_value:
            # LLM found something OCR missed - prefer LLM
            final_value = llm_value
            source = 'llm'
            needs_review = False
        else:
            # OCR found something LLM missed - prefer OCR
            final_value = ocr_value
            source = 'ocr'
            needs_review = False

        final_fields[field_name] = final_value

        # Record diff if there's a discrepancy or both have values
        if has_llm and (ocr_value or llm_value):
            diffs.append({
                'field_name': field_name,
                'ocr_value': ocr_value,
                'llm_value': llm_value,
                'final_value': final_value,
                'source': source,
                'needs_review': needs_review,
            })

    return final_fields, diffs


def _normalize_value(value: Any) -> Optional[str]:
    """Normalize a value to string for comparison."""
    if value is None:
        return None
    value_str = str(value).strip()
    if not value_str:
        return None
    return value_str


# Fields that should be compared as numbers
NUMERIC_FIELDS = ['total_with_tax', 'amount', 'tax_amount']


def _values_are_equal(field_name: str, value1: Optional[str], value2: Optional[str]) -> bool:
    """Compare two values, using numeric comparison for numeric fields.

    Args:
        field_name: Name of the field being compared
        value1: First value (normalized string)
        value2: Second value (normalized string)

    Returns:
        True if values are considered equal
    """
    # If both are None or empty, they're equal
    if not value1 and not value2:
        return True

    # If only one is None/empty, they're not equal
    if not value1 or not value2:
        return False

    # For numeric fields, compare as numbers
    if field_name in NUMERIC_FIELDS:
        try:
            from decimal import Decimal, InvalidOperation
            # Remove any currency symbols and whitespace
            clean1 = value1.replace('¥', '').replace('￥', '').replace(',', '').strip()
            clean2 = value2.replace('¥', '').replace('￥', '').replace(',', '').strip()
            num1 = Decimal(clean1)
            num2 = Decimal(clean2)
            return num1 == num2
        except (InvalidOperation, ValueError):
            # Fall back to string comparison if not valid numbers
            pass

    # Default to string comparison
    return value1 == value2


def _update_invoice_from_fields(invoice: Invoice, fields: dict) -> None:
    """Update invoice fields from extracted data."""
    from datetime import datetime
    from decimal import Decimal

    if fields.get('invoice_number'):
        invoice.invoice_number = fields['invoice_number']

    if fields.get('issue_date'):
        try:
            invoice.issue_date = datetime.strptime(fields['issue_date'], '%Y-%m-%d').date()
        except ValueError:
            pass

    if fields.get('buyer_name'):
        invoice.buyer_name = fields['buyer_name']

    if fields.get('buyer_tax_id'):
        invoice.buyer_tax_id = fields['buyer_tax_id']

    if fields.get('seller_name'):
        invoice.seller_name = fields['seller_name']

    if fields.get('seller_tax_id'):
        invoice.seller_tax_id = fields['seller_tax_id']

    if fields.get('item_name'):
        invoice.item_name = fields['item_name']

    if fields.get('total_with_tax'):
        try:
            invoice.total_with_tax = Decimal(fields['total_with_tax'])
        except (ValueError, TypeError):
            pass

    if fields.get('amount'):
        try:
            invoice.amount = Decimal(fields['amount'])
        except (ValueError, TypeError):
            pass

    if fields.get('tax_amount'):
        try:
            invoice.tax_amount = Decimal(fields['tax_amount'])
        except (ValueError, TypeError):
            pass

    if fields.get('tax_rate'):
        invoice.tax_rate = fields['tax_rate']
