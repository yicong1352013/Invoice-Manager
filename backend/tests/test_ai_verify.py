"""Tests for the detail-page AI verification flow ("AI校验").

Business invariants:
- The verdict table only ever contains the known comparable fields, each with a
  normalized status; overall is recomputed locally and never trusted from the
  model (a model claiming "pass" with an incorrect key field must surface issues).
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
from app.services.prompts import INVOICE_VERIFY_PROMPT, VERIFY_FIELD_LABELS


def test_verify_labels_cover_every_comparable_field():
    """The prompt must describe every verified field — a new COMPARABLE_FIELDS
    entry without a label would leave the model guessing its meaning."""
    assert set(VERIFY_FIELD_LABELS) == set(COMPARABLE_FIELDS)


def test_verify_prompt_has_optional_field_rule():
    """Optional fields absent from both the record and the invoice face must be
    judged correct, not unsure — otherwise every sparse invoice fails AI校验."""
    assert "选填字段" in INVOICE_VERIFY_PROMPT
    assert "判 correct" in INVOICE_VERIFY_PROMPT


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
    verdict = parse_verify_response(content, {})

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
    verdict = parse_verify_response(_verdict_payload(overall="pass", fields=fields), {})

    assert verdict["overall"] == "issues"
    by_field = {f["field"]: f for f in verdict["fields"]}
    assert by_field[COMPARABLE_FIELDS[0]]["corrected_value"] == "13060593"
    assert verdict["summary"] == "需处理 1 个关键问题"


def test_parse_drops_unknown_fields_and_keeps_incorrect_value():
    fields = [
        {"field": "payee", "status": "incorrect", "corrected_value": "王五", "reason": ""},  # 收款人 not a comparable field
        {"field": "seller_name", "status": "incorrect", "corrected_value": "正确公司", "reason": "密码区污染"},
        {"field": "invoice_code", "status": "incorrect", "corrected_value": "044031900111", "reason": "漏识"},
    ]
    verdict = parse_verify_response(_verdict_payload(fields=fields), {})

    names = {f["field"] for f in verdict["fields"]}
    assert "payee" not in names  # unknown fields are dropped
    by_field = {f["field"]: f for f in verdict["fields"]}
    assert by_field["seller_name"]["corrected_value"] == "正确公司"
    # invoice_code became a comparable field: its verdict and correction survive
    assert by_field["invoice_code"]["status"] == "incorrect"
    assert by_field["invoice_code"]["corrected_value"] == "044031900111"
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
        parse_verify_response(bad, {})


def _review_field(name, status, current=None, corrected="原件值"):
    fields = [{"field": field, "status": "correct"} for field in COMPARABLE_FIELDS]
    next(item for item in fields if item["field"] == name).update(
        status=status, corrected_value=corrected, severity="none",
    )
    return parse_verify_response(_verdict_payload(fields=fields), {name: current})


@pytest.mark.parametrize("name", [
    "buyer_address_phone", "buyer_bank_account", "seller_address_phone", "seller_bank_account",
    "specification", "unit", "quantity", "unit_price", "total_with_tax_cn", "remark", "drawer",
])
@pytest.mark.parametrize("status", ["incorrect", "unsure"])
def test_auxiliary_differences_warn_without_blocking(name, status):
    verdict = _review_field(name, status, current="当前值")
    field = next(item for item in verdict["fields"] if item["field"] == name)
    assert verdict["overall"] == "pass"
    assert verdict["summary"] == "审核通过，含 1 项提示"
    assert field["severity"] == "warning"
    assert field["status"] == status
    assert field["corrected_value"] == ("原件值" if status == "incorrect" else None)


@pytest.mark.parametrize("name", [
    "invoice_number", "issue_date", "buyer_name", "buyer_tax_id", "seller_name", "seller_tax_id",
    "item_name", "amount", "tax_amount", "tax_rate", "total_with_tax",
])
@pytest.mark.parametrize("status", ["incorrect", "unsure"])
def test_key_field_differences_block_even_if_model_claims_no_severity(name, status):
    verdict = _review_field(name, status, current="当前值")
    field = next(item for item in verdict["fields"] if item["field"] == name)
    assert verdict["overall"] == "issues"
    assert field["severity"] == "blocking"


@pytest.mark.parametrize("name", ["invoice_type", "invoice_code"])
@pytest.mark.parametrize("current,status,severity", [
    (None, "incorrect", "warning"), ("  ", "incorrect", "warning"),
    (None, "unsure", "warning"), ("已有值", "unsure", "warning"),
    ("已有值", "incorrect", "blocking"), ("已有值", "correct", "none"),
])
def test_optional_invoice_identity_only_blocks_explicit_conflicts(name, current, status, severity):
    verdict = _review_field(name, status, current)
    field = next(item for item in verdict["fields"] if item["field"] == name)
    assert field["severity"] == severity
    assert verdict["overall"] == ("issues" if severity == "blocking" else "pass")


@pytest.mark.parametrize("name,current,corrected", [
    ("item_name", "运输服务*客运服务费", "*运输服务*客运服务费"),
    ("item_name", "＊运输服务＊客运服务费 ", "运输服务*客运服务费"),
    ("buyer_name", " 公司（上海） ", "公司(上海)"),
    ("invoice_number", "００１２３", "00123"),
    ("invoice_code", "044031900111", " 044031900111 "),
    ("issue_date", "2026-10-24", "20261024"),
    ("total_with_tax", "113.00", "113"),
    ("quantity", "2.5000", "2.5"),
    ("unit_price", "40.0000", "40"),
])
def test_format_equivalence_needs_no_correction(name, current, corrected):
    verdict = _review_field(name, "incorrect", current, corrected)
    field = next(item for item in verdict["fields"] if item["field"] == name)
    assert verdict["overall"] == "pass"
    assert field["status"] == "correct"
    assert field["severity"] == "none"
    assert field["corrected_value"] is None


@pytest.mark.parametrize("name,current,corrected", [
    ("invoice_number", "00123", "123"),
    ("invoice_number", "01O23", "01023"),
    ("invoice_code", "044031900111", "44031900111"),
    ("item_name", "运输服务*客运服务费", "*运输服务*货运服务费"),
    ("item_name", "型号A1", "型号A2"),
    ("item_name", "型号①", "型号1"),
    ("item_name", "运输服务*客运服务费", "客运服务费"),
    ("issue_date", "2026-10-24", "2026-10-25"),
    ("issue_date", "2026-02-30", "2026-02-30"),
    ("total_with_tax", "113.00", "113.01"),
    ("total_with_tax", "NaN", "NaN"),
    ("tax_rate", "0%", "免税"),
    ("tax_rate", "免税", "不征税"),
])
def test_substantive_differences_never_become_format_equivalence(name, current, corrected):
    verdict = _review_field(name, "incorrect", current, corrected)
    field = next(item for item in verdict["fields"] if item["field"] == name)
    assert verdict["overall"] == "issues"
    assert field["severity"] == "blocking"
    assert field["corrected_value"] == corrected


def test_missing_key_verdicts_block_incomplete_model_response():
    verdict = parse_verify_response(_verdict_payload(fields=[
        {"field": "remark", "status": "correct"},
    ]), {})
    field = next(item for item in verdict["fields"] if item["field"] == "invoice_number")
    assert field["status"] == "unsure"
    assert field["severity"] == "blocking"
    assert verdict["overall"] == "issues"


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


def test_run_uses_current_values_for_policy_and_keeps_original_model_response():
    fields = [{"field": field, "status": "correct"} for field in COMPARABLE_FIELDS]
    next(item for item in fields if item["field"] == "item_name").update(
        status="incorrect", corrected_value="*运输服务*客运服务费",
    )
    next(item for item in fields if item["field"] == "quantity").update(
        status="incorrect", corrected_value="99",
    )
    original = _verdict_payload(overall="issues", fields=fields, summary="原模型认为需要人工审核")
    service = _FakeLLMService(content=original)
    outcome = run_ai_verify(service, {"item_name": "运输服务*客运服务费", "quantity": "2"}, None, b"image", "png")
    assert outcome["content"] == original
    assert outcome["verdict"]["overall"] == "pass"
    assert outcome["verdict"]["summary"] == "审核通过，含 1 项提示"
    assert len(service.calls) == 1


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
