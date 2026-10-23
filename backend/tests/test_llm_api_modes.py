"""Tests for provider api_mode (Chat Completions vs Responses API) and
configurable generation parameters.

Business invariants:
- 'responses' mode must reach responses.create with instructions + input items
  and parse output[].content[].text — the DeepSeek Responses contract.
- Unconfigured parameters must fall back to the historical defaults; configured
  ones must be forwarded; None must never be sent as an explicit value.
- The model list fetcher must follow each protocol's auth convention.
"""

import asyncio
from types import SimpleNamespace

import httpx

from app.models.provider import ProviderProtocol
from app.services.llm_service import (
    AnthropicCompatibleClient,
    OpenAICompatibleClient,
    build_client,
    extract_responses_text,
)
from app.routers.settings import (
    _fetch_models_anthropic,
    _fetch_models_openai_compatible,
)


# --- extract_responses_text ---------------------------------------------------


def test_extract_responses_text_walks_output_items():
    """DeepSeek's Responses API returns text inside output[].content[].text;
    non-message items (reasoning, usage summaries) must be ignored."""
    payload = {
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": '{"invoice_number": '},
                    {"type": "output_text", "text": '"12345678"}'},
                    {"type": "refusal", "refusal": ""},
                ],
            },
        ]
    }
    assert extract_responses_text(payload) == '{"invoice_number": "12345678"}'


def test_extract_responses_text_prefers_output_text_aggregate():
    payload = {"output_text": "  aggregated  "}
    assert extract_responses_text(payload) == "aggregated"


def test_extract_responses_text_empty_response():
    assert extract_responses_text({"output": []}) == ""


# --- responses mode dispatch --------------------------------------------------


class _FakeResponsesAPI:
    def __init__(self, text: str):
        self.captured = None
        self._text = text
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.captured = kwargs
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": self._text}]}]}


def _openai_client(api_mode="chat", temperature=None, max_output_tokens=None):
    client = OpenAICompatibleClient(
        model="deepseek-flash",
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        declared_vision=True,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        api_mode=api_mode,
    )
    return client


def test_responses_mode_uses_instructions_and_input_items():
    fake = _FakeResponsesAPI("pong")
    client = _openai_client(api_mode="responses")
    client._client = fake  # bypass lazy SDK init

    result = client.chat_completion("SYS", "hello")

    assert result == "pong"
    assert fake.captured["model"] == "deepseek-flash"
    assert fake.captured["instructions"] == "SYS"
    assert fake.captured["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "hello"}]}
    ]
    # unconfigured params must not be sent at all (provider defaults apply)
    assert "temperature" not in fake.captured
    assert "max_output_tokens" not in fake.captured


def test_responses_mode_forwards_configured_params():
    fake = _FakeResponsesAPI("pong")
    client = _openai_client(api_mode="responses", temperature=0.7, max_output_tokens=4096)
    client._client = fake

    client.chat_completion("SYS", "hello")

    assert fake.captured["temperature"] == 0.7
    assert fake.captured["max_output_tokens"] == 4096


def test_responses_mode_vision_sends_data_url_image():
    fake = _FakeResponsesAPI("ok")
    client = _openai_client(api_mode="responses")
    client._client = fake

    result = client.vision_completion("SYS", "describe", b"\x89PNG", "image/png")

    assert result == "ok"
    content = fake.captured["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "describe"}
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")


# --- chat mode defaults and overrides ------------------------------------------


class _FakeChatCompletions:
    def __init__(self):
        self.captured = None

    def create(self, **kwargs):
        self.captured = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=" hi "))])


def test_chat_mode_keeps_historical_defaults():
    chat = _FakeChatCompletions()
    client = _openai_client()
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=chat))

    assert client.chat_completion("SYS", "hi") == "hi"
    assert chat.captured["temperature"] == 0.1
    assert chat.captured["max_tokens"] == 2000


def test_chat_mode_overrides_forwarded():
    chat = _FakeChatCompletions()
    client = _openai_client(temperature=0.3, max_output_tokens=1024)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=chat))

    client.chat_completion("SYS", "hi")

    assert chat.captured["temperature"] == 0.3
    assert chat.captured["max_tokens"] == 1024


def test_chat_mode_new_token_models_use_max_completion_tokens():
    chat = _FakeChatCompletions()
    client = OpenAICompatibleClient(
        model="o3-mini", api_key="k", base_url=None, declared_vision=True,
        max_output_tokens=512,
    )
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=chat))

    client.chat_completion("SYS", "hi")

    assert chat.captured["max_completion_tokens"] == 512
    assert "max_tokens" not in chat.captured


# --- anthropic params -----------------------------------------------------------


def test_anthropic_defaults_and_overrides():
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(text=" ok ")])

    client = AnthropicCompatibleClient(
        model="claude-3-5-sonnet-20241022", api_key="k", base_url=None, declared_vision=True,
    )
    client._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    assert client.chat_completion("SYS", "hi") == "ok"
    assert captured["max_tokens"] == 2000
    assert "temperature" not in captured  # Anthropic default applies

    client2 = AnthropicCompatibleClient(
        model="claude-3-5-sonnet-20241022", api_key="k", base_url=None, declared_vision=True,
        temperature=0.2, max_output_tokens=800,
    )
    client2._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    client2.chat_completion("SYS", "hi")
    assert captured["max_tokens"] == 800
    assert captured["temperature"] == 0.2


# --- build_client passthrough ----------------------------------------------------


def test_build_client_passes_mode_and_params():
    row = SimpleNamespace(
        protocol=ProviderProtocol.OPENAI_COMPATIBLE,
        api_key="sk-x",
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
        vision=True,
        api_mode="responses",
        temperature=0.4,
        max_output_tokens=2048,
    )
    client = build_client(row)
    assert isinstance(client, OpenAICompatibleClient)
    assert client.api_mode == "responses"
    assert client.temperature == 0.4
    assert client.max_output_tokens == 2048


def test_build_client_defaults_when_columns_absent():
    """Older rows (or test doubles) without the new columns must still build."""
    row = SimpleNamespace(
        protocol=ProviderProtocol.OPENAI_COMPATIBLE,
        api_key="sk-x",
        model="gpt-4o-mini",
        base_url=None,
        vision=False,
    )
    client = build_client(row)
    assert client.api_mode == "chat"
    assert client.temperature is None


# --- model list fetching ----------------------------------------------------------


def test_fetch_models_openai_uses_bearer_and_parses_ids(monkeypatch):
    urls = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-flash"}, {"no_id": True}]}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            urls.append((url, headers))
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    models = asyncio.run(_fetch_models_openai_compatible("https://api.deepseek.com", "sk-test"))

    assert models == ["deepseek-chat", "deepseek-flash"]
    url, headers = urls[0]
    assert url == "https://api.deepseek.com/models"
    assert headers == {"Authorization": "Bearer sk-test"}


def test_fetch_models_openai_retries_under_v1_on_404(monkeypatch):
    urls = []

    class _NotFound:
        status_code = 404

        def raise_for_status(self):
            raise AssertionError("404 must not raise before retry")

        def json(self):
            return {}

    class _Ok:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "m1"}]}

    responses = [_NotFound(), _Ok()]

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            urls.append(url)
            return responses.pop(0) if len(responses) > 1 else responses[0]

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    models = asyncio.run(_fetch_models_openai_compatible("https://proxy.example.com", "sk-test"))

    assert models == ["m1"]
    assert urls == [
        "https://proxy.example.com/models",
        "https://proxy.example.com/v1/models",
    ]


def test_fetch_models_anthropic_uses_api_key_header(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "claude-3-5-sonnet-20241022"}]}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    models = asyncio.run(_fetch_models_anthropic("https://api.anthropic.com", "sk-ant"))

    assert models == ["claude-3-5-sonnet-20241022"]
    assert captured["url"] == "https://api.anthropic.com/v1/models"
    assert captured["headers"]["x-api-key"] == "sk-ant"
    assert "anthropic-version" in captured["headers"]
