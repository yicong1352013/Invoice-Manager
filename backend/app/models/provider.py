from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, Enum as SQLEnum

from app.database import Base


class ProviderProtocol(str, Enum):
    OPENAI_COMPATIBLE = "openai_compatible"      # OpenAI 及一切兼容端点（DeepSeek/GLM/千问/自定义中转）
    ANTHROPIC_COMPATIBLE = "anthropic_compatible"  # Anthropic Messages API
    GOOGLE = "google"                            # Google Gemini 原生


class LlmProvider(Base):
    """A configured LLM provider endpoint (preset or custom)."""

    __tablename__ = "llm_providers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, index=True, nullable=False)  # stable slug
    display_name = Column(String(100), nullable=False)
    protocol = Column(SQLEnum(ProviderProtocol), nullable=False)
    base_url = Column(String(500), nullable=True)
    api_key = Column(String(500), nullable=True)
    model = Column(String(200), nullable=True)
    vision = Column(Boolean, default=False, nullable=False)  # declared image-input capability
    is_preset = Column(Boolean, default=False, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class AppSetting(Base):
    """Simple key-value store for runtime system settings."""

    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)


# Well-known app setting keys
SETTING_ACTIVE_PROVIDER_ID = "active_provider_id"
SETTING_OCR_ENGINE = "ocr_engine"  # 'local' | 'baidu' (consumed from Phase 3)
SETTING_BAIDU_OCR_API_KEY = "baidu_ocr_api_key"
SETTING_BAIDU_OCR_SECRET_KEY = "baidu_ocr_secret_key"
SETTING_MCP_ENABLED = "mcp_enabled"  # consumed from Phase 6
SETTING_MCP_TOKEN = "mcp_token"
SETTING_PROVIDERS_MIGRATED = "providers_migrated"
