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
import json
from decimal import Decimal
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
        'InvoiceType': '电子发票（普通发票）',
        'InvoiceCode': '044031900111',
        'InvoiceDate': '2025年11月05日',
        'PurchaserName': '示例买方科技有限公司',
        'PurchaserRegisterNum': '123456789012345678',
        'PurchaserAddress': '北京市海淀区中关村大街1号 010-88888888',
        'PurchaserBank': '工商银行北京分行 0200001019200365678',
        'SellerName': '示例卖方服务有限公司',
        'SellerRegisterNum': '91440300MA5DA1234X',
        'SellerAddress': '深圳市南山区',
        'SellerBank': '招商银行深圳分行 755916666660801',
        'AmountInFiguers': '¥8,600.00',
        'AmountInWords': '捌仟陆佰元整',
        'TotalAmount': '8600.00',
        'TotalTax': '0.00',
        'TaxRate': '免税',
        'Remarks': '测试备注信息',
        'NoteDrawer': '李开票',
        'CommodityName': ['*信息技术服务*软件开发服务'],
    }
    fields = map_vat_invoice_fields(words)
    assert fields['invoice_number'] == '25952000000233223073'
    assert fields['invoice_type'] == '电子发票（普通发票）'
    assert fields['invoice_code'] == '044031900111'
    assert fields['issue_date'] == '2025-11-05'
    assert fields['buyer_name'] == '示例买方科技有限公司'
    assert fields['buyer_tax_id'] == '123456789012345678'
    assert fields['buyer_address_phone'] == '北京市海淀区中关村大街1号 010-88888888'
    assert fields['buyer_bank_account'] == '工商银行北京分行 0200001019200365678'
    assert fields['seller_name'] == '示例卖方服务有限公司'
    assert fields['seller_tax_id'] == '91440300MA5DA1234X'
    assert fields['seller_address_phone'] == '深圳市南山区'
    assert fields['seller_bank_account'] == '招商银行深圳分行 755916666660801'
    assert fields['total_with_tax'] == '8600.00'
    assert fields['total_with_tax_cn'] == '捌仟陆佰元整'
    assert fields['amount'] == '8600.00'
    assert fields['tax_amount'] == '0.00'
    assert fields['remark'] == '测试备注信息'
    assert fields['drawer'] == '李开票'
    # commodity prefix *分类* stripped
    assert fields['item_name'] == '软件开发服务'
    # raw Baidu keys must not leak into fields
    assert 'SellerAddress' not in fields


def test_baidu_field_mapping_empty_and_partial():
    fields = map_vat_invoice_fields({})
    assert all(v is None for v in fields.values())
    fields2 = map_vat_invoice_fields({'InvoiceNum': '12345678'})
    assert fields2['invoice_number'] == '12345678'
    assert fields2['buyer_name'] is None


# --- 数电票 header fields flow through extraction/compare/persist --------------


def test_header_fields_flow_through_compare_and_update():
    """New header fields participate in conflict resolution and persistence:
    - invoice_code found by both engines is kept (it used to be dropped);
    - an LLM-only field (buyer address) is adopted without review;
    - a conflicting new field is left blank for manual review.
    """
    from app.services.invoice_service import (
        COMPARABLE_FIELDS,
        _compare_and_resolve,
        _update_invoice_from_fields,
    )

    assert 'invoice_type' in COMPARABLE_FIELDS
    assert 'invoice_code' in COMPARABLE_FIELDS

    ocr = _complete_fields(invoice_type='电子发票（普通发票）', invoice_code='044031900111')
    llm = _complete_fields(
        invoice_type='电子发票（普通发票）', invoice_code='044031900111',
        buyer_address_phone='北京市海淀区 010-88888888',
        remark='合同编号:HT2025',
    )
    final_fields, _diffs = _compare_and_resolve(ocr, llm, has_llm=True)

    assert final_fields['invoice_code'] == '044031900111'  # no longer dropped
    assert final_fields['buyer_address_phone'] == '北京市海淀区 010-88888888'
    assert final_fields['remark'] == '合同编号:HT2025'

    invoice = SimpleNamespace()
    _update_invoice_from_fields(invoice, final_fields)
    assert invoice.invoice_type == '电子发票（普通发票）'
    assert invoice.invoice_code == '044031900111'
    assert invoice.buyer_address_phone == '北京市海淀区 010-88888888'
    assert invoice.remark == '合同编号:HT2025'

    # OCR and LLM disagree on a new field -> blank + needs review, like old fields
    final2, diffs2 = _compare_and_resolve(
        _complete_fields(drawer='张三'), _complete_fields(drawer='李四'), has_llm=True)
    assert final2['drawer'] is None
    assert any(d['field_name'] == 'drawer' and d['needs_review'] for d in diffs2)


def test_ocr_regex_extracts_new_header_fields():
    """Label-anchored regexes pick up 发票类型/大写合计/开票人/备注 from OCR text."""
    from app.services.ocr_service import FieldExtractor

    text = "\n".join([
        "电子发票（普通发票）",
        "发票号码：25952000000233223073",
        "开票日期：2025年11月05日",
        "价税合计（大写）◎捌仟陆佰元整 （小写）¥8600.00",
        "开票人：李四",
        "备注：测试备注",
    ])
    fields = FieldExtractor().extract_fields(text)
    assert fields['invoice_type'] == '电子发票（普通发票）'
    assert fields['total_with_tax_cn'] == '捌仟陆佰元整'
    assert fields['drawer'] == '李四'
    assert fields['remark'] == '测试备注'


@pytest.mark.parametrize('raw, expected', [
    ('1,000', Decimal('1000')), ('不适用', None), ('NaN', None),
    ('100000000', None), ('0.00001', None),
])
def test_optional_llm_number_cannot_fail_otherwise_valid_invoice(monkeypatch, raw, expected):
    import app.services.invoice_service as svc
    from app.services.llm_service import LLMService

    invoice = SimpleNamespace(id=1, file_data=b'isolated', file_type='png')
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: invoice)),
        add=Mock(), commit=AsyncMock(), rollback=AsyncMock(),
    )
    outcome = OcrOutcome('local', 'isolated', 70.0, _complete_fields())
    fields = LLMService()._parse_json_response(json.dumps(_complete_fields(quantity=raw)), 'test')
    monkeypatch.setattr(svc, '_run_ocr', lambda *args: outcome)
    monkeypatch.setattr(svc, 'get_active_provider_row', AsyncMock(return_value=None))
    monkeypatch.setattr('app.services.provider_service.get_setting', AsyncMock(return_value=None))
    monkeypatch.setattr(svc, 'LLMService', lambda _: SimpleNamespace(is_available=True))
    monkeypatch.setattr(svc, '_run_llm_fallback', lambda *args: fields)

    assert asyncio.run(svc.process_invoice(1, db))
    assert invoice.quantity == expected
    assert invoice.total_with_tax == Decimal('8600.00')
    assert invoice.status.value == '已确认'
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.parametrize('raw', ['不适用', 'NaN', 'Infinity', '100000000', '0.00001'])
def test_invalid_optional_ocr_numbers_do_not_reach_invoice_columns(raw):
    from app.services.invoice_service import _update_invoice_from_fields

    invoice = SimpleNamespace(quantity=None, unit_price=None)
    _update_invoice_from_fields(invoice, {'quantity': raw, 'unit_price': raw, 'amount': '12.34'})
    assert invoice.quantity is None and invoice.unit_price is None
    assert invoice.amount == Decimal('12.34')
