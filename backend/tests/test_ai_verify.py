"""Tests for the detail-page AI verification flow ("AI校验").

Business invariants:
- The verdict table only ever contains the known comparable fields, each with a
  normalized status; overall is recomputed locally and never trusted from the
  model (a model claiming "pass" with an incorrect field must surface issues).
- corrected_value only survives on 'incorrect' verdicts — the apply-corrections
  UI must never write values the reviewer did not see flagged as wrong.
- Vision providers must read the original file; PDFs without a poppler render
  must degrade to OCR text instead of failing; with neither source there is
  nothing to verify (ValueError → 400, not a paid LLM call).
"""

import json
from types import SimpleNamespace

import pytest

import app.services.invoice_service as invoice_service
from app.services.invoice_service import (
    COMPARABLE_FIELDS,
    build_verify_prompt,
    parse_verify_response,
    run_ai_verify,
)
from app.services.llm_service import LLMService


def _verdict_payload(overall="pass", fields=None, summary="全部一致"):
    return json.dumps({
        "overall": overall,
        "fields": fields if fields is not None else [
            {"field": name, "status": "correct", "corrected_value": None, "reason": ""}
            for name in COMPARABLE_FIELDS
        ],
        "summary": summary,
    }, ensure_ascii=False)


# --- parse_verify_response ------------------------------------------------------


def test_parse_strips_code_fence_and_normalizes():
    content = "```json\n" + _verdict_payload(fields=[
        {"field": "invoice_number", "status": "CORRECT ", "corrected_value": "x", "reason": None},
        {"field": "tax_rate", "status": "weird", "corrected_value": "9%", "reason": "看不清"},
    ]) + "\n```"
    verdict = parse_verify_response(content)

    by_field = {f["field"]: f for f in verdict["fields"]}
    # unknown status normalized to unsure; corrected_value dropped (not incorrect)
    assert by_field["tax_rate"]["status"] == "unsure"
    assert by_field["tax_rate"]["corrected_value"] is None
    # corrected_value dropped for non-incorrect even when the model sent one
    assert by_field["invoice_number"]["status"] == "correct"
    assert by_field["invoice_number"]["corrected_value"] is None
    # every comparable field present, missing ones filled as unsure
    assert set(by_field) == set(COMPARABLE_FIELDS)
    assert by_field["amount"]["status"] == "unsure"
    assert verdict["overall"] == "issues"


def test_parse_overall_recomputed_never_trusted():
    """A model claiming pass while flagging an incorrect field must be issues."""
    fields = [
        {"field": name, "status": "correct"} for name in COMPARABLE_FIELDS
    ]
    fields[0] = {"field": COMPARABLE_FIELDS[0], "status": "incorrect",
                 "corrected_value": "13060593", "reason": "号码识别错位"}
    verdict = parse_verify_response(_verdict_payload(overall="pass", fields=fields))

    assert verdict["overall"] == "issues"
    by_field = {f["field"]: f for f in verdict["fields"]}
    assert by_field[COMPARABLE_FIELDS[0]]["corrected_value"] == "13060593"
    assert verdict["summary"] == "全部一致"


def test_parse_drops_unknown_fields_and_keeps_incorrect_value():
    fields = [
        {"field": "invoice_code", "status": "incorrect", "corrected_value": "123", "reason": ""},
        {"field": "seller_name", "status": "incorrect", "corrected_value": "正确公司", "reason": "密码区污染"},
    ]
    verdict = parse_verify_response(_verdict_payload(fields=fields))

    names = {f["field"] for f in verdict["fields"]}
    assert "invoice_code" not in names  # not a comparable field
    by_field = {f["field"]: f for f in verdict["fields"]}
    assert by_field["seller_name"]["corrected_value"] == "正确公司"
    # non-incorrect verdicts never carry an applicable value
    assert all(f["corrected_value"] is None
               for f in verdict["fields"] if f["status"] != "incorrect")


@pytest.mark.parametrize("bad", [
    "not json at all",
    '["array", "not", "object"]',
    '{"overall": "pass"}',  # missing fields array
])
def test_parse_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_verify_response(bad)


# --- build_verify_prompt ----------------------------------------------------------


def test_prompt_embeds_current_values_and_ocr_text():
    prompt = build_verify_prompt({"invoice_number": "13060593"}, "开票日期: 2025年01月01日")
    assert '"invoice_number": "13060593"' in prompt
    assert "## 发票OCR识别文本" in prompt
    assert "开票日期: 2025年01月01日" in prompt


def test_prompt_without_ocr_text_has_no_ocr_section():
    prompt = build_verify_prompt({"invoice_number": "13060593"}, None)
    assert "## 发票OCR识别文本" not in prompt
    assert '"invoice_number": "13060593"' in prompt


# --- run_ai_verify mode selection --------------------------------------------------


class _FakeLLMService:
    def __init__(self, content=None, error=None, vision=True):
        self.calls = []
        self._content = content if content is not None else _verdict_payload()
        self._error = error
        self._vision = vision

    def supports_vision(self):
        return self._vision

    def verify_completion(self, system_prompt, user_prompt, image_data, mime_type):
        self.calls.append({"image_data": image_data, "mime_type": mime_type,
                           "user_prompt": user_prompt, "system_prompt": system_prompt})
        if self._error:
            raise self._error
        return self._content


def test_vision_provider_reads_original_image_bytes():
    service = _FakeLLMService()
    outcome = run_ai_verify(service, {"invoice_number": "1"}, None, b"\x89PNGRAW", "png")

    assert outcome["mode"] == "vision"
    assert service.calls[0]["image_data"] == b"\x89PNGRAW"
    assert service.calls[0]["mime_type"] == "image/png"


def test_vision_pdf_without_poppler_falls_back_to_ocr_text(monkeypatch):
    monkeypatch.setattr(invoice_service, "_pdf_first_page_png", lambda data: None)
    service = _FakeLLMService()
    outcome = run_ai_verify(service, {"invoice_number": "1"}, "发票号码: 13060593", b"%PDF", "pdf")

    assert outcome["mode"] == "text"
    assert service.calls[0]["image_data"] is None
    assert "13060593" in service.calls[0]["user_prompt"]


def test_vision_pdf_with_render_sends_converted_page(monkeypatch):
    monkeypatch.setattr(invoice_service, "_pdf_first_page_png", lambda data: b"RENDERED")
    service = _FakeLLMService()
    outcome = run_ai_verify(service, {}, "unused", b"%PDF", "pdf")

    assert outcome["mode"] == "vision"
    assert service.calls[0]["image_data"] == b"RENDERED"


def test_text_only_provider_uses_chat_path():
    service = _FakeLLMService(vision=False)
    outcome = run_ai_verify(service, {}, "OCR 原文", b"\x89PNGRAW", "png")

    assert outcome["mode"] == "text"
    assert service.calls[0]["image_data"] is None


def test_no_image_and_no_ocr_text_is_a_client_error():
    service = _FakeLLMService(vision=False)
    with pytest.raises(ValueError):
        run_ai_verify(service, {}, "  ", b"%PDF", "pdf")
    assert service.calls == []  # no paid LLM call happens


def test_llm_failure_becomes_runtime_error():
    service = _FakeLLMService(error=ConnectionError("upstream down"))
    with pytest.raises(RuntimeError, match="LLM调用失败"):
        run_ai_verify(service, {}, "ocr text", b"\x89PNGRAW", "png")


def test_unparseable_llm_output_becomes_runtime_error():
    service = _FakeLLMService(content="垃圾输出")
    with pytest.raises(RuntimeError, match="无法解析"):
        run_ai_verify(service, {}, "ocr text", b"\x89PNGRAW", "png")


# --- LLMService.verify_completion dispatch ----------------------------------------


class _FakeClient:
    def __init__(self):
        self.vision_calls = []
        self.chat_calls = []

    def supports_vision(self):
        return True

    def vision_completion(self, system_prompt, user_prompt, image_data, mime_type):
        self.vision_calls.append((system_prompt, user_prompt, image_data, mime_type))
        return "vision-ok"

    def chat_completion(self, system_prompt, user_prompt):
        self.chat_calls.append((system_prompt, user_prompt))
        return "chat-ok"


def _service_with(client):
    service = LLMService(None)
    service._client = client
    return service


def test_verify_completion_uses_vision_when_image_given():
    client = _FakeClient()
    assert _service_with(client).verify_completion("S", "U", b"IMG", "image/jpeg") == "vision-ok"
    assert client.vision_calls == [("S", "U", b"IMG", "image/jpeg")]
    assert client.chat_calls == []


def test_verify_completion_falls_back_to_chat_without_image():
    client = _FakeClient()
    assert _service_with(client).verify_completion("S", "U") == "chat-ok"
    assert client.vision_calls == []
    assert client.chat_calls == [("S", "U")]


def test_verify_completion_rejects_image_on_visionless_client():
    class _TextOnly(_FakeClient):
        def supports_vision(self):
            return False

    service = _service_with(_TextOnly())
    with pytest.raises(RuntimeError, match="不支持图片输入"):
        service.verify_completion("S", "U", b"IMG")


def test_verify_completion_requires_available_provider():
    with pytest.raises(RuntimeError, match="No LLM provider"):
        LLMService(None).verify_completion("S", "U")
