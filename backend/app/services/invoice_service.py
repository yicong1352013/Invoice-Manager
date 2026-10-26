"""Invoice processing service: OCR-first pipeline with LLM fallback."""

import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.invoice import Invoice, OcrResult, LlmResult, ParsingDiff, InvoiceStatus
from app.services.ocr_service import get_ocr_service, get_field_extractor, is_valid_item_name
from app.services.llm_service import LLMService
from app.services.provider_service import get_active_provider_row
from app.services.prompts import (
    INVOICE_VERIFY_OCR_SECTION,
    INVOICE_VERIFY_PROMPT,
    INVOICE_VERIFY_SYSTEM_PROMPT,
)
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Separate thread pools for OCR and LLM to maximize throughput
# OCR is CPU-bound (image processing), LLM is I/O-bound (API calls)
# Using separate pools prevents one from blocking the other
_ocr_executor = ThreadPoolExecutor(max_workers=settings.ocr_max_workers)
_llm_executor = ThreadPoolExecutor(max_workers=settings.llm_max_workers)
logger.info(f"Initialized thread pools: OCR={settings.ocr_max_workers}, LLM={settings.llm_max_workers}")

# Fields to compare between OCR and LLM. Also the AI校验 verified set:
# ai-verify builds current_values from this list and parse_verify_response
# only accepts these names — keep prompts.VERIFY_FIELD_LABELS in sync.
# Order drives the diff/verify display order.
COMPARABLE_FIELDS = [
    'invoice_number',
    'invoice_type',
    'invoice_code',
    'issue_date',
    'buyer_name',
    'buyer_tax_id',
    'buyer_address_phone',
    'buyer_bank_account',
    'seller_name',
    'seller_tax_id',
    'seller_address_phone',
    'seller_bank_account',
    'item_name',
    'specification',
    'unit',
    'quantity',
    'unit_price',
    'total_with_tax',
    'total_with_tax_cn',
    'amount',
    'tax_amount',
    'tax_rate',
    'remark',
    'drawer',
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
    engine: str                                   # 'local' | 'paddle_remote' | 'baidu'
    raw_text: str
    confidence: Optional[float]                   # None when engine gives no score
    fields: Dict[str, Any] = field(default_factory=dict)
    raw_response: Optional[str] = None            # engine's verbatim output (debug/audit)


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
    paddle_endpoint: Optional[str] = None,
    paddle_api_key: Optional[str] = None,
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

    from app.services.ocr_service import RemotePaddleOCRService
    if paddle_endpoint:
        # custom PaddleOCR HTTP service instead of the in-process engine
        ocr_service = RemotePaddleOCRService(paddle_endpoint, api_key=paddle_api_key)
        engine_name = "paddle_remote"
    else:
        ocr_service = get_ocr_service()
        engine_name = "local"
    extractor = get_field_extractor()
    if file_type == 'pdf':
        raw_text, confidence, ocr_lines, raw_response = ocr_service.process_pdf(file_data)
    else:
        raw_text, confidence, ocr_lines, raw_response = ocr_service.process_image(file_data)
    fields = extractor.extract_fields(raw_text, ocr_lines)
    if file_type == 'pdf' and paddle_endpoint:
        # The embedded header can supply letters missed by OCR without replacing its audit evidence.
        pdf_text, _, _, _ = ocr_service._extract_pdf_text_layer(file_data)
        title = extractor.extract_fields('\n'.join(pdf_text.splitlines()[:4])).get('invoice_type')
        if title:
            fields['invoice_type'] = title
    return OcrOutcome(engine=engine_name, raw_text=raw_text, confidence=confidence,
                      fields=fields, raw_response=raw_response)


def _fields_needing_review(fields: Dict[str, Any], raw_text: str) -> List[str]:
    review_fields = [name for name in CRITICAL_FIELDS if not str(fields.get(name) or '').strip()]
    if fields.get('item_name') and not is_valid_item_name(fields['item_name']):
        review_fields.append('item_name')
    if re.search(r'税\s*率|征收率', raw_text) and not str(fields.get('tax_rate') or '').strip():
        review_fields.append('tax_rate')
    return review_fields


def _ocr_sufficient(outcome: OcrOutcome, accept_confidence: float) -> bool:
    """OCR-first gate: complete critical fields + acceptable confidence.

    Engines without a confidence score (Baidu structured API) are judged by
    field completeness alone.
    """
    if _fields_needing_review(outcome.fields, outcome.raw_text):
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


def _pdf_first_page_png(file_data: bytes) -> Optional[bytes]:
    """Render the first PDF page to PNG bytes (requires poppler); None on failure."""
    try:
        from pdf2image import convert_from_bytes
        from io import BytesIO

        # Use higher DPI (300) for better text recognition in PDFs
        # Some PDFs have text rendered as graphics which need higher resolution
        images = convert_from_bytes(file_data, dpi=300, first_page=1, last_page=1)
        if not images:
            return None
        buffer = BytesIO()
        images[0].save(buffer, format='PNG', optimize=False)
        return buffer.getvalue()
    except Exception as e:
        logger.error(f"PDF to image conversion failed: {e}")
        return None


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
        png = _pdf_first_page_png(file_data)
        if png is None:
            logger.warning("Failed to convert PDF to image for LLM vision")
            return {}
        file_data = png
        mime_type = 'image/png'
        logger.info(f"PDF converted to image for LLM vision: {len(file_data)} bytes")

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
        SETTING_PADDLE_OCR_API_KEY,
        SETTING_PADDLE_OCR_ENDPOINT,
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
        paddle_endpoint = await get_setting(db, SETTING_PADDLE_OCR_ENDPOINT)
        paddle_api_key = await get_setting(db, SETTING_PADDLE_OCR_API_KEY)
        llm_service = LLMService(await get_active_provider_row(db))

        loop = asyncio.get_running_loop()

        # Step 1: OCR (CPU-bound pool)
        outcome = await loop.run_in_executor(
            _ocr_executor, _run_ocr, engine, baidu_key, baidu_secret,
            invoice.file_data, invoice.file_type, paddle_endpoint, paddle_api_key,
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

        # Save OCR result (all comparable fields exist as string columns)
        ocr_result = OcrResult(
            invoice_id=invoice_id,
            raw_text=outcome.raw_text,
            raw_response=outcome.raw_response,
            **{name: outcome.fields.get(name) for name in COMPARABLE_FIELDS},
        )
        db.add(ocr_result)

        # Save LLM result if available
        if has_llm:
            llm_result = LlmResult(
                invoice_id=invoice_id,
                **{name: llm_fields.get(name) for name in COMPARABLE_FIELDS},
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
        review_fields = _fields_needing_review(final_fields, outcome.raw_text)
        if review_fields:
            logger.warning(f"Invoice {invoice_id} fields requiring review: {review_fields}")

        needs_review = has_conflicts or bool(review_fields)
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
NUMERIC_FIELDS = ['total_with_tax', 'amount', 'tax_amount', 'quantity', 'unit_price']


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
    """Update invoice fields from extracted data (values are strings or None)."""
    from datetime import datetime
    from decimal import Decimal

    if fields.get('issue_date'):
        try:
            invoice.issue_date = datetime.strptime(fields['issue_date'], '%Y-%m-%d').date()
        except ValueError:
            pass

    # String columns shared by the header/item fields
    for name in (
        'invoice_number', 'invoice_type', 'invoice_code',
        'buyer_name', 'buyer_tax_id', 'buyer_address_phone', 'buyer_bank_account',
        'seller_name', 'seller_tax_id', 'seller_address_phone', 'seller_bank_account',
        'item_name', 'specification', 'unit', 'total_with_tax_cn',
        'tax_rate', 'remark', 'drawer',
    ):
        if fields.get(name):
            setattr(invoice, name, fields[name])

    # Decimal columns
    for name in ('total_with_tax', 'amount', 'tax_amount', 'quantity', 'unit_price'):
        if fields.get(name):
            try:
                value = Decimal(str(fields[name]).replace(',', '').replace('，', ''))
                if not value.is_finite():
                    continue
                if name in ('quantity', 'unit_price') and (
                    abs(value) >= Decimal('100000000') or value != value.quantize(Decimal('0.0001'))
                ):
                    continue
                setattr(invoice, name, value)
            except (InvalidOperation, ValueError, TypeError):
                pass


# --- AI verification (detail-page "AI校验") -----------------------------------

_VERIFY_STATUSES = {"correct", "incorrect", "unsure"}
_VERIFY_WARNING_FIELDS = {
    "buyer_address_phone", "buyer_bank_account", "seller_address_phone", "seller_bank_account",
    "specification", "unit", "quantity", "unit_price", "total_with_tax_cn", "remark", "drawer",
}
_VERIFY_CONDITIONAL_FIELDS = {"invoice_type", "invoice_code"}
_VERIFY_NUMERIC_FIELDS = {"quantity", "unit_price", "amount", "tax_amount", "total_with_tax"}
_VERIFY_WIDTH_TRANSLATION = {0x3000: 0x20, **{code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}}


def _verify_values_equivalent(name: str, current: Any, corrected: str) -> bool:
    if current is None:
        return False
    left = str(current).translate(_VERIFY_WIDTH_TRANSLATION).strip()
    right = corrected.translate(_VERIFY_WIDTH_TRANSLATION).strip()
    if not left or not right:
        return False
    if name in _VERIFY_NUMERIC_FIELDS:
        try:
            a, b = Decimal(left), Decimal(right)
            return a.is_finite() and b.is_finite() and a == b
        except InvalidOperation:
            return False
    if name == "issue_date":
        try:
            return date.fromisoformat(left) == date.fromisoformat(right)
        except ValueError:
            return False
    if name == "item_name":
        left, right = left.removeprefix("*").strip(), right.removeprefix("*").strip()
    return left == right


def build_verify_prompt(current_values: Dict[str, Any], ocr_text: Optional[str]) -> str:
    """Assemble the verification prompt with current values (+ optional OCR text)."""
    values_json = json.dumps(current_values, ensure_ascii=False, indent=2)
    ocr_section = ""
    if ocr_text and ocr_text.strip():
        ocr_section = INVOICE_VERIFY_OCR_SECTION.replace("{ocr_text}", ocr_text.strip())
    return (
        INVOICE_VERIFY_PROMPT
        .replace("{current_values}", values_json)
        .replace("{ocr_section}", ocr_section)
    )


def parse_verify_response(content: str, current_values: Dict[str, Any]) -> Dict[str, Any]:
    """Parse + normalize the LLM verification verdict.

    Invariants:
    - Only known field names (COMPARABLE_FIELDS) survive; unknown ones are dropped.
    - Every comparable field gets a verdict; missing entries become 'unsure'.
    - status is normalized to correct/incorrect/unsure; anything else is 'unsure'.
    - corrected_value is kept only for 'incorrect' verdicts with a non-empty value.
    - Severity and overall are computed locally; auxiliary differences only warn.
    """
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("AI校验结果不是JSON对象")
    raw_fields = data.get("fields")
    if not isinstance(raw_fields, list):
        raise ValueError("AI校验结果缺少fields数组")

    by_field: Dict[str, Dict[str, Any]] = {}
    for item in raw_fields:
        if not isinstance(item, dict):
            continue
        name = str(item.get("field") or "").strip()
        if name not in COMPARABLE_FIELDS:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in _VERIFY_STATUSES:
            status = "unsure"
        corrected = item.get("corrected_value")
        corrected = str(corrected).strip() if corrected is not None else None
        if status != "incorrect" or not corrected:
            corrected = None
        by_field[name] = {
            "field": name,
            "status": status,
            "corrected_value": corrected,
            "reason": str(item.get("reason") or "").strip() or None,
        }

    fields = [
        by_field.get(name, {"field": name, "status": "unsure", "corrected_value": None, "reason": None})
        for name in COMPARABLE_FIELDS
    ]
    for item in fields:
        name, status = item["field"], item["status"]
        if status == "incorrect" and item["corrected_value"] is not None and _verify_values_equivalent(
            name, current_values.get(name), item["corrected_value"]
        ):
            item.update(status="correct", corrected_value=None, reason="仅格式差异，与当前值等价")
            status = "correct"
        if status == "correct":
            item["severity"] = "none"
        elif name in _VERIFY_WARNING_FIELDS or (
            name in _VERIFY_CONDITIONAL_FIELDS and (
                status == "unsure" or not str(current_values.get(name) or "").strip()
            )
        ):
            item["severity"] = "warning"
        else:
            item["severity"] = "blocking"
    blocking = sum(item["severity"] == "blocking" for item in fields)
    warnings = sum(item["severity"] == "warning" for item in fields)
    overall = "issues" if blocking else "pass"
    if blocking:
        summary = f"需处理 {blocking} 个关键问题" + (f"，另有 {warnings} 项提示" if warnings else "")
    else:
        summary = f"审核通过，含 {warnings} 项提示" if warnings else "审核通过，各字段与原件一致"
    return {
        "overall": overall,
        "fields": fields,
        "summary": summary,
    }


def run_ai_verify(
    llm_service: LLMService,
    current_values: Dict[str, Any],
    ocr_text: Optional[str],
    file_data: bytes,
    file_type: str,
) -> Dict[str, Any]:
    """One AI verification run (worker thread).

    Vision-capable providers read the original image (PDFs need a poppler
    render; when unavailable the call degrades to the OCR text). Returns
    {"mode", "content", "verdict"}; raises ValueError when there is nothing
    to verify from and RuntimeError when the LLM call or parsing fails.
    """
    mime_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png'}
    image_data: Optional[bytes] = None
    mime_type = 'image/png'

    if llm_service.supports_vision():
        if file_type in mime_map:
            image_data = file_data
            mime_type = mime_map[file_type]
        elif file_type == 'pdf':
            png = _pdf_first_page_png(file_data)
            if png is not None:
                image_data = png
            else:
                logger.warning("AI verify: PDF→image conversion unavailable, falling back to OCR text")

    if image_data is None and not (ocr_text and ocr_text.strip()):
        raise ValueError("无原件图片且无OCR识别文本，无法进行AI校验")

    prompt = build_verify_prompt(current_values, ocr_text)
    try:
        content = llm_service.verify_completion(
            INVOICE_VERIFY_SYSTEM_PROMPT, prompt, image_data, mime_type)
    except Exception as e:
        raise RuntimeError(f"LLM调用失败: {e}") from e

    try:
        verdict = parse_verify_response(content, current_values)
    except ValueError as e:
        raise RuntimeError(f"AI返回内容无法解析: {e}") from e

    return {
        "mode": "vision" if image_data is not None else "text",
        "content": content,
        "verdict": verdict,
    }
