"""The detail response must preserve stored fields and their separate sources."""

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models.invoice import AiVerification, Invoice, InvoiceStatus, LlmResult, OcrResult, ParsingDiff
from app.models.user import UserRole
from app.routers.invoices import get_invoice


class AsyncDb:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)


@pytest.mark.parametrize('populated', [True, False])
def test_detail_preserves_all_invoice_fields_without_borrowing_source_values(populated):
    fields = {
        'invoice_number': '00001234', 'invoice_code': '001234567890',
        'invoice_type': '测试增值税电子普通发票', 'issue_date': date(2026, 10, 24),
        'buyer_name': '测试购买方', 'buyer_tax_id': 'BUYER001',
        'seller_name': '测试销售方', 'seller_tax_id': 'SELLER002',
        'buyer_address_phone': '购买方地址 电话001', 'buyer_bank_account': '购买方银行 000123',
        'seller_address_phone': '销售方地址 电话002', 'seller_bank_account': '销售方银行 000456',
        'item_name': '测试服务*型号A1', 'specification': 'A1', 'unit': '次',
        'quantity': Decimal('0'), 'unit_price': Decimal('0'), 'amount': Decimal('0'),
        'tax_amount': Decimal('0'), 'tax_rate': '0%', 'total_with_tax': Decimal('0'),
        'total_with_tax_cn': '零元整', 'drawer': '测试开票人', 'remark': '测试备注',
    }
    if not populated:
        fields = dict.fromkeys(fields)
    engine = create_engine('sqlite://')
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            invoice = Invoice(file_name='test.pdf', file_type='pdf', file_data=b'original',
                              owner='alice', status=InvoiceStatus.CONFIRMED, **fields)
            session.add(invoice)
            session.flush()
            session.add_all([
                OcrResult(invoice_id=invoice.id, raw_text='原始OCR文字', raw_response='原始OCR响应', drawer='OCR开票人'),
                LlmResult(invoice_id=invoice.id, drawer='LLM开票人'),
                ParsingDiff(invoice_id=invoice.id, field_name='drawer', ocr_value='OCR开票人', llm_value='LLM开票人'),
                AiVerification(invoice_id=invoice.id, overall='pass', mode='text', fields=[
                    {'field': 'drawer', 'status': 'incorrect', 'severity': 'warning', 'corrected_value': '建议开票人'},
                ], raw_response='原始AI响应'),
            ])
            session.flush()
            user = SimpleNamespace(username='alice', role=UserRole.USER)

            response = asyncio.run(get_invoice(invoice.id, db=AsyncDb(session), current_user=user))

            for name, expected in fields.items():
                assert getattr(response, name) == expected, f'detail lost or replaced stored {name}'
            assert response.ocr_result.raw_text == '原始OCR文字'
            assert response.ocr_result.raw_response == '原始OCR响应'
            assert response.ocr_result.drawer == 'OCR开票人'
            assert response.llm_result.drawer == 'LLM开票人'
            assert response.parsing_diffs[0].field_name == 'drawer'
            assert response.ai_verification.fields[0]['severity'] == 'warning'
            assert response.ai_verification.raw_response == '原始AI响应'
            assert 'file_data' not in response.model_dump()
    finally:
        engine.dispose()
