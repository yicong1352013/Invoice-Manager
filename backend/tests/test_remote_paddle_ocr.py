"""Tests for the custom PaddleOCR HTTP endpoint (paddle_ocr_endpoint).

Contract under test is the real deployed service (paddlex --serve,
AIStudio-style JSON API):
    POST {endpoint} {"file": <raw base64>, "fileType": 1}
    -> {"errorCode": 0, "result": {"ocrResults": [{"prunedResult":
         {"rec_texts": [...], "rec_scores": [...], "rec_polys": [...]}}]}}

Business invariants:
- The AIStudio nesting (result.ocrResults[].prunedResult) must parse;
  multiple ocrResults entries are PDF pages concatenated in order.
- Plain PaddleX pipeline serving (result.rec_texts, multipart upload) is
  the fallback contract and must keep working.
- errorCode != 0 must raise with the service's errorMsg, never silently
  return empty recognition.
- Lines without scores keep confidence=None so the OCR-first gate judges
  by field completeness (same policy as the Baidu engine).
"""

from types import SimpleNamespace

import numpy as np
import pytest

from app.services.ocr_service import (
    FieldExtractor, OCRService, RemotePaddleOCRService, parse_remote_paddle_pages,
    parse_remote_paddle_response,
)


def _page(texts, scores, y0=0):
    return {
        "prunedResult": {
            "rec_texts": texts,
            "rec_scores": scores,
            "rec_polys": [[[0, y0 + i * 20], [10, y0 + i * 20], [10, y0 + i * 20 + 10], [0, y0 + i * 20 + 10]]
                          for i in range(len(texts))],
            "dt_polys": [],
        }
    }


AISTUDIO_PAYLOAD = {
    "logId": "x",
    "errorCode": 0,
    "errorMsg": "Success",
    "result": {
        "ocrResults": [_page(["发票号码:12345678", "价税合计 ¥116.00"], [0.98, 0.91])],
        "dataInfo": {"width": 700, "height": 160, "type": "image"},
    },
}


@pytest.mark.parametrize('cipher', [
    '// -9 7 < > 1 + < 2 4 6 6 3 < 4 9 - 7* 8 4 5 3 3 < 7',
    '0 3 6 8 */ 6 2 1 7 + 4 1 > 6 1 4 < 9 < 8 + / 9 / 6 > /',
    '/ < / 3 - 7 1 < 5 < 7 9 2 > 5 > 7 - 2 4 1 * + - 3 8 > *',
])
def test_remote_pdf_cipher_never_becomes_item_and_tax_column_is_extracted(monkeypatch, cipher):
    texts = ['电子普通发票', cipher, '项目名称', '金额', '税率', '税额',
             '*经营租赁*通行费', '20.90', '3%', '0.63']
    payload = {'errorCode': 0, 'result': {'ocrResults': [_page(texts, [0.98] * len(texts))]}}
    monkeypatch.setattr('httpx.post', _FakeHttpx([_resp(200, payload)]))

    raw, _, lines, response = RemotePaddleOCRService('http://ocr.test/ocr').process_pdf(b'pdf')
    fields = FieldExtractor().extract_fields(raw, lines)

    assert fields['item_name'] == '经营租赁*通行费'
    assert fields['tax_rate'] == '3%'
    assert cipher in response


@pytest.mark.parametrize('rates, expected', [
    (['1.5%'], '1.5%'),
    (['免税'], '免税'),
    (['不征税'], '不征税'),
    (['3%', '13%'], None),
])
def test_separate_tax_column_preserves_rate_or_requires_review_for_multiple_rates(rates, expected):
    raw = '\n'.join(['电子普通发票', '税率/征收率', '税额', '*服务*测试服务', *rates])
    assert FieldExtractor().extract_fields(raw)['tax_rate'] == expected

PLAIN_PADDLEX_PAYLOAD = {
    "logId": "x",
    "result": {
        "rec_texts": ["发票号码:12345678", "价税合计 ¥116.00"],
        "rec_scores": [0.98, 0.91],
        "dt_polys": [[[0, 0], [10, 0], [10, 10], [0, 10]], [[0, 20], [10, 20], [10, 30], [0, 30]]],
    },
}


# --- response parsing -----------------------------------------------------------


def test_parse_aistudio_nested_pruned_result():
    normalized = parse_remote_paddle_response(AISTUDIO_PAYLOAD)
    assert [n[0] for n in normalized] == ["发票号码:12345678", "价税合计 ¥116.00"]
    assert normalized[0][1] == 0.98
    assert normalized[0][2] == [[0, 0], [10, 0], [10, 10], [0, 10]]


def test_parse_aistudio_multipage_pdf_concatenates_in_order():
    payload = {
        "errorCode": 0,
        "result": {"ocrResults": [
            _page(["第一页文本"], [0.9], y0=0),
            _page(["第二页文本"], [0.95], y0=100),
        ], "dataInfo": {"type": "pdf"}},
    }
    normalized = parse_remote_paddle_response(payload)
    assert [n[0] for n in normalized] == ["第一页文本", "第二页文本"]


def test_parse_aistudio_missing_scores_keep_none():
    page = {"prunedResult": {"rec_texts": ["无分数行"], "rec_polys": [[[0, 0], [1, 0], [1, 1], [0, 1]]]}}
    payload = {"errorCode": 0, "result": {"ocrResults": [page]}}
    normalized = parse_remote_paddle_response(payload)
    assert normalized == [("无分数行", None, [[0, 0], [1, 0], [1, 1], [0, 1]])]


def test_parse_plain_paddlex_serving_format():
    normalized = parse_remote_paddle_response(PLAIN_PADDLEX_PAYLOAD)
    assert [n[0] for n in normalized] == ["发票号码:12345678", "价税合计 ¥116.00"]
    assert normalized[1][1] == 0.91


def test_parse_unrecognized_payload_returns_empty():
    assert parse_remote_paddle_response({"error": "boom"}) == []
    assert parse_remote_paddle_response("not a dict") == []
    assert parse_remote_paddle_response({"result": {"ocrResults": []}}) == []


# --- wire protocol ---------------------------------------------------------------


class _FakeHttpx:
    """Captures httpx.post calls and replays scripted responses."""

    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


def _resp(status_code=200, payload=None):
    import json as json_mod

    body = json_mod.dumps(payload if payload is not None else {}, ensure_ascii=False)
    return SimpleNamespace(
        status_code=status_code,
        raise_for_status=lambda: None,
        text=body,
        json=lambda: payload if payload is not None else {},
    )


def _white_image_array():
    return np.full((20, 40, 3), 255, dtype=np.uint8)


def test_json_body_matches_aistudio_contract(monkeypatch):
    fake = _FakeHttpx([_resp(200, AISTUDIO_PAYLOAD)])
    monkeypatch.setattr("httpx.post", fake)

    service = RemotePaddleOCRService("http://192.168.66.32:8080/ocr")
    normalized = service._invoke_ocr(_white_image_array())

    assert len(normalized) == 2
    assert service._multipart_mode is False
    url, kwargs = fake.calls[0]
    assert url == "http://192.168.66.32:8080/ocr"
    body = kwargs["json"]
    assert set(body) == {"file", "fileType"}
    assert body["fileType"] == 1
    # raw base64 (no data: prefix) decodes back to PNG bytes
    import base64 as b64mod

    assert b64mod.b64decode(body["file"])[:4] == b"\x89PNG"


def test_json_422_falls_back_to_multipart(monkeypatch):
    fake = _FakeHttpx([_resp(422), _resp(200, PLAIN_PADDLEX_PAYLOAD)])
    monkeypatch.setattr("httpx.post", fake)

    service = RemotePaddleOCRService("http://paddlex-official:8080/ocr")
    normalized = service._invoke_ocr(_white_image_array())

    assert len(normalized) == 2
    assert service._multipart_mode is True
    assert "json" in fake.calls[0][1]
    assert "files" in fake.calls[1][1]


def test_error_code_raises_with_service_message(monkeypatch):
    payload = {"logId": "x", "errorCode": 1001, "errorMsg": "Invalid input file"}
    monkeypatch.setattr("httpx.post", _FakeHttpx([_resp(200, payload)]))

    service = RemotePaddleOCRService("http://192.168.66.32:8080/ocr")
    try:
        service._invoke_ocr(_white_image_array())
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "Invalid input file" in str(e)


def test_api_key_sent_in_both_conventional_headers(monkeypatch):
    fake = _FakeHttpx([_resp(200, AISTUDIO_PAYLOAD)])
    monkeypatch.setattr("httpx.post", fake)

    service = RemotePaddleOCRService("http://192.168.66.32:8080/ocr", api_key="proxy-secret-1")
    service._invoke_ocr(_white_image_array())

    headers = fake.calls[0][1]["headers"]
    assert headers["Authorization"] == "Bearer proxy-secret-1"
    assert headers["X-API-Key"] == "proxy-secret-1"


def test_no_api_key_sends_no_auth_headers(monkeypatch):
    fake = _FakeHttpx([_resp(200, AISTUDIO_PAYLOAD)])
    monkeypatch.setattr("httpx.post", fake)

    service = RemotePaddleOCRService("http://192.168.66.32:8080/ocr")
    service._invoke_ocr(_white_image_array())

    headers = fake.calls[0][1].get("headers") or {}
    assert "Authorization" not in headers
    assert "X-API-Key" not in headers


def test_api_key_kept_on_multipart_fallback(monkeypatch):
    fake = _FakeHttpx([_resp(401), _resp(200, PLAIN_PADDLEX_PAYLOAD)])
    monkeypatch.setattr("httpx.post", fake)

    service = RemotePaddleOCRService("http://paddlex-official:8080/ocr", api_key="proxy-secret-1")
    service._invoke_ocr(_white_image_array())

    assert service._multipart_mode is True
    headers = fake.calls[1][1]["headers"]
    assert headers["X-API-Key"] == "proxy-secret-1"


def test_remote_service_sends_whole_pdf_natively(monkeypatch):
    """A configured remote endpoint must OCR digital PDFs itself (fileType=0)
    instead of the local text-layer shortcut or a poppler rasterizer."""
    service = RemotePaddleOCRService("http://192.168.66.32:8080/ocr")
    assert service.prefer_text_layer is False
    assert OCRService().prefer_text_layer is True

    two_pages = {
        "errorCode": 0,
        "result": {"ocrResults": [
            _page(["第一页甲", "第一页乙"], [0.9, 0.91], y0=0),
            _page(["第二页丙"], [0.95], y0=100),  # page-local y overlaps page 1
        ], "dataInfo": {"type": "pdf"}},
    }
    monkeypatch.setattr("httpx.post", _FakeHttpx([_resp(200, two_pages)]))

    text, confidence, lines, raw_response = service.process_pdf(b"%PDF-fake")
    assert "第一页甲" in text and "第二页丙" in text
    # page order preserved even though page 2's local y (100) would sort
    # between page 1's lines without the page offset
    assert [l["text"] for l in lines] == ["第一页甲", "第一页乙", "第二页丙"]
    assert confidence is not None
    # the verbatim service body is carried through for the detail page
    assert raw_response is not None and "第一页甲" in raw_response


def test_parse_remote_paddle_pages_keeps_pages_separate():
    two_pages = {
        "errorCode": 0,
        "result": {"ocrResults": [
            _page(["第一页文本"], [0.9], y0=0),
            _page(["第二页文本"], [0.95], y0=100),
        ]},
    }
    pages = parse_remote_paddle_pages(two_pages)
    assert len(pages) == 2
    assert pages[0][0][0] == "第一页文本"
    assert pages[1][0][0] == "第二页文本"


def test_process_image_reports_average_confidence(monkeypatch):
    import io

    from PIL import Image

    monkeypatch.setattr("httpx.post", _FakeHttpx([_resp(200, AISTUDIO_PAYLOAD)]))

    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, format="PNG")

    service = RemotePaddleOCRService("http://192.168.66.32:8080/ocr")
    text, confidence, lines, raw_response = service.process_image(buf.getvalue())

    assert "发票号码" in text
    assert confidence == (0.98 + 0.91) / 2 * 100
    assert lines[0]["text"] == "发票号码:12345678"


def _positioned_page(items):
    """Synthetic invoice cells; all values and coordinates are test data."""
    return {"prunedResult": {
        "rec_texts": [text for text, x, y, width in items],
        "rec_scores": [0.99] * len(items),
        "rec_polys": [[[x, y], [x + width, y], [x + width, y + 10], [x, y + 10]]
                      for text, x, y, width in items],
    }}


def _table_items():
    return [
        ('电子发票（普通发票）', 10, 10, 160),
        ('项目名称', 10, 100, 180), ('规格型号', 210, 100, 60),
        ('单位', 300, 100, 40), ('数量', 380, 100, 40),
        ('单价', 460, 100, 40), ('金额', 540, 100, 40),
        ('税率', 620, 100, 40), ('税额', 700, 100, 40),
        ('*设备*终端 A8', 10, 125, 150), ('X200', 210, 125, 50),
        ('台', 310, 125, 10), ('1', 423, 125, 10),
        ('13.21', 480, 125, 35), ('13.21', 560, 125, 35),
        ('6%', 635, 125, 20), ('0.79', 715, 125, 25),
        ('合计', 10, 170, 60),
    ]


def _extract_remote_fields(monkeypatch, *pages):
    payload = {'result': {'ocrResults': [_positioned_page(items) for items in pages]}}
    monkeypatch.setattr('httpx.post', _FakeHttpx([_resp(200, payload)]))
    raw, _, lines, _ = RemotePaddleOCRService('http://ocr.test/ocr').process_pdf(b'pdf')
    return FieldExtractor().extract_fields(raw, lines)


def test_single_item_columns_preserve_models_and_right_aligned_numbers(monkeypatch):
    fields = _extract_remote_fields(monkeypatch, _table_items())
    assert {name: fields.get(name) for name in
            ('item_name', 'specification', 'unit', 'quantity', 'unit_price')} == {
        'item_name': '设备*终端 A8', 'specification': 'X200',
        'unit': '台', 'quantity': '1', 'unit_price': '13.21',
    }


def test_reliable_name_column_preserves_multiple_legitimate_model_numbers(monkeypatch):
    items = [('*设备*终端 2 4 8' if text == '*设备*终端 A8' else text, x, y, width)
             for text, x, y, width in _table_items()]
    fields = _extract_remote_fields(monkeypatch, items)
    assert fields['item_name'] == '设备*终端 2 4 8'
    assert (fields['quantity'], fields['unit_price']) == ('1', '13.21')


def test_split_table_header_and_zero_values_are_not_missing(monkeypatch):
    items = [item for item in _table_items() if item[0] not in ('数量', 'X200')]
    items.extend([('数', 380, 100, 16), ('量', 398, 100, 16)])
    items = [('0' if text == '1' else '0.00' if text == '13.21' else text, x, y, width)
             for text, x, y, width in items]
    fields = _extract_remote_fields(monkeypatch, items)
    assert (fields.get('specification'), fields.get('quantity'), fields.get('unit_price')) == (None, '0', '0.00')


def test_empty_price_column_is_not_filled_from_amount(monkeypatch):
    items = [item for item in _table_items() if not (item[1] == 480 and item[2] == 125)]
    fields = _extract_remote_fields(monkeypatch, items)
    assert fields.get('quantity') == '1'
    assert fields.get('unit_price') is None


def test_multiple_items_do_not_supply_flat_quantity_unit_or_price(monkeypatch):
    items = _table_items() + [('*服务*安装', 10, 145, 120), ('次', 310, 145, 10),
                              ('2', 423, 145, 10), ('25.00', 480, 145, 35)]
    fields = _extract_remote_fields(monkeypatch, items)
    assert all(fields.get(name) is None for name in ('specification', 'unit', 'quantity', 'unit_price'))


def test_items_continued_on_another_page_do_not_supply_flat_item_values(monkeypatch):
    fields = _extract_remote_fields(monkeypatch, _table_items(), [
        ('*服务*安装', 10, 20, 120), ('次', 310, 20, 10), ('2', 423, 20, 10),
    ])
    assert all(fields.get(name) is None for name in ('specification', 'unit', 'quantity', 'unit_price'))


def test_table_mapping_uses_relative_columns_after_resize_and_translation(monkeypatch):
    items = [(text, x * 1.7 + 200, y * 1.7 + 80, width * 1.7)
             for text, x, y, width in _table_items()]
    fields = _extract_remote_fields(monkeypatch, items)
    assert (fields.get('unit'), fields.get('quantity'), fields.get('unit_price')) == ('台', '1', '13.21')


@pytest.mark.parametrize('title', ['北京增值税电子普通发票', '广东增值税专用发票'])
def test_old_invoice_title_can_share_a_line_with_invoice_code(title):
    fields = FieldExtractor().extract_fields(f'{title} 发票代码：011000000001\n发票号码：12345678')
    assert fields['invoice_type'] == title


def _digital_invoice_pdf():
    from io import BytesIO
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    output = BytesIO()
    document = canvas.Canvas(output, pagesize=(800, 240))
    document.setFont('STSong-Light', 10)
    items = [(text, x, y, width) for text, x, y, width in _table_items() if y != 10]
    items += [
        ('北京增值税电子普通发票', 10, 10, 180), ('发票代码：011000000001', 280, 10, 160),
        ('发票号码：12345678', 10, 30, 150), ('开票日期：2023年04月06日', 280, 30, 180),
        ('购买方名称：示例买方有限公司', 10, 50, 200),
        ('纳税人识别号：123456789012345', 10, 70, 200),
        ('价税合计（小写）￥14.00', 10, 190, 180),
    ]
    for text, x, y, _ in items:
        document.drawString(x, 240 - y, text)
    document.save()
    return output.getvalue()


def test_pdf_text_layer_keeps_item_columns_separate_without_running_ocr():
    raw, _, lines, _ = OCRService().process_pdf(_digital_invoice_pdf())
    fields = FieldExtractor().extract_fields(raw, lines)
    assert {name: fields.get(name) for name in ('item_name', 'quantity', 'unit_price')} == {
        'item_name': '设备*终端 A8', 'quantity': '1', 'unit_price': '13.21',
    }


def test_remote_pdf_title_uses_embedded_evidence_without_replacing_ocr_raw(monkeypatch):
    from app.services.invoice_service import _run_ocr

    payload = {'result': {'ocrResults': [_positioned_page([
        ('北京增值税电普通发票', 10, 10, 180), ('发票号码：12345678', 10, 30, 180),
    ])]}}
    fake = _FakeHttpx([_resp(200, payload)])
    monkeypatch.setattr('httpx.post', fake)
    outcome = _run_ocr('local', None, None, _digital_invoice_pdf(), 'pdf', 'http://ocr.test/ocr')
    assert outcome.fields['invoice_type'] == '北京增值税电子普通发票'
    assert outcome.raw_text == '北京增值税电普通发票\n发票号码：12345678'
    assert outcome.raw_response == _resp(200, payload).text
    assert len(fake.calls) == 1


def test_plain_text_table_without_coordinates_does_not_swallow_numeric_columns():
    text = '电子普通发票\n项目名称 规格型号 单位 数量 单价 金额 税率 税额\n*设备*终端 A8 台 1 13.21 13.21 6% 0.79'
    fields = FieldExtractor().extract_fields(text)
    assert fields['item_name'] is None
    assert all(fields.get(name) is None for name in ('unit', 'quantity', 'unit_price'))


def test_split_total_label_stops_the_item_table(monkeypatch):
    items = [item for item in _table_items() if item[0] != '合计']
    items += [('合', 10, 170, 10), ('计', 45, 170, 10), ('￥13.21', 560, 170, 45),
              ('价税合计', 10, 190, 80), ('￥14.00', 560, 190, 45)]
    fields = _extract_remote_fields(monkeypatch, items)
    assert (fields.get('unit'), fields.get('quantity'), fields.get('unit_price')) == ('台', '1', '13.21')


def test_missing_quantity_header_cannot_turn_quantity_into_an_empty_price(monkeypatch):
    items = [item for item in _table_items() if item[0] != '数量'
             and not (item[1] == 480 and item[2] == 125)]
    fields = _extract_remote_fields(monkeypatch, items)
    assert fields.get('quantity') is None
    assert fields.get('unit_price') is None


def test_multi_page_pdf_text_does_not_put_numeric_columns_in_item_name():
    from io import BytesIO
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    source = PdfReader(BytesIO(_digital_invoice_pdf()))
    writer.add_page(source.pages[0])
    writer.add_page(source.pages[0])
    output = BytesIO()
    writer.write(output)
    raw, _, lines, _ = OCRService().process_pdf(output.getvalue())
    fields = FieldExtractor().extract_fields(raw, lines)
    assert fields['item_name'] is None
    assert all(fields.get(name) is None for name in ('specification', 'unit', 'quantity', 'unit_price'))
