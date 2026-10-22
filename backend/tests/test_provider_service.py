"""Tests for provider configuration service.

Business invariants:
- Every legacy .env provider credential must survive the one-time DB migration.
- Secrets must never be returned in full (masking keeps only the last 4 chars).
- Presets must cover the six advertised providers with working protocols.
"""

from types import SimpleNamespace

from app.models.provider import ProviderProtocol, SETTING_ACTIVE_PROVIDER_ID
from app.services.provider_service import PRESET_PROVIDERS, _migrate_legacy_env, mask_secret


def test_mask_secret_hides_all_but_last_four():
    assert mask_secret("sk-1234567890abcd") == "****abcd"
    assert mask_secret("short") == "****"  # never leak short secrets
    assert mask_secret("") is None
    assert mask_secret(None) is None


def test_presets_cover_advertised_providers():
    names = {p["name"] for p in PRESET_PROVIDERS}
    assert names == {"openai", "anthropic", "deepseek", "zhipu", "qwen", "google"}
    for preset in PRESET_PROVIDERS:
        assert isinstance(preset["protocol"], ProviderProtocol)
        assert preset["model"], f"preset {preset['name']} must have a default model"


def test_domestic_presets_default_to_vision_models():
    """Invoice recognition needs image input: domestic presets must default
    to vision-capable models (the pre-v0.4 defaults were text-only and dead)."""
    by_name = {p["name"]: p for p in PRESET_PROVIDERS}
    assert by_name["deepseek"]["model"] == "deepseek-flash"
    assert by_name["zhipu"]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert by_name["qwen"]["model"] == "qwen-vl-plus"
    for slug in ("deepseek", "zhipu", "qwen"):
        assert by_name[slug]["vision"] is True


class _FakeDB:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


def _fake_settings(**overrides):
    base = {
        "llm_provider": "",
        # all keys empty by default
        **{attr: "" for triple in (
            ("openai_api_key", "openai_model", "openai_base_url"),
        ) for attr in triple},
    }
    base.update(overrides)
    return SimpleNamespace(
        llm_provider=base.get("llm_provider", ""),
        openai_api_key=base.get("openai_api_key", ""),
        openai_model=base.get("openai_model", ""),
        openai_base_url=base.get("openai_base_url", ""),
        deepseek_api_key=base.get("deepseek_api_key", ""),
        deepseek_model=base.get("deepseek_model", ""),
        deepseek_base_url=base.get("deepseek_base_url", ""),
        zhipu_api_key=base.get("zhipu_api_key", ""),
        zhipu_model=base.get("zhipu_model", ""),
        zhipu_base_url=base.get("zhipu_base_url", ""),
        qwen_api_key=base.get("qwen_api_key", ""),
        qwen_model=base.get("qwen_model", ""),
        qwen_base_url=base.get("qwen_base_url", ""),
        anthropic_api_key=base.get("anthropic_api_key", ""),
        anthropic_model=base.get("anthropic_model", ""),
        anthropic_base_url=base.get("anthropic_base_url", ""),
        google_api_key=base.get("google_api_key", ""),
        google_model=base.get("google_model", ""),
        google_base_url=base.get("google_base_url", ""),
        get_active_llm_provider=lambda: base.get("llm_provider") or None,
    )


def _row(slug, row_id=1):
    return SimpleNamespace(id=row_id, name=slug, api_key=None, model=None, base_url=None)


def test_legacy_env_keys_migrate_into_rows(monkeypatch):
    fake = _fake_settings(
        llm_provider="deepseek",
        deepseek_api_key="sk-ds-9999",
        deepseek_model="deepseek-chat",
    )
    monkeypatch.setattr("app.services.provider_service.get_settings", lambda: fake)

    rows = {slug: _row(slug) for slug in ("openai", "deepseek", "zhipu")}
    db = _FakeDB()
    _migrate_legacy_env(db, rows)

    assert rows["deepseek"].api_key == "sk-ds-9999"
    assert rows["deepseek"].model == "deepseek-chat"
    # provider without a key is untouched
    assert rows["openai"].api_key is None
    # active provider recorded
    assert any(getattr(o, "key", None) == SETTING_ACTIVE_PROVIDER_ID for o in db.added)


def test_legacy_placeholder_keys_are_ignored(monkeypatch):
    fake = _fake_settings(openai_api_key="your_openai_api_key_here")
    monkeypatch.setattr("app.services.provider_service.get_settings", lambda: fake)

    rows = {"openai": _row("openai")}
    _migrate_legacy_env(_FakeDB(), rows)
    assert rows["openai"].api_key is None
