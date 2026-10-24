"""Tests for the OCR-first pipeline decision logic.

Business invariants:
- A sufficient OCR result (all critical fields + confidence) must NEVER
  trigger a paid LLM call.
- An insufficient OCR result must trigger the LLM fallback whenever one is
  configured; vision models read the image, text-only models parse OCR text.
- Baidu vat_invoice field mapping must land in our canonical field names.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio
import pytest

from app.services.baidu_ocr_service import map_vat_invoice_fields
from app.services.invoice_service import (
    CRITICAL_FIELDS,
    OcrOutcome,
    _ocr_sufficient,
    _run_llm_fallback,
)


def _complete_fields(**overrides):
    fields = {
        'invoice_number': '25952000000233223073',
        'issue_date': '2025-11-05',
        'total_with_tax': '8600.00',
        'buyer_name': '示例买方科技有限公司',
        'buyer_tax_id': '123456789012345',
        'seller_name': '示例卖方服务有限公司',
        'seller_tax_id': 'ABCD123456789012',
        'item_name': '服务费',
        'amount': '8600.00',
        'tax_amount': '0',
        'tax_rate': '免税',
    }
    fields.update(overrides)
    return fields


# --- sufficiency gate --------------------------------------------------------


def test_complete_confident_ocr_is_sufficient():
    outcome = OcrOutcome(engine='local', raw_text='...', confidence=90.0,
                         fields=_complete_fields())
    assert _ocr_sufficient(outcome, accept_confidence=85.0)


def test_low_confidence_ocr_is_insufficient_even_when_complete():
    outcome = OcrOutcome(engine='local', raw_text='...', confidence=70.0,
                         fields=_complete_fields())
    assert not _ocr_sufficient(outcome, accept_confidence=85.0)


def test_missing_critical_field_is_insufficient_even_with_high_confidence():
    fields = _complete_fields()
    fields['invoice_number'] = None
    outcome = OcrOutcome(engine='local', raw_text='...', confidence=99.0, fields=fields)
    assert not _ocr_sufficient(outcome, accept_confidence=85.0)


def test_baidu_engine_judged_by_completeness_only():
    """Baidu returns structured fields without confidence; completeness rules."""
    outcome = OcrOutcome(engine='baidu', raw_text='...', confidence=None,
                         fields=_complete_fields())
    assert _ocr_sufficient(outcome, accept_confidence=85.0)
    partial = _complete_fields(seller_tax_id=None)
    outcome2 = OcrOutcome(engine='baidu', raw_text='...', confidence=None, fields=partial)
    assert not _ocr_sufficient(outcome2, accept_confidence=85.0)


def test_critical_fields_match_status_logic_set():
    """The sufficiency set must stay aligned with the auto-confirm set."""
    assert 'invoice_number' in CRITICAL_FIELDS
    assert 'total_with_tax' in CRITICAL_FIELDS
    assert len(CRITICAL_FIELDS) == 8


@pytest.mark.parametrize('overrides', [
    {'item_name': '8 4 5 3 3 < 7'},
    {'item_name': '12345678'},
    {'tax_rate': None},
])
def test_invalid_item_or_unread_tax_column_cannot_skip_review(overrides):
    outcome = OcrOutcome(engine='paddle_remote', raw_text='电子发票\n税率\n税额\n3%',
                         confidence=98.0, fields=_complete_fields(**overrides))
    assert not _ocr_sufficient(outcome, accept_confidence=85.0)


@pytest.mark.parametrize('overrides, expected_status', [
    ({'item_name': '8 4 5 3 3 < 7'}, '待审核'),
    ({'tax_rate': None}, '待审核'),
    ({}, '已确认'),
])
def test_processing_uses_field_validity_for_final_status_without_llm(monkeypatch, overrides, expected_status):
    import app.services.invoice_service as svc

    invoice = SimpleNamespace(id=1, file_data=b'pdf', file_type='pdf')
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: invoice)),
        add=Mock(), commit=AsyncMock(), rollback=AsyncMock(),
    )
    outcome = OcrOutcome(engine='paddle_remote', raw_text='电子发票\n税率\n税额\n3%',
                         confidence=98.0, fields=_complete_fields(**overrides))
    monkeypatch.setattr(svc, '_run_ocr', lambda *args: outcome)
    monkeypatch.setattr(svc, 'get_active_provider_row', AsyncMock(return_value=None))
    monkeypatch.setattr('app.services.provider_service.get_setting', AsyncMock(return_value=None))
    monkeypatch.setattr(svc, 'LLMService', lambda _: _FakeLLM(available=False))

    assert asyncio.run(svc.process_invoice(1, db))
    assert invoice.status.value == expected_status
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


# --- LLM fallback path selection ---------------------------------------------


class _FakeLLM:
    def __init__(self, available=True, vision=True):
        self.available = available
        self.vision = vision
        self.vision_calls = 0
        self.text_calls = 0

    @property
    def is_available(self):
        return self.available

    def supports_vision(self):
        return self.vision

    def parse_invoice_from_image(self, *_args, **_kwargs):
        self.vision_calls += 1
        return {'invoice_number': '1'}

    def parse_invoice_from_text(self, *_args, **_kwargs):
        self.text_calls += 1
        return {'invoice_number': '2'}


def _run_vision_via_image(service, data, ftype):
    return service.parse_invoice_from_image()


def test_vision_model_reads_image(monkeypatch):
    import app.services.invoice_service as svc
    llm = _FakeLLM(available=True, vision=True)
    monkeypatch.setattr(
        svc, '_run_llm_vision',
        lambda service, data, ftype: service.parse_invoice_from_image(),
    )
    result = svc._run_llm_fallback(llm, 'ocr text', b'image', 'png')
    assert llm.vision_calls == 1
    assert llm.text_calls == 0
    assert result == {'invoice_number': '1'}


def test_text_only_model_parses_ocr_text():
    import app.services.invoice_service as svc
    llm = _FakeLLM(available=True, vision=False)
    result = svc._run_llm_fallback(llm, 'ocr text', b'image', 'png')
    assert llm.text_calls == 1
    assert llm.vision_calls == 0
    assert result == {'invoice_number': '2'}


def test_unavailable_llm_returns_empty():
    import app.services.invoice_service as svc
    llm = _FakeLLM(available=False)
    assert svc._run_llm_fallback(llm, 'ocr text', b'image', 'png') == {}


# --- Baidu vat_invoice field mapping -----------------------------------------


def test_baidu_field_mapping_full_response():
    words = {
        'InvoiceNum': '25952000000233223073',
        'InvoiceDate': '2025年11月05日',
        'PurchaserName': '示例买方科技有限公司',
        'PurchaserRegisterNum': '123456789012345678',
        'SellerName': '示例卖方服务有限公司',
        'SellerRegisterNum': '91440300MA5DA1234X',
        'AmountInFiguers': '¥8,600.00',
        'TotalAmount': '8600.00',
        'TotalTax': '0.00',
        'TaxRate': '免税',
        'CommodityName': ['*信息技术服务*软件开发服务'],
        'SellerAddress': '深圳市南山区',
    }
    fields = map_vat_invoice_fields(words)
    assert fields['invoice_number'] == '25952000000233223073'
    assert fields['issue_date'] == '2025-11-05'
    assert fields['buyer_name'] == '示例买方科技有限公司'
    assert fields['buyer_tax_id'] == '123456789012345678'
    assert fields['seller_name'] == '示例卖方服务有限公司'
    assert fields['seller_tax_id'] == '91440300MA5DA1234X'
    assert fields['total_with_tax'] == '8600.00'
    assert fields['amount'] == '8600.00'
    assert fields['tax_amount'] == '0.00'
    # commodity prefix *分类* stripped
    assert fields['item_name'] == '软件开发服务'
    # unmapped keys must not leak into fields
    assert 'SellerAddress' not in fields and 'seller_address' not in fields


def test_baidu_field_mapping_empty_and_partial():
    fields = map_vat_invoice_fields({})
    assert all(v is None for v in fields.values())
    fields2 = map_vat_invoice_fields({'InvoiceNum': '12345678'})
    assert fields2['invoice_number'] == '12345678'
    assert fields2['buyer_name'] is None
