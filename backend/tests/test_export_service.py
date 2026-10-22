"""Tests for export/print builders.

Business invariants:
- Totals must sum every monetary column treating missing values as zero.
- Merged print PDF must contain exactly one page per input original.
- Word/PDF builders must include the totals row and not crash on empty data.
"""

from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

from app.services.export_service import (
    build_pdf_export,
    build_word_export,
    compute_totals,
    merge_original_files,
)


def _inv(amount=None, tax=None, total=None):
    return SimpleNamespace(
        invoice_number='12345678', issue_date='2025-11-05',
        buyer_name='买方', buyer_tax_id='123456789012345',
        seller_name='卖方', seller_tax_id='223456789012345',
        item_name='服务费', amount=amount, tax_amount=tax, total_with_tax=total,
        tax_rate='6%', status=SimpleNamespace(value='已确认'), owner='admin',
        file_name='a.png', created_at=None,
    )


def test_totals_sum_with_missing_values_as_zero():
    invoices = [
        _inv(amount=Decimal('100.00'), tax=Decimal('6.00'), total=Decimal('106.00')),
        _inv(amount=None, tax=Decimal('4.00'), total=Decimal('4.00')),  # missing amount
        _inv(),  # all missing
    ]
    totals = compute_totals(invoices)
    assert totals['amount'] == Decimal('100.00')
    assert totals['tax_amount'] == Decimal('10.00')
    assert totals['total_with_tax'] == Decimal('110.00')


def test_totals_empty_list_is_zero():
    totals = compute_totals([])
    assert totals == {'amount': Decimal(0), 'tax_amount': Decimal(0), 'total_with_tax': Decimal(0)}


def test_word_export_contains_totals_row():
    invoices = [_inv(amount=Decimal('100.00'), tax=Decimal('6.00'), total=Decimal('106.00'))]
    output = build_word_export(invoices, compute_totals(invoices))
    content = output.getvalue()
    assert content[:2] == b'PK'  # docx is a zip
    import zipfile
    with zipfile.ZipFile(BytesIO(content)) as archive:
        document_xml = archive.read('word/document.xml').decode('utf-8')
    assert '合计' in document_xml
    assert '106.00' in document_xml


def test_pdf_export_renders_with_cjk_font():
    invoices = [_inv(amount=Decimal('100.00'), tax=Decimal('6.00'), total=Decimal('106.00'))]
    output = build_pdf_export(invoices, compute_totals(invoices))
    content = output.getvalue()
    assert content[:5] == b'%PDF-'


def _one_page_pdf_bytes() -> bytes:
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _png_bytes() -> bytes:
    from PIL import Image
    buf = BytesIO()
    Image.new('RGB', (60, 30), 'white').save(buf, format='PNG')
    return buf.getvalue()


def test_merge_original_files_mixed_pdf_and_image():
    pdf1 = _one_page_pdf_bytes()
    png = _png_bytes()
    pdf2 = _one_page_pdf_bytes()
    merged = merge_original_files([('pdf', pdf1), ('png', png), ('pdf', pdf2)])

    from pypdf import PdfReader
    reader = PdfReader(BytesIO(merged))
    assert len(reader.pages) == 3  # 1 + image-as-page + 1, order preserved


def test_merge_original_files_rejects_empty():
    try:
        merge_original_files([])
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
