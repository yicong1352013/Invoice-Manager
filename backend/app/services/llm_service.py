"""LLM-based invoice parsing service.

Providers are configured in the database (llm_providers table) and built into
protocol clients on demand: openai_compatible, anthropic_compatible, google.
"""

import base64
import json
import logging
import re
import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from app.models.provider import LlmProvider, ProviderProtocol
from app.services.prompts import (
    INVOICE_TEXT_PROMPT,
    INVOICE_TEXT_SYSTEM_PROMPT,
    INVOICE_VISION_PROMPT,
    INVOICE_VISION_SYSTEM_PROMPT,
    REQUIRED_FIELDS,
)

logger = logging.getLogger(__name__)


def _model_matches_vision_pattern(model_name: str, vision_patterns: list[str]) -> bool:
    """Check if a model name matches any vision pattern.

    Uses exact match or prefix match with separator to avoid false positives.
    For example, "gpt-4o" matches "gpt-4o" and "gpt-4o-mini" but not "gpt-4o-audio".
    """
    for pattern in vision_patterns:
        if model_name == pattern:
            return True
        if model_name.startswith(pattern + "-") or model_name.startswith(pattern + "."):
            return True
    return False


def _model_uses_new_token_param(model_name: str) -> bool:
    """Newer OpenAI models (o1, o3, gpt-5.x) require max_completion_tokens."""
    new_format_prefixes = ["o1", "o3", "gpt-5"]
    return any(model_name.startswith(prefix) for prefix in new_format_prefixes)


def _get_max_tokens_param(model_name: str, value: int) -> dict:
    if _model_uses_new_token_param(model_name):
        return {"max_completion_tokens": value}
    return {"max_tokens": value}


def extract_responses_text(response: Any) -> str:
    """Extract assistant text from a Responses API response.

    Works with the SDK object (output_text aggregate or output[].content[]
    walk) and with plain dicts in unit tests. Returns concatenated
    output_text parts.
    """
    convenience = getattr(response, "output_text", None)
    if convenience is None and isinstance(response, dict):
        convenience = response.get("output_text")
    if convenience:
        return convenience.strip()

    output = getattr(response, "output", None)
    if output is None and isinstance(response, dict):
        output = response.get("output") or []
    parts: list[str] = []
    for item in output or []:
        item_type = getattr(item, "type", None)
        if item_type is None and isinstance(item, dict):
            item_type = item.get("type")
        if item_type != "message":
            continue
        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content") or []
        for block in content or []:
            block_type = getattr(block, "type", None)
            if block_type is None and isinstance(block, dict):
                block_type = block.get("type")
            if block_type == "output_text":
                text = getattr(block, "text", None)
                if text is None and isinstance(block, dict):
                    text = block.get("text")
                if text:
                    parts.append(text)
    return "".join(parts).strip()


class BaseLLMClient(ABC):
    """A single configured LLM endpoint."""

    # Known vision model name patterns per protocol (fallback heuristic when
    # the provider row does not declare vision capability)
    VISION_PATTERNS: list[str] = []

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: Optional[str],
        declared_vision: bool,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        api_mode: str = "chat",
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.declared_vision = declared_vision
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.api_mode = api_mode
        self._client: Any = None
        self._lock = threading.Lock()

    def _generation_params(self, default_temperature: float, default_max_tokens: int) -> dict:
        """Generation kwargs honoring provider-configured overrides."""
        params: dict = {"temperature": self.temperature if self.temperature is not None else default_temperature}
        max_tokens = self.max_output_tokens if self.max_output_tokens is not None else default_max_tokens
        params.update(_get_max_tokens_param(self.model, max_tokens))
        return params

    def _ensure_client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = self._create_client()
        return self._client

    @abstractmethod
    def _create_client(self):
        """Create the underlying SDK client (lazy, thread-safe)."""

    @abstractmethod
    def get_protocol_name(self) -> str:
        """Protocol slug for logging."""

    @abstractmethod
    def chat_completion(self, system_prompt: str, user_prompt: str) -> str:
        """Send a text chat completion and return the response content."""

    def vision_completion(self, system_prompt: str, user_prompt: str, image_data: bytes, mime_type: str = "image/png") -> str:
        """Send a vision completion with an image; override where supported."""
        raise NotImplementedError("Vision not supported by this provider")

    def supports_vision(self) -> bool:
        """Declared capability wins; fall back to model-name heuristic."""
        if self.declared_vision:
            return True
        return _model_matches_vision_pattern(self.model, self.VISION_PATTERNS)


class OpenAICompatibleClient(BaseLLMClient):
    """OpenAI-compatible endpoint: Chat Completions or Responses API.

    api_mode 'chat' (default) covers OpenAI and most proxies (DeepSeek/GLM/
    Qwen); 'responses' targets the Responses API (e.g. DeepSeek /responses).
    """

    VISION_PATTERNS = ["gpt-4o", "gpt-4-turbo", "gpt-4-vision", "gpt-5", "o1", "o3",
                       "qwen-vl", "qwen2-vl", "qwen2.5-vl", "glm-4v", "deepseek-vl",
                       "deepseek-flash", "deepseek-v4"]

    def _create_client(self):
        from openai import OpenAI
        kwargs = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
            # Override User-Agent for custom base URLs to avoid
            # Cloudflare WAF blocking "OpenAI/Python" user agent
            kwargs["default_headers"] = {"User-Agent": "python-httpx/0.27.0"}
        return OpenAI(**kwargs)

    def get_protocol_name(self) -> str:
        return "openai_compatible"

    def _responses_params(self) -> dict:
        """Responses API kwargs (max_output_tokens is the native param there)."""
        params: dict = {}
        if self.temperature is not None:
            params["temperature"] = self.temperature
        if self.max_output_tokens is not None:
            params["max_output_tokens"] = self.max_output_tokens
        return params

    def chat_completion(self, system_prompt: str, user_prompt: str) -> str:
        if self.api_mode == "responses":
            response = self._ensure_client().responses.create(
                model=self.model,
                instructions=system_prompt,
                input=[{"role": "user", "content": [{"type": "input_text", "text": user_prompt}]}],
                **self._responses_params(),
            )
            return extract_responses_text(response)

        response = self._ensure_client().chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            **self._generation_params(default_temperature=0.1, default_max_tokens=2000),
        )
        return response.choices[0].message.content.strip()

    def vision_completion(self, system_prompt: str, user_prompt: str, image_data: bytes, mime_type: str = "image/png") -> str:
        base64_image = base64.b64encode(image_data).decode("utf-8")
        if self.api_mode == "responses":
            response = self._ensure_client().responses.create(
                model=self.model,
                instructions=system_prompt,
                input=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": user_prompt},
                        {"type": "input_image", "image_url": f"data:{mime_type};base64,{base64_image}"},
                    ],
                }],
                **self._responses_params(),
            )
            return extract_responses_text(response)

        response = self._ensure_client().chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}
                        }
                    ]
                }
            ],
            **self._generation_params(default_temperature=0.1, default_max_tokens=2000),
        )
        return response.choices[0].message.content.strip()


class AnthropicCompatibleClient(BaseLLMClient):
    """Anthropic Messages API."""

    VISION_PATTERNS = ["claude-3", "claude-4", "claude-sonnet", "claude-opus", "claude-haiku"]

    def _create_client(self):
        import anthropic
        kwargs = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return anthropic.Anthropic(**kwargs)

    def get_protocol_name(self) -> str:
        return "anthropic_compatible"

    def _messages_params(self) -> dict:
        params: dict = {"max_tokens": self.max_output_tokens if self.max_output_tokens is not None else 2000}
        if self.temperature is not None:
            params["temperature"] = self.temperature
        return params

    def chat_completion(self, system_prompt: str, user_prompt: str) -> str:
        response = self._ensure_client().messages.create(
            model=self.model,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            **self._messages_params(),
        )
        return response.content[0].text.strip()

    def vision_completion(self, system_prompt: str, user_prompt: str, image_data: bytes, mime_type: str = "image/png") -> str:
        base64_image = base64.b64encode(image_data).decode("utf-8")
        response = self._ensure_client().messages.create(
            model=self.model,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": base64_image
                            }
                        },
                        {"type": "text", "text": user_prompt}
                    ]
                }
            ],
            **self._messages_params(),
        )
        return response.content[0].text.strip()


class GoogleClient(BaseLLMClient):
    """Google Gemini native SDK."""

    VISION_PATTERNS = ["gemini-1.5", "gemini-2", "gemini-pro-vision"]

    def __init__(self, model: str, api_key: str, base_url: Optional[str], declared_vision: bool):
        super().__init__(model, api_key, base_url, declared_vision)
        self._genai = None
        self._genai_lock = threading.Lock()

    def _ensure_genai(self):
        if self._genai is None:
            with self._genai_lock:
                if self._genai is None:
                    import google.generativeai as genai
                    configure_kwargs = {"api_key": self.api_key}
                    if self.base_url:
                        configure_kwargs["client_options"] = {"api_endpoint": self.base_url}
                    genai.configure(**configure_kwargs)
                    self._genai = genai
        return self._genai

    def _create_client(self):
        return self._ensure_genai().GenerativeModel(self.model)

    def get_protocol_name(self) -> str:
        return "google"

    def _generate_kwargs(self) -> dict:
        generation_config: dict = {}
        if self.temperature is not None:
            generation_config["temperature"] = self.temperature
        if self.max_output_tokens is not None:
            generation_config["max_output_tokens"] = self.max_output_tokens
        return {"generation_config": generation_config} if generation_config else {}

    def chat_completion(self, system_prompt: str, user_prompt: str) -> str:
        full_prompt = f"{system_prompt}\n\n{user_prompt}"
        response = self._ensure_client().generate_content(full_prompt, **self._generate_kwargs())
        return response.text.strip()

    def vision_completion(self, system_prompt: str, user_prompt: str, image_data: bytes, mime_type: str = "image/png") -> str:
        from PIL import Image
        from io import BytesIO

        image = Image.open(BytesIO(image_data))
        full_prompt = f"{system_prompt}\n\n{user_prompt}"
        response = self._ensure_client().generate_content([full_prompt, image], **self._generate_kwargs())
        return response.text.strip()


def build_client(row: Optional[LlmProvider]) -> Optional[BaseLLMClient]:
    """Build a protocol client from a provider row, or None if unusable."""
    if row is None:
        return None
    if not row.api_key or not row.model:
        return None

    clients = {
        ProviderProtocol.OPENAI_COMPATIBLE: OpenAICompatibleClient,
        ProviderProtocol.ANTHROPIC_COMPATIBLE: AnthropicCompatibleClient,
        ProviderProtocol.GOOGLE: GoogleClient,
    }
    client_class = clients.get(row.protocol)
    if client_class is None:
        logger.error(f"Unknown provider protocol: {row.protocol}")
        return None
    return client_class(
        model=row.model,
        api_key=row.api_key,
        base_url=row.base_url or None,
        declared_vision=bool(row.vision),
        temperature=getattr(row, "temperature", None),
        max_output_tokens=getattr(row, "max_output_tokens", None),
        api_mode=getattr(row, "api_mode", None) or "chat",
    )


class LLMService:
    """Invoice parsing facade bound to one provider row."""

    def __init__(self, provider_row: Optional[LlmProvider] = None):
        self._row = provider_row
        self._client: Optional[BaseLLMClient] = None
        if provider_row is not None:
            self._client = build_client(provider_row)

    @property
    def is_available(self) -> bool:
        return self._client is not None

    @property
    def provider_name(self) -> Optional[str]:
        return self._row.name if self._row else None

    def supports_vision(self) -> bool:
        return self._client.supports_vision() if self._client else False

    def parse_invoice_from_image(self, image_data: bytes, mime_type: str = "image/png") -> Dict[str, Any]:
        """Parse invoice directly from image using vision capabilities."""
        if not self.is_available:
            logger.warning("No LLM provider configured, skipping vision parsing")
            return {}

        if not self._client.supports_vision():
            logger.warning(f"Provider {self._client.get_protocol_name()} model does not support vision")
            return {}

        try:
            content = self._client.vision_completion(
                INVOICE_VISION_SYSTEM_PROMPT,
                INVOICE_VISION_PROMPT,
                image_data,
                mime_type
            )
            return self._parse_json_response(content, self._client.get_protocol_name())
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM vision response as JSON: {e}")
            return {}
        except Exception as e:
            logger.error(f"LLM vision parsing failed ({self._client.get_protocol_name()}): {e}")
            return {}

    def parse_invoice_from_text(self, raw_text: str) -> Dict[str, Any]:
        """Parse invoice fields from OCR raw text (fallback for text-only models)."""
        if not self.is_available:
            logger.warning("No LLM provider configured, skipping text parsing")
            return {}

        if not raw_text or not raw_text.strip():
            return {}

        try:
            prompt = INVOICE_TEXT_PROMPT.replace("{ocr_text}", raw_text)
            content = self._client.chat_completion(INVOICE_TEXT_SYSTEM_PROMPT, prompt)
            return self._parse_json_response(content, self._client.get_protocol_name())
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM text response as JSON: {e}")
            return {}
        except Exception as e:
            logger.error(f"LLM text parsing failed ({self._client.get_protocol_name()}): {e}")
            return {}

    def _normalize_field_value(self, field_name: str, value: Any) -> Optional[str]:
        """Normalize and validate a single field value."""
        if value is None:
            return None

        if isinstance(value, (int, float)):
            value = str(value)
        if not isinstance(value, str):
            return None

        cleaned = value.strip()
        if not cleaned:
            return None

        if field_name in ['total_with_tax', 'amount', 'tax_amount']:
            cleaned = re.sub(r'[¥￥$€,，\s]', '', cleaned)
            match = re.search(r'(\d+(?:\.\d{1,2})?)', cleaned)
            return match.group(1) if match else None

        if field_name == 'issue_date':
            match = re.search(r'(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})', cleaned)
            if match:
                year, month, day = match.groups()
                return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', cleaned):
                return cleaned
            return None

        if field_name == 'invoice_number':
            cleaned = re.sub(r'[\s-]+', '', cleaned)
            return cleaned if re.fullmatch(r'\d{8,20}', cleaned) else None

        if field_name == 'invoice_code':
            cleaned = re.sub(r'[\s-]+', '', cleaned)
            return cleaned if re.fullmatch(r'\d{10,12}', cleaned) else None

        if field_name in ['buyer_tax_id', 'seller_tax_id']:
            cleaned = re.sub(r'\s+', '', cleaned.upper())
            return cleaned if re.fullmatch(r'[A-Z0-9]{15,20}', cleaned) else None

        if field_name == 'tax_rate':
            if cleaned == '免税':
                return cleaned
            return cleaned if re.fullmatch(r'\d+%', cleaned) else None

        return cleaned

    def _parse_json_response(self, content: str, provider_name: str) -> Dict[str, Any]:
        """Parse JSON from LLM response with schema validation and data cleaning."""
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        raw_fields = json.loads(content)
        if not isinstance(raw_fields, dict):
            raise json.JSONDecodeError("LLM response is not a JSON object", content, 0)

        fields: Dict[str, Optional[str]] = {}
        for field in REQUIRED_FIELDS:
            fields[field] = self._normalize_field_value(field, raw_fields.get(field))

        logger.info(f"LLM ({provider_name}) extracted fields: {list(fields.keys())}")
        return fields
