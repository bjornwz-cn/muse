"""0.1.3：LLM 调用链顺序——本地优先，再用云模型，免费额度云在按量云之前。

档位：0=本地 Ollama，1=免费额度云模型，2=按量云模型；档内保持用户列表顺序。
计费属性 billing：free（订阅/免费额度）/ payg（按量计费）；
类型默认值 ark-plan→free、ark-payg→payg、ollama→free、custom→payg。
"""
import pytest

from common.llm_models import validate_models
from curator_core.llmkit import LLM, _providers_from_models, order_providers

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"


def _models():
    return [
        {"id": "ark-plan", "name": "火山 Ark Plan", "type": "ark-plan",
         "model": "deepseek-v4-1-flash-261017", "base_url": ARK_BASE,
         "key_env": "ARK_PLAN_KEY", "enabled": True},
        {"id": "ark-payg", "name": "火山 Ark 按量", "type": "ark-payg",
         "model": "deepseek-v4-1-flash-261017", "base_url": ARK_BASE,
         "key_env": "ARK_PAYG_KEY", "enabled": True},
        {"id": "local", "name": "本地 Ollama", "type": "ollama",
         "model": "qwen3:4b", "base_url": "http://127.0.0.1:11434",
         "embed_model": "bge-m3", "key_env": "", "enabled": True},
    ]


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    # 测试 Key：环境变量优先于项目 config/.env.local 里的真实 Key
    monkeypatch.setenv("ARK_PLAN_KEY", "sk-test-plan")
    monkeypatch.setenv("ARK_PAYG_KEY", "sk-test-payg")


def test_chain_order_local_first():
    """默认列表顺序是 ark-plan → ark-payg → local，生效链应为 local 在前。"""
    providers = order_providers(_providers_from_models(_models()))
    assert [p["name"] for p in providers] == ["local", "ark-plan", "ark-payg"]
    assert [p["billing"] for p in providers] == ["free", "free", "payg"]


def test_chain_order_free_cloud_before_payg(monkeypatch):
    """免费额度云模型在按量云模型之前（与列表顺序无关）。"""
    monkeypatch.setenv("TEST_CUSTOM_KEY", "sk-test-custom")
    models = [
        {"id": "c-payg", "name": "自定义按量", "type": "custom",
         "model": "x", "base_url": "https://example.invalid/v1",
         "key_env": "TEST_CUSTOM_KEY", "enabled": True, "billing": "payg"},
        {"id": "ark-plan", "name": "火山 Ark Plan", "type": "ark-plan",
         "model": "deepseek-v4-1-flash-261017", "base_url": ARK_BASE,
         "key_env": "ARK_PLAN_KEY", "enabled": True, "billing": "free"},
        {"id": "local", "name": "本地 Ollama", "type": "ollama",
         "model": "qwen3:4b", "base_url": "http://127.0.0.1:11434",
         "embed_model": "bge-m3", "key_env": "", "enabled": True, "billing": "free"},
    ]
    providers = order_providers(_providers_from_models(models))
    assert [p["name"] for p in providers] == ["local", "ark-plan", "c-payg"]


def test_chain_order_stable_within_tier():
    """档内保持用户列表顺序：两家本地、两家免费云各按原顺序。"""
    models = [
        {"id": "cloud-b", "name": "云B", "type": "ark-payg",
         "model": "deepseek-v4-1-flash-261017", "base_url": ARK_BASE,
         "key_env": "ARK_PAYG_KEY", "enabled": True, "billing": "free"},
        {"id": "local-a", "name": "本地A", "type": "ollama",
         "model": "qwen3:4b", "base_url": "http://127.0.0.1:11434",
         "embed_model": "bge-m3", "key_env": "", "enabled": True},
        {"id": "cloud-a", "name": "云A", "type": "ark-plan",
         "model": "deepseek-v4-1-flash-261017", "base_url": ARK_BASE,
         "key_env": "ARK_PLAN_KEY", "enabled": True, "billing": "free"},
        {"id": "local-b", "name": "本地B", "type": "ollama",
         "model": "qwen3:8b", "base_url": "http://127.0.0.1:11434",
         "embed_model": "bge-m3", "key_env": "", "enabled": True},
    ]
    providers = order_providers(_providers_from_models(models))
    assert [p["name"] for p in providers] == ["local-a", "local-b", "cloud-b", "cloud-a"]


def test_billing_defaults_filled_by_validate():
    """未填 billing 时按类型默认值补齐。"""
    by_id = {m["id"]: m for m in validate_models(_models())}
    assert by_id["ark-plan"]["billing"] == "free"
    assert by_id["ark-payg"]["billing"] == "payg"
    assert by_id["local"]["billing"] == "free"


def test_billing_custom_explicit_free_kept():
    """custom 显式标 free 时保留（不强制改回 payg）。"""
    models = validate_models([{"id": "c1", "name": "自建免费", "type": "custom",
                               "model": "x", "base_url": "https://example.invalid/v1",
                               "key_env": "TEST_K", "enabled": True, "billing": "free"}])
    assert models[0]["billing"] == "free"


def test_billing_invalid_rejected():
    with pytest.raises(ValueError):
        validate_models([{"id": "c1", "name": "错", "type": "custom",
                          "model": "x", "base_url": "https://example.invalid/v1",
                          "key_env": "TEST_K", "enabled": True, "billing": "包月"}])


def test_chain_skips_keyless_cloud(monkeypatch, tmp_path):
    """Key 为空的云模型不进链（Ark Plan 订阅到期/无 Key 时自动跳过）。"""
    monkeypatch.delenv("ARK_PLAN_KEY", raising=False)
    monkeypatch.delenv("ARK_PAYG_KEY", raising=False)
    # 同时把配置目录指到空目录，避免读到项目 config/.env.local 里的真实 Key
    monkeypatch.setenv("CURATOR_CONFIG_DIR", str(tmp_path))
    providers = order_providers(_providers_from_models(_models()))
    assert [p["name"] for p in providers] == ["local"]


def test_order_providers_does_not_mutate_input():
    providers = _providers_from_models(_models())
    before = [p["name"] for p in providers]
    order_providers(providers)
    assert [p["name"] for p in providers] == before


def test_fallback_next_provider_on_failure(monkeypatch):
    """某一家失败自动试下一家：本地失败 → 火山 Ark Plan 成功。"""
    providers = order_providers(_providers_from_models(_models()))
    llm = LLM(providers=providers)
    assert [p["name"] for p in llm.providers] == ["local", "ark-plan", "ark-payg"]
    monkeypatch.setattr(llm, "_call_ollama", lambda *a, **k: (None, "server"))
    monkeypatch.setattr(llm, "_call_ark", lambda *a, **k: ('{"category": "x"}', None))
    out = llm.chat("test", fmt=None)
    assert out == '{"category": "x"}'
    assert llm.active == "ark-plan"
    assert llm.stats["local"]["fail"] == 1
    assert llm.stats["ark-plan"]["ok"] == 1
