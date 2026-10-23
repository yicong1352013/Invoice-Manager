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

from app.services.ocr_service import (
    OCRService, RemotePaddleOCRService, parse_remote_paddle_pages,
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
    return SimpleNamespace(
        status_code=status_code,
        raise_for_status=lambda: None,
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

    text, confidence, lines = service.process_pdf(b"%PDF-fake")
    assert "第一页甲" in text and "第二页丙" in text
    # page order preserved even though page 2's local y (100) would sort
    # between page 1's lines without the page offset
    assert [l["text"] for l in lines] == ["第一页甲", "第一页乙", "第二页丙"]
    assert confidence is not None


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
    text, confidence, lines = service.process_image(buf.getvalue())

    assert "发票号码" in text
    assert confidence == (0.98 + 0.91) / 2 * 100
    assert lines[0]["text"] == "发票号码:12345678"
