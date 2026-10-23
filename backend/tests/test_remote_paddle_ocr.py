"""Tests for the custom PaddleOCR HTTP endpoint (paddle_ocr_endpoint).

Business invariants:
- Both mainstream self-hosted protocols must parse: PaddleX/PaddleOCR 3.x
  pipeline serving (rec_texts/rec_scores/dt_polys) and classic PaddleHub
  ([text, poly, (score)] items).
- Endpoints that return no scores must yield confidence=None so the OCR-first
  gate judges by field completeness (same policy as the Baidu engine), never
  a fabricated score.
- The wire contract auto-detects: multipart file first, PaddleHub JSON retry
  on failure, and the working style is remembered.
"""

from types import SimpleNamespace

import numpy as np

from app.services.ocr_service import RemotePaddleOCRService, parse_remote_paddle_response


PADDLEX_PAYLOAD = {
    "logId": "x",
    "result": {
        "rec_texts": ["发票号码:12345678", "价税合计 ¥116.00"],
        "rec_scores": [0.98, 0.91],
        "dt_polys": [[[0, 0], [10, 0], [10, 10], [0, 10]], [[0, 20], [10, 20], [10, 30], [0, 30]]],
    },
}

HUB_PAYLOAD_WITH_SCORE = {
    "msg": "",
    "results": [[
        ["开票日期:2026年01月01日", [[0, 0], [10, 0], [10, 10], [0, 10]], 0.97],
        ["购买方", [[0, 40], [10, 40], [10, 50], [0, 50]]],
    ]],
}


# --- response parsing -----------------------------------------------------------


def test_parse_paddlex_pipeline_serving_format():
    normalized = parse_remote_paddle_response(PADDLEX_PAYLOAD)
    assert normalized == [
        ("发票号码:12345678", 0.98, [[0, 0], [10, 0], [10, 10], [0, 10]]),
        ("价税合计 ¥116.00", 0.91, [[0, 20], [10, 20], [10, 30], [0, 30]]),
    ]


def test_parse_paddlex_nested_ocr_results():
    payload = {"result": {"ocrResults": [PADDLEX_PAYLOAD["result"]]}}
    assert parse_remote_paddle_response(payload) == parse_remote_paddle_response(PADDLEX_PAYLOAD)


def test_parse_paddlehub_items_with_and_without_score():
    normalized = parse_remote_paddle_response(HUB_PAYLOAD_WITH_SCORE)
    assert normalized[0] == ("开票日期:2026年01月01日", 0.97, [[0, 0], [10, 0], [10, 10], [0, 10]])
    # hub items without a score keep confidence=None (no fabricated numbers)
    assert normalized[1] == ("购买方", None, [[0, 40], [10, 40], [10, 50], [0, 50]])


def test_parse_root_level_rec_texts():
    payload = {"rec_texts": ["仅文本"], "rec_scores": [0.5]}
    assert parse_remote_paddle_response(payload) == [("仅文本", 0.5, [])]


def test_parse_unrecognized_payload_returns_empty():
    assert parse_remote_paddle_response({"error": "boom"}) == []
    assert parse_remote_paddle_response("not a dict") == []
    assert parse_remote_paddle_response({"result": []}) == []


# --- adaptive wire protocol -------------------------------------------------------


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


def test_multipart_first_then_hub_json_retry(monkeypatch):
    fake = _FakeHttpx([_resp(404), _resp(200, HUB_PAYLOAD_WITH_SCORE)])
    monkeypatch.setattr("httpx.post", fake)

    service = RemotePaddleOCRService("http://ocr.internal:8866/predict/ocr_system")
    normalized = service._invoke_ocr(_white_image_array())

    assert len(normalized) == 2
    assert service._json_mode is True
    assert len(fake.calls) == 2
    first_url, first_kwargs = fake.calls[0]
    assert first_url == "http://ocr.internal:8866/predict/ocr_system"
    assert "files" in first_kwargs  # PaddleX multipart contract first
    _, second_kwargs = fake.calls[1]
    assert "json" in second_kwargs and "images" in second_kwargs["json"]


def test_multipart_success_is_remembered(monkeypatch):
    fake = _FakeHttpx([_resp(200, PADDLEX_PAYLOAD), _resp(200, PADDLEX_PAYLOAD)])
    monkeypatch.setattr("httpx.post", fake)

    service = RemotePaddleOCRService("http://ocr.internal:9300/ocr/")
    normalized = service._invoke_ocr(_white_image_array())
    service._invoke_ocr(_white_image_array())

    assert len(normalized) == 2
    assert service._json_mode is False
    assert all("files" in kwargs for _, kwargs in fake.calls)


def test_unparseable_200_retries_with_hub_json(monkeypatch):
    fake = _FakeHttpx([_resp(200, {"weird": "shape"}), _resp(200, HUB_PAYLOAD_WITH_SCORE)])
    monkeypatch.setattr("httpx.post", fake)

    service = RemotePaddleOCRService("http://ocr.internal:8866/predict/ocr_system")
    normalized = service._invoke_ocr(_white_image_array())

    assert len(normalized) == 2
    assert service._json_mode is True


def test_process_image_reports_average_confidence(monkeypatch):
    import io

    from PIL import Image

    monkeypatch.setattr("httpx.post", _FakeHttpx([_resp(200, PADDLEX_PAYLOAD)]))

    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, format="PNG")

    service = RemotePaddleOCRService("http://ocr.internal:9300/ocr")
    text, confidence, lines = service.process_image(buf.getvalue())

    assert "发票号码" in text
    assert confidence == (0.98 + 0.91) / 2 * 100
    assert len(lines) == 2
    assert lines[0]["text"] == "发票号码:12345678"
