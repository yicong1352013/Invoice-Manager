"""Baidu Cloud VAT-invoice OCR engine (百度智能云 · 增值税发票识别).

Uses the official REST API:
    POST https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice
Authenticated with an access token obtained from AK/SK (client credentials),
cached until shortly before expiry. Structured fields come back directly —
no local regex extraction needed.
"""

import base64
import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
VAT_INVOICE_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice"

# Baidu word-result key -> our field name (unmapped keys stay in raw text only)
_FIELD_MAP = {
    "InvoiceNum": "invoice_number",
    "InvoiceDate": "issue_date",
    "SellerName": "seller_name",
    "SellerRegisterNum": "seller_tax_id",
    "PurchaserName": "buyer_name",
    "PurchaserRegisterNum": "buyer_tax_id",
    "AmountInFiguers": "total_with_tax",      # 价税合计(小写)
    "TotalAmount": "amount",                  # 合计金额(不含税)
    "TotalTax": "tax_amount",                 # 合计税额
    "TaxRate": "tax_rate",
}

# Fields we surface from the structured response
_EXTRACT_FIELDS = [
    "invoice_number", "issue_date", "buyer_name", "buyer_tax_id",
    "seller_name", "seller_tax_id", "total_with_tax", "amount",
    "tax_amount", "tax_rate", "item_name",
]


def _normalize_date(value: str) -> Optional[str]:
    """Baidu returns dates like '2024年05月17日' or '2024-05-17'."""
    if not value:
        return None
    match = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})", value)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return value or None


def _normalize_amount(value: str) -> Optional[str]:
    if not value:
        return None
    cleaned = re.sub(r"[¥￥$€,，\s]", "", str(value))
    match = re.search(r"(\d+(?:\.\d{1,2})?)", cleaned)
    return match.group(1) if match else None


def map_vat_invoice_fields(words_result: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Map the vat_invoice words_result to our field names (pure function)."""
    fields: Dict[str, Optional[str]] = {name: None for name in _EXTRACT_FIELDS}

    for baidu_key, our_key in _FIELD_MAP.items():
        value = words_result.get(baidu_key)
        if value is None or value == "":
            continue
        if isinstance(value, (list, dict)):
            continue  # commodity arrays handled separately
        value = str(value).strip()
        if our_key == "issue_date":
            value = _normalize_date(value)
        elif our_key in ("total_with_tax", "amount", "tax_amount"):
            value = _normalize_amount(value)
        elif our_key in ("buyer_tax_id", "seller_tax_id"):
            value = re.sub(r"\s+", "", value.upper())
        if value:
            fields[our_key] = value

    # Commodity name: vat_invoice returns it as a list of row dicts/strings
    commodity = words_result.get("CommodityName")
    if isinstance(commodity, list) and commodity:
        first = commodity[0]
        if isinstance(first, dict):
            name = first.get("word") or first.get("name") or None
        else:
            name = str(first)
        if name:
            fields["item_name"] = re.sub(r"^\*[^*]+\*", "", name).strip() or name
    elif isinstance(commodity, str) and commodity.strip():
        fields["item_name"] = re.sub(r"^\*[^*]+\*", "", commodity).strip()

    return fields


class BaiduCloudOCRService:
    """Thin client for the Baidu Cloud VAT-invoice OCR API."""

    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def _get_access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token
        response = httpx.post(
            TOKEN_URL,
            params={
                "grant_type": "client_credentials",
                "client_id": self.api_key,
                "client_secret": self.secret_key,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if "access_token" not in data:
            raise RuntimeError(f"百度云令牌获取失败: {data.get('error_description', data)}")
        self._token = data["access_token"]
        # Refresh a day early to avoid edge expiry
        self._token_expires_at = time.time() + float(data.get("expires_in", 2592000)) - 86400
        return self._token

    def _recognize_image(self, image_bytes: bytes) -> Dict[str, Any]:
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        response = httpx.post(
            VAT_INVOICE_URL,
            params={"access_token": self._get_access_token()},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"image": encoded},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if "words_result" not in data:
            error_msg = data.get("error_msg") or str(data)[:200]
            raise RuntimeError(f"百度云发票识别失败: {error_msg}")
        return data

    def process_image(self, image_data: bytes) -> Tuple[str, Dict[str, Optional[str]]]:
        """Recognize an invoice image.

        Returns (raw_text, fields). Confidence is None: the API returns
        structured fields without per-field confidence, so sufficiency is
        judged by field completeness upstream.
        """
        data = self._recognize_image(image_data)
        words_result: Dict[str, Any] = data.get("words_result", {})
        fields = map_vat_invoice_fields(words_result)

        raw_parts = []
        for value in words_result.values():
            if isinstance(value, str) and value.strip():
                raw_parts.append(value.strip())
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        word = item.get("word")
                        if word:
                            raw_parts.append(str(word))
                    elif isinstance(item, str):
                        raw_parts.append(item)
        raw_text = "\n".join(raw_parts)
        logger.info(f"Baidu OCR extracted fields: {[k for k, v in fields.items() if v]}")
        return raw_text, fields

    def process_pdf(self, pdf_data: bytes) -> Tuple[str, Dict[str, Optional[str]]]:
        """Recognize a PDF invoice by rasterizing its first page (300 DPI)."""
        from pdf2image import convert_from_bytes
        from io import BytesIO

        images = convert_from_bytes(pdf_data, dpi=300, first_page=1, last_page=1)
        if not images:
            raise RuntimeError("PDF 转图片失败，无法调用百度云识别")
        buffer = BytesIO()
        images[0].save(buffer, format="PNG")
        return self.process_image(buffer.getvalue())
