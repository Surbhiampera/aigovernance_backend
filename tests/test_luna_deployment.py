from app.services import deployment_service as ds
from app.services.ai_model_pricing import get_model_pricing_for_provider
from app.services.provider_translation import _is_reasoning_model

_ENV = {
    "AZURE_OPENAI_LUNA_API_KEY": "k",
    "AZURE_OPENAI_LUNA_ENDPOINT": "https://example.openai.azure.com",
    "AZURE_OPENAI_LUNA_DEPLOYMENT_NAME": "gpt-5.6-luna",
    "AZURE_OPENAI_LUNA_API_VERSION": "2025-04-01-preview",
}


def test_luna_env_fallback_builds_azure_url(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    deps = [d for d in ds._env_fallbacks(org_id="o", project_id=None) if d.model_name == "gpt-5.6-luna"]
    assert len(deps) == 1
    url, headers = ds.build_provider_request(deps[0])
    assert url == (
        "https://example.openai.azure.com/openai/deployments/gpt-5.6-luna"
        "/chat/completions?api-version=2025-04-01-preview"
    )
    assert headers["api-key"] == "k"
    assert ds._env_fallback_model_names()["gpt-5.6-luna"] == "azure_openai"


def test_luna_absent_without_env(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    assert "gpt-5.6-luna" not in ds._env_fallback_model_names()


def test_luna_priced_and_treated_as_reasoning():
    assert get_model_pricing_for_provider("gpt-5.6-luna", "azure_openai") is not None
    assert _is_reasoning_model("gpt-5.6-luna")
