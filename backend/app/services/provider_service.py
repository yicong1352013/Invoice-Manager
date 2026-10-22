"""Provider configuration service: DB-backed LLM providers and app settings.

Handles preset seeding, one-time migration of legacy .env configuration, and
active-provider resolution. Runtime configuration lives here; .env keeps only
bootstrap values (database URL, JWT secret, initial admin).
"""

import logging
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.provider import (
    AppSetting,
    LlmProvider,
    ProviderProtocol,
    SETTING_ACTIVE_PROVIDER_ID,
    SETTING_BAIDU_OCR_API_KEY,
    SETTING_BAIDU_OCR_SECRET_KEY,
    SETTING_MCP_ENABLED,
    SETTING_MCP_TOKEN,
    SETTING_OCR_ENGINE,
    SETTING_PROVIDERS_MIGRATED,
)

logger = logging.getLogger(__name__)

# Preset providers seeded on first startup. Keys stay empty until configured.
PRESET_PROVIDERS: List[dict] = [
    {
        "name": "openai",
        "display_name": "OpenAI (GPT)",
        "protocol": ProviderProtocol.OPENAI_COMPATIBLE,
        "base_url": "",
        "model": "gpt-4o-mini",
        "vision": True,
    },
    {
        "name": "anthropic",
        "display_name": "Anthropic (Claude)",
        "protocol": ProviderProtocol.ANTHROPIC_COMPATIBLE,
        "base_url": "",
        "model": "claude-3-5-sonnet-20241022",
        "vision": True,
    },
    {
        "name": "deepseek",
        "display_name": "DeepSeek",
        "protocol": ProviderProtocol.OPENAI_COMPATIBLE,
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "vision": True,
    },
    {
        "name": "zhipu",
        "display_name": "智谱 (GLM)",
        "protocol": ProviderProtocol.OPENAI_COMPATIBLE,
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4v-flash",
        "vision": True,
    },
    {
        "name": "qwen",
        "display_name": "阿里云 (通义千问)",
        "protocol": ProviderProtocol.OPENAI_COMPATIBLE,
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-plus",
        "vision": True,
    },
    {
        "name": "google",
        "display_name": "Google (Gemini)",
        "protocol": ProviderProtocol.GOOGLE,
        "base_url": "",
        "model": "gemini-1.5-flash",
        "vision": True,
    },
]

# Legacy .env field mapping for the one-time migration
_LEGACY_ENV_MAP = {
    "openai": ("openai_api_key", "openai_model", "openai_base_url"),
    "anthropic": ("anthropic_api_key", "anthropic_model", "anthropic_base_url"),
    "google": ("google_api_key", "google_model", "google_base_url"),
    "qwen": ("qwen_api_key", "qwen_model", "qwen_base_url"),
    "deepseek": ("deepseek_api_key", "deepseek_model", "deepseek_base_url"),
    "zhipu": ("zhipu_api_key", "zhipu_model", "zhipu_base_url"),
}


def mask_secret(secret: Optional[str]) -> Optional[str]:
    """Mask a secret, keeping the last 4 characters for recognition.
    Short secrets are fully masked so most of the value is never revealed."""
    if not secret:
        return None
    if len(secret) <= 8:
        return "****"
    return f"****{secret[-4:]}"


async def get_setting(db: AsyncSession, key: str, default: Optional[str] = None) -> Optional[str]:
    row = await db.get(AppSetting, key)
    return row.value if row is not None else default


async def set_setting(db: AsyncSession, key: str, value: Optional[str]) -> None:
    row = await db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value


async def list_providers(db: AsyncSession) -> List[LlmProvider]:
    result = await db.execute(select(LlmProvider).order_by(LlmProvider.sort_order, LlmProvider.id))
    return list(result.scalars().all())


async def get_active_provider_row(db: AsyncSession) -> Optional[LlmProvider]:
    """Resolve the active provider row, or None when not configured."""
    active_id_raw = await get_setting(db, SETTING_ACTIVE_PROVIDER_ID)
    if active_id_raw:
        try:
            row = await db.get(LlmProvider, int(active_id_raw))
        except ValueError:
            row = None
        if row is not None and row.enabled:
            return row

    # Fall back to the first enabled provider that has credentials
    providers = await list_providers(db)
    for provider in providers:
        if provider.enabled and provider.api_key and provider.model:
            return provider
    return None


async def ensure_provider_seeded(db: AsyncSession) -> None:
    """Seed preset providers and run the one-time legacy .env migration."""

    existing = await list_providers(db)
    by_name = {p.name: p for p in existing}

    for index, preset in enumerate(PRESET_PROVIDERS):
        if preset["name"] in by_name:
            continue
        db.add(LlmProvider(
            name=preset["name"],
            display_name=preset["display_name"],
            protocol=preset["protocol"],
            base_url=preset["base_url"] or None,
            model=preset["model"],
            vision=preset["vision"],
            is_preset=True,
            enabled=True,
            sort_order=index,
        ))
    await db.flush()

    # Re-list so freshly seeded presets are visible to the migration
    by_name = {p.name: p for p in await list_providers(db)}

    migrated = await get_setting(db, SETTING_PROVIDERS_MIGRATED)
    if migrated != "1":
        _migrate_legacy_env(db, by_name)
        await set_setting(db, SETTING_PROVIDERS_MIGRATED, "1")

    # Defaults for settings consumed by later phases
    if await get_setting(db, SETTING_OCR_ENGINE) is None:
        await set_setting(db, SETTING_OCR_ENGINE, "local")
    if await get_setting(db, SETTING_MCP_ENABLED) is None:
        await set_setting(db, SETTING_MCP_ENABLED, "false")
    # Baidu/MCP credentials default to unset

    await db.commit()


def _migrate_legacy_env(db: AsyncSession, by_name: dict) -> None:
    """Import legacy .env LLM configuration into provider rows (run once)."""
    settings = get_settings()
    active = settings.get_active_llm_provider()

    for slug, (key_attr, model_attr, url_attr) in _LEGACY_ENV_MAP.items():
        row = by_name.get(slug)
        if row is None:
            continue  # preset not seeded (unexpected) — skip
        api_key = getattr(settings, key_attr, "") or ""
        if not api_key or api_key.startswith("your_"):
            continue
        row.api_key = api_key
        model = getattr(settings, model_attr, "") or ""
        if model:
            row.model = model
        base_url = getattr(settings, url_attr, "") or ""
        if base_url:
            row.base_url = base_url
        logger.info(f"Migrated legacy .env config for provider '{slug}'")

    if active and active in by_name and by_name[active].api_key:
        row = by_name[active]
        # async in a sync helper: set via session add pattern
        db.add(AppSetting(key=SETTING_ACTIVE_PROVIDER_ID, value=str(row.id)))
        logger.info(f"Migrated legacy active provider: {active}")


async def load_llm_service(db: AsyncSession):
    """Build an LLMService for the currently active provider."""
    from app.services.llm_service import LLMService

    row = await get_active_provider_row(db)
    return LLMService(row)
