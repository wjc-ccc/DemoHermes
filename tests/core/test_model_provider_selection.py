"""MODEL_PROVIDER 决定默认客户端和密钥，MODEL 决定模型名。"""
from agent.core.model.doubao import DoubaoModel
from agent.core.model.registry import ModelRegistry


def test_doubao_provider_uses_doubao_key_and_shared_model_name(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "doubao")
    monkeypatch.setenv("MODEL", "deepseek-v4-flash")
    monkeypatch.setattr("agent.core.model.doubao.DOUBAO_API_KEY", "ark-test-key")
    monkeypatch.setattr("agent.core.model.doubao.DOUBAO_BASE_URL", "https://ark.example.com/api/plan")
    monkeypatch.setattr("agent.core.model.claude.MODEL", "deepseek-v4-flash")

    model = ModelRegistry.default().get()

    assert isinstance(model, DoubaoModel)
    assert model.name == "doubao"
    assert model.api_key == "ark-test-key"
    assert model.base_url == "https://ark.example.com/api/plan"
    assert model.model_name == "deepseek-v4-flash"


def test_deepseek_provider_stays_default_when_doubao_is_not_selected(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "deepseek")

    model = ModelRegistry.default().get()

    assert model.name == "deepseek"
