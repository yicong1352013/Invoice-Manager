"""Settings API: LLM provider management, app settings (OCR engine, MCP).

All read endpoints require an authenticated user; write endpoints require an
admin (or the legacy LLM_CONFIG_TOKEN as an alternative bearer credential).
"""

import logging
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models.provider import LlmProvider, ProviderProtocol
from app.models.user import User, UserRole
from app.services.provider_service import (
    get_active_provider_row,
    get_setting,
    list_providers,
    mask_secret,
    set_setting,
)
from app.models.provider import (
    SETTING_BAIDU_OCR_API_KEY,
    SETTING_BAIDU_OCR_SECRET_KEY,
    SETTING_MCP_ENABLED,
    SETTING_MCP_TOKEN,
    SETTING_OCR_ENGINE,
)
from app.services.model_registry import get_models_with_fallback

logger = logging.getLogger(__name__)
router = APIRouter()


_bearer_scheme = HTTPBearer(auto_error=False)


async def require_settings_write(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Admin gate for settings writes. LLM_CONFIG_TOKEN (if configured) is
    accepted as an alternative bearer credential for API automation."""
    raw_token = credentials.credentials if credentials else None
    config_token = os.environ.get("LLM_CONFIG_TOKEN", "")
    if config_token and raw_token == config_token:
        return User(username="llm-config-token", role=UserRole.ADMIN, enabled=True)

    user = await get_current_user(credentials=credentials, token=None, db=db)
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# --- schemas -----------------------------------------------------------------


class ProviderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    display_name: str
    protocol: ProviderProtocol
    base_url: Optional[str] = None
    model: Optional[str] = None
    vision: bool
    api_mode: str = "chat"
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    is_preset: bool
    enabled: bool
    api_key_masked: Optional[str] = None
    has_api_key: bool = False


class ProviderCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=50, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    display_name: str = Field(min_length=1, max_length=100)
    protocol: ProviderProtocol
    base_url: Optional[str] = Field(default=None, max_length=500)
    api_key: Optional[str] = Field(default=None, max_length=500)
    model: Optional[str] = Field(default=None, max_length=200)
    vision: bool = False
    api_mode: str = Field(default="chat", pattern=r"^(chat|responses)$")
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_output_tokens: Optional[int] = Field(default=None, ge=1)


class ProviderUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    base_url: Optional[str] = None
    api_key: Optional[str] = None  # write-only: empty/None = keep current
    model: Optional[str] = None
    vision: Optional[bool] = None
    enabled: Optional[bool] = None
    api_mode: Optional[str] = Field(default=None, pattern=r"^(chat|responses)$")
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_output_tokens: Optional[int] = Field(default=None, ge=1)


class ProviderTestRequest(BaseModel):
    protocol: ProviderProtocol
    api_key: str = Field(min_length=1)
    model: str
    base_url: Optional[str] = None
    api_mode: str = Field(default="chat", pattern=r"^(chat|responses)$")
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_output_tokens: Optional[int] = Field(default=None, ge=1)


class ProviderTestResponse(BaseModel):
    success: bool
    message: str
    response_time_ms: Optional[int] = None


class LLMStatusResponse(BaseModel):
    is_configured: bool
    active_provider: Optional[ProviderOut] = None


class AppSettingsOut(BaseModel):
    ocr_engine: str
    baidu_ocr_api_key_masked: Optional[str] = None
    has_baidu_ocr_api_key: bool = False
    baidu_ocr_secret_key_masked: Optional[str] = None
    has_baidu_ocr_secret_key: bool = False
    mcp_enabled: bool
    mcp_token_masked: Optional[str] = None
    has_mcp_token: bool = False


class AppSettingsUpdateRequest(BaseModel):
    ocr_engine: Optional[str] = None  # 'local' | 'baidu'
    baidu_ocr_api_key: Optional[str] = None  # empty/None = keep
    baidu_ocr_secret_key: Optional[str] = None
    mcp_enabled: Optional[bool] = None
    mcp_token: Optional[str] = None


def _provider_out(row: LlmProvider) -> ProviderOut:
    out = ProviderOut.model_validate(row)
    out.api_key_masked = mask_secret(row.api_key)
    out.has_api_key = bool(row.api_key)
    return out


# --- LLM status (any authenticated user) -------------------------------------


@router.get("/llm/status", response_model=LLMStatusResponse)
async def get_llm_status(
    _current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get LLM configuration status (drives the frontend promo banner)."""
    active = await get_active_provider_row(db)
    configured = active is not None and bool(active.api_key) and bool(active.model)
    return LLMStatusResponse(
        is_configured=configured,
        active_provider=_provider_out(active) if active else None,
    )


# --- Provider CRUD (admin) ---------------------------------------------------


@router.get("/providers", response_model=List[ProviderOut])
async def list_llm_providers(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all providers (preset and custom) with masked keys."""
    active = await get_active_provider_row(db)
    rows = await list_providers(db)
    return [_provider_out(r) for r in rows]


@router.post("/providers", response_model=ProviderOut)
async def create_provider(
    body: ProviderCreateRequest,
    _admin: User = Depends(require_settings_write),
    db: AsyncSession = Depends(get_db),
):
    """Add a custom provider (OpenAI/Anthropic compatible endpoint)."""
    existing = await db.execute(select(LlmProvider).where(LlmProvider.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"供应商标识已存在: {body.name}")

    row = LlmProvider(
        name=body.name,
        display_name=body.display_name,
        protocol=body.protocol,
        base_url=(body.base_url or "").strip() or None,
        api_key=(body.api_key or "").strip() or None,
        model=(body.model or "").strip() or None,
        vision=body.vision,
        api_mode=body.api_mode,
        temperature=body.temperature,
        max_output_tokens=body.max_output_tokens,
        is_preset=False,
        enabled=True,
        sort_order=100,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _provider_out(row)


@router.patch("/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(
    provider_id: int,
    body: ProviderUpdateRequest,
    _admin: User = Depends(require_settings_write),
    db: AsyncSession = Depends(get_db),
):
    """Update a provider. Secret fields are write-only (empty = keep)."""
    row = await db.get(LlmProvider, provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="供应商不存在")

    if body.display_name is not None:
        row.display_name = body.display_name
    if body.base_url is not None:
        row.base_url = body.base_url.strip() or None
    if body.api_key:  # empty/None keeps the current key
        row.api_key = body.api_key.strip()
    if body.model is not None:
        row.model = body.model.strip() or None
    if body.vision is not None:
        row.vision = body.vision
    if body.enabled is not None:
        row.enabled = body.enabled
    # Numeric overrides use model_fields_set so an explicit null clears the
    # value while an absent field keeps it.
    if body.api_mode is not None:
        row.api_mode = body.api_mode
    if "temperature" in body.model_fields_set:
        row.temperature = body.temperature
    if "max_output_tokens" in body.model_fields_set:
        row.max_output_tokens = body.max_output_tokens

    await db.commit()
    await db.refresh(row)
    return _provider_out(row)


@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: int,
    _admin: User = Depends(require_settings_write),
    db: AsyncSession = Depends(get_db),
):
    """Delete a custom provider (presets cannot be deleted, only cleared)."""
    row = await db.get(LlmProvider, provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="供应商不存在")
    if row.is_preset:
        raise HTTPException(status_code=400, detail="预置供应商不能删除，只能清空密钥或停用")

    active_id = await get_setting(db, "active_provider_id")
    await db.delete(row)
    if active_id == str(provider_id):
        await set_setting(db, "active_provider_id", None)
    await db.commit()
    return {"message": f"已删除供应商 {row.name}"}


@router.post("/providers/{provider_id}/activate", response_model=ProviderOut)
async def activate_provider(
    provider_id: int,
    _admin: User = Depends(require_settings_write),
    db: AsyncSession = Depends(get_db),
):
    """Set the active provider used by the recognition pipeline."""
    row = await db.get(LlmProvider, provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="供应商不存在")
    if not row.enabled:
        raise HTTPException(status_code=400, detail="供应商已停用，请先启用")
    if not row.api_key or not row.model:
        raise HTTPException(status_code=400, detail="请先填写该供应商的 API Key 和模型")

    await set_setting(db, "active_provider_id", str(provider_id))
    await db.commit()
    return _provider_out(row)


@router.post("/providers/test", response_model=ProviderTestResponse)
async def test_provider_config(
    body: ProviderTestRequest,
    _admin: User = Depends(require_settings_write),
):
    """Test an (unsaved) provider configuration with the real client library."""
    import time

    from app.services.llm_service import (
        AnthropicCompatibleClient,
        GoogleClient,
        OpenAICompatibleClient,
    )

    client_cls = {
        ProviderProtocol.OPENAI_COMPATIBLE: OpenAICompatibleClient,
        ProviderProtocol.ANTHROPIC_COMPATIBLE: AnthropicCompatibleClient,
        ProviderProtocol.GOOGLE: GoogleClient,
    }[body.protocol]

    base_url = body.base_url.strip().rstrip("/") if body.base_url else None
    client = client_cls(
        model=body.model.strip(),
        api_key=body.api_key.strip(),
        base_url=base_url,
        declared_vision=True,
        temperature=body.temperature,
        max_output_tokens=body.max_output_tokens,
        api_mode=body.api_mode,
    )

    start = time.monotonic()
    try:
        result_text = client.chat_completion(
            "You are a helpful assistant.",
            "Reply with exactly: OK"
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ProviderTestResponse(
            success=True,
            message=f"连接成功，响应: {result_text[:50]}",
            response_time_ms=elapsed_ms,
        )
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        error_msg = str(e)
        if "401" in error_msg or "Unauthorized" in error_msg or "authentication" in error_msg.lower():
            msg = "API密钥无效或未授权"
        elif "403" in error_msg or "Forbidden" in error_msg:
            msg = "API密钥权限不足或访问被拒绝"
        elif "404" in error_msg or "Not Found" in error_msg:
            msg = "API地址无效，请检查URL是否正确（OpenAI兼容接口的 base_url 通常需要包含 /v1 路径）"
        elif "connect" in error_msg.lower() or "timeout" in error_msg.lower():
            msg = "无法连接到该地址，请检查URL和网络连接"
        elif "<!DOCTYPE" in error_msg or "<html" in error_msg.lower():
            msg = "API地址返回了网页而非API响应，请检查URL是否正确"
        elif "model" in error_msg.lower() and ("not found" in error_msg.lower() or "does not exist" in error_msg.lower()):
            msg = "模型不存在，请检查模型名称是否正确"
        else:
            msg = f"测试失败: {error_msg.split(chr(10))[0][:150]}"
        logger.error(f"Provider config test failed ({body.protocol}): {e}")
        return ProviderTestResponse(success=False, message=msg, response_time_ms=elapsed_ms)


@router.post("/providers/{provider_id}/test", response_model=ProviderTestResponse)
async def test_saved_provider(
    provider_id: int,
    _admin: User = Depends(require_settings_write),
    db: AsyncSession = Depends(get_db),
):
    """Test a saved provider's stored configuration."""
    row = await db.get(LlmProvider, provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="供应商不存在")
    if not row.api_key or not row.model:
        raise HTTPException(status_code=400, detail="请先填写该供应商的 API Key 和模型")

    return await test_provider_config(
        body=ProviderTestRequest(
            protocol=row.protocol,
            api_key=row.api_key,
            model=row.model,
            base_url=row.base_url,
            api_mode=getattr(row, "api_mode", None) or "chat",
            temperature=getattr(row, "temperature", None),
            max_output_tokens=getattr(row, "max_output_tokens", None),
        ),
        _admin=_admin,
    )


# --- Fetch live model list from the provider (admin) --------------------------


class FetchModelsResponse(BaseModel):
    models: List[str]
    message: Optional[str] = None


async def _fetch_models_openai_compatible(base_url: str, api_key: str) -> List[str]:
    import httpx

    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.get(f"{base_url}/models", headers=headers)
        if resp.status_code == 404 and not base_url.endswith("/v1"):
            # Some deployments expose the endpoint under /v1 only
            resp = await http.get(f"{base_url}/v1/models", headers=headers)
        resp.raise_for_status()
        data = resp.json().get("data") or []
    return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]


async def _fetch_models_anthropic(base_url: str, api_key: str) -> List[str]:
    import httpx

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.get(f"{base_url}/v1/models", headers=headers)
        resp.raise_for_status()
        data = resp.json().get("data") or []
    return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]


@router.post("/providers/{provider_id}/fetch-models", response_model=FetchModelsResponse)
async def fetch_provider_models(
    provider_id: int,
    _admin: User = Depends(require_settings_write),
    db: AsyncSession = Depends(get_db),
):
    """Fetch the live model list from the provider's /models endpoint."""
    row = await db.get(LlmProvider, provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="供应商不存在")
    if not row.api_key:
        raise HTTPException(status_code=400, detail="请先填写该供应商的 API Key")

    base = (row.base_url or "").strip().rstrip("/")
    try:
        if row.protocol == ProviderProtocol.OPENAI_COMPATIBLE:
            models = await _fetch_models_openai_compatible(
                base or "https://api.openai.com/v1", row.api_key
            )
        elif row.protocol == ProviderProtocol.ANTHROPIC_COMPATIBLE:
            models = await _fetch_models_anthropic(
                base or "https://api.anthropic.com", row.api_key
            )
        else:
            return FetchModelsResponse(
                models=[],
                message="Google Gemini 暂不支持拉取模型列表，请手动填写模型名",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Fetch models failed for provider {row.name}: {e}")
        raise HTTPException(status_code=502, detail=f"拉取模型列表失败: {str(e)[:200]}")

    return FetchModelsResponse(models=models)


# --- Model catalog (any authenticated user) ----------------------------------


class ModelInfo(BaseModel):
    id: str
    name: str
    vision: bool
    context_length: Optional[int] = None
    pricing: Optional[dict] = None


class ModelsResponse(BaseModel):
    models: List[ModelInfo]
    source: str  # "openrouter" or "fallback" or "none"


@router.get("/models", response_model=ModelsResponse)
async def get_available_models(
    provider: Optional[str] = None,
    vision_only: bool = False,
    _current_user: User = Depends(get_current_user),
):
    """Available model suggestions for a provider slug (catalog only —
    custom providers accept any model name via free-text input)."""
    if not provider:
        return ModelsResponse(models=[], source="none")
    models = get_models_with_fallback(provider.lower(), vision_only)
    source = "openrouter" if models and "/" in models[0].get("id", "") else "fallback"
    return ModelsResponse(models=[ModelInfo(**m) for m in models], source=source)


# --- App settings: OCR engine + MCP (admin) ----------------------------------


@router.get("/app-settings", response_model=AppSettingsOut)
async def get_app_settings(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    baidu_key = await get_setting(db, SETTING_BAIDU_OCR_API_KEY)
    baidu_secret = await get_setting(db, SETTING_BAIDU_OCR_SECRET_KEY)
    mcp_token = await get_setting(db, SETTING_MCP_TOKEN)
    return AppSettingsOut(
        ocr_engine=await get_setting(db, SETTING_OCR_ENGINE, "local") or "local",
        baidu_ocr_api_key_masked=mask_secret(baidu_key),
        has_baidu_ocr_api_key=bool(baidu_key),
        baidu_ocr_secret_key_masked=mask_secret(baidu_secret),
        has_baidu_ocr_secret_key=bool(baidu_secret),
        mcp_enabled=(await get_setting(db, SETTING_MCP_ENABLED, "false")) == "true",
        mcp_token_masked=mask_secret(mcp_token),
        has_mcp_token=bool(mcp_token),
    )


@router.put("/app-settings", response_model=AppSettingsOut)
async def update_app_settings(
    body: AppSettingsUpdateRequest,
    _admin: User = Depends(require_settings_write),
    db: AsyncSession = Depends(get_db),
):
    if body.ocr_engine is not None:
        if body.ocr_engine not in ("local", "baidu"):
            raise HTTPException(status_code=400, detail="ocr_engine 只支持 local 或 baidu")
        await set_setting(db, SETTING_OCR_ENGINE, body.ocr_engine)
    if body.baidu_ocr_api_key:
        await set_setting(db, SETTING_BAIDU_OCR_API_KEY, body.baidu_ocr_api_key.strip())
    if body.baidu_ocr_secret_key:
        await set_setting(db, SETTING_BAIDU_OCR_SECRET_KEY, body.baidu_ocr_secret_key.strip())
    if body.mcp_enabled is not None:
        await set_setting(db, SETTING_MCP_ENABLED, "true" if body.mcp_enabled else "false")
    if body.mcp_token:
        await set_setting(db, SETTING_MCP_TOKEN, body.mcp_token.strip())
    await db.commit()
    return await get_app_settings(_admin=_admin, db=db)
