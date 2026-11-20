#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.1.2：模型列表（新增/排序/启用/fallback 链）与 Key 掩码。

只用临时目录与临时 DB，不碰项目 data/nas-curator.db 与 NAS，不调真实模型
（fallback 用 monkeypatch 模拟 provider 成败）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from common import llm_models as LM


def test_validate_models():
    norm = LM.validate_models(LM.DEFAULT_MODELS)
    assert [m["id"] for m in norm] == ["ark-plan", "ark-payg", "local"]
    assert norm[0]["key_env"] == "ARK_PLAN_KEY"
    # 自定义缺 endpoint -> 拒绝
    with pytest.raises(ValueError):
        LM.validate_models([{"name": "X", "type": "custom", "model": "m1"}])
    # Key 明文不得进列表文件
    with pytest.raises(ValueError):
        LM.validate_models([{"name": "X", "type": "custom", "model": "m1",
                             "base_url": "http://x/v1", "key": "sk-secret"}])
    # 非法 type / 重复 id / 空 model
    with pytest.raises(ValueError):
        LM.validate_models([{"name": "X", "type": "nope", "model": "m1"}])
    with pytest.raises(ValueError):
        LM.validate_models([
            {"id": "a", "name": "A", "type": "ollama", "model": "qwen3:4b"},
            {"id": "a", "name": "B", "type": "ollama", "model": "qwen3:4b"}])
    with pytest.raises(ValueError):
        LM.validate_models([{"name": "X", "type": "ollama", "model": ""}])
    # 空列表合法（LLM 不可用，只走规则）
    assert LM.validate_models([]) == []
    print("validate_models: PASS")


def test_mask_key_matches_deps():
    from api import deps as D
    for v in ["", "a", "abcd", "abcde", "sk-test-secret-12345"]:
        assert LM.mask_key(v) == D.mask_key(v), v
    assert LM.mask_key("sk-test-secret-12345") == "sk****45"
    print("mask_key: PASS")


def test_load_models_file(tmp_path, monkeypatch):
    models_path = tmp_path / "llm_models.json"
    monkeypatch.setenv("CURATOR_LLM_MODELS", str(models_path))
    models, source = LM.load_models()
    assert source == "default" and len(models) == 3
    custom = [
        {"id": "local2", "name": "本地2", "type": "ollama",
         "model": "qwen3:8b", "base_url": "http://127.0.0.1:11434",
         "enabled": True},
        {"id": "mine", "name": "自建", "type": "custom",
         "model": "gpt-4o-mini", "base_url": "https://api.example.com/v1",
         "key_env": "LLM_KEY_MINE", "enabled": False},
    ]
    LM.save_models(custom)
    models, source = LM.load_models()
    assert source == "file"
    assert [m["id"] for m in models] == ["local2", "mine"]
    assert "api_key" not in json.dumps(models) and "sk-" not in json.dumps(models)
    pub = LM.public_models(models, {"LLM_KEY_MINE": "sk-abcdef123456"})
    assert pub[1]["key_set"] is True and pub[1]["key_masked"] == "sk****56"
    assert pub[0]["key_set"] is False
    print("load_models file: PASS")


def test_llm_loads_models_file_and_skips_keyless(monkeypatch, tmp_path):
    models_path = tmp_path / "llm_models.json"
    empty_cfg = tmp_path / "empty-config"
    empty_cfg.mkdir()
    monkeypatch.setenv("CURATOR_LLM_MODELS", str(models_path))
    # 隔离项目 config/.env.local（开发机上有真实 ARK_PLAN_KEY），否则 Key 会被读到
    monkeypatch.setenv("CURATOR_CONFIG_DIR", str(empty_cfg))
    monkeypatch.delenv("CURATOR_LLM_CONFIG", raising=False)
    monkeypatch.delenv("ARK_PLAN_KEY", raising=False)
    monkeypatch.delenv("ARK_PAYG_KEY", raising=False)
    LM.save_models([
        {"id": "ark-plan", "name": "火山 Ark Plan", "type": "ark-plan",
         "model": "deepseek-v4-1-flash-261017",
         "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
         "key_env": "ARK_PLAN_KEY", "enabled": True},
        {"id": "local", "name": "本地 Ollama", "type": "ollama",
         "model": "qwen3:4b", "base_url": "http://127.0.0.1:11434",
         "enabled": True},
    ])
    from curator_core import llmkit
    monkeypatch.setattr(llmkit, "CONFIG_PATH", "")
    from curator_core.llmkit import LLM
    llm = LLM()
    # ark-plan 无 Key -> 跳过，只剩 local
    assert [p["name"] for p in llm.providers] == ["local"], llm.providers
    # 写 Key 到环境变量 -> ark-plan 出现且排第一
    monkeypatch.setenv("ARK_PLAN_KEY", "dummy-plan-key")
    llm2 = LLM()
    assert [p["name"] for p in llm2.providers] == ["ark-plan", "local"]
    print("llm loads models file: PASS")


def test_llm_reads_key_from_env_local(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / ".env.local").write_text("ARK_PLAN_KEY=dummy-from-file\n", encoding="utf-8")
    monkeypatch.setenv("CURATOR_CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("CURATOR_LLM_CONFIG", raising=False)
    monkeypatch.delenv("ARK_PLAN_KEY", raising=False)
    monkeypatch.delenv("ARK_PAYG_KEY", raising=False)
    monkeypatch.delenv("CURATOR_LLM_MODELS", raising=False)
    from curator_core import llmkit
    monkeypatch.setattr(llmkit, "CONFIG_PATH", "")
    from curator_core.llmkit import LLM
    llm = LLM()
    names = [p["name"] for p in llm.providers]
    assert "ark-plan" in names, names  # .env.local 里的 Key 被读到
    print("llm reads key from .env.local: PASS")


def test_fallback_chain_first_fails_second_used(monkeypatch):
    from curator_core.llmkit import LLM
    providers = [
        {"name": "first", "kind": "ark", "base_url": "http://127.0.0.1:9",
         "api_key": "k1", "model": "m1", "timeout": 5},
        {"name": "second", "kind": "ollama", "base_url": "http://127.0.0.1:11434",
         "model": "qwen3:4b", "embed_model": "bge-m3:latest", "timeout": 5},
    ]
    llm = LLM(providers=providers)
    monkeypatch.setattr(llm, "_call_ark", lambda *a, **k: (None, "server:500"))
    monkeypatch.setattr(llm, "_call_ollama",
                        lambda *a, **k: ("fallback-ok", None))
    out = llm.chat("hello")
    assert out == "fallback-ok"
    assert llm.active == "second"
    assert llm.stats["first"]["fail"] == 1
    assert llm.stats["second"]["ok"] == 1
    print("fallback chain: PASS")


def test_call_ark_thinking_flag(monkeypatch):
    from curator_core.llmkit import LLM
    import requests as _rq

    captured = {}

    class FakeResp:
        status_code = 200
        headers = {}

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

        @property
        def text(self):
            return "ok"

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return FakeResp()

    monkeypatch.setattr(_rq, "post", fake_post)
    llm = LLM(providers=[])
    p = {"name": "x", "kind": "ark", "base_url": "http://x",
         "api_key": "k", "model": "m", "timeout": 5}
    content, err = llm._call_ark(p, [{"role": "user", "content": "hi"}],
                                 False, 5, 100)
    assert content == "ok" and err is None
    assert captured["json"]["thinking"] == {"type": "disabled"}
    content, err = llm._call_ark(p, [{"role": "user", "content": "hi"}],
                                 False, 5, 100, use_thinking=False)
    assert "thinking" not in captured["json"], "自定义端点不应带 thinking 字段"
    print("call_ark thinking flag: PASS")


# ---------------------------------------------------------------- 设置页 API

@pytest.fixture()
def client(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    models_path = cfg_dir / "llm_models.json"
    env_local = cfg_dir / ".env.local"
    monkeypatch.setenv("CURATOR_LLM_MODELS", str(models_path))
    monkeypatch.setenv("CURATOR_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("CURATOR_DB", str(tmp_path / "s.db"))
    import api.deps as deps
    monkeypatch.setattr(deps, "ENV_LOCAL", str(env_local))
    import api.settings as settings
    monkeypatch.setattr(settings, "ENV_LOCAL", str(env_local))
    from common import db as DB
    DB.init_db(str(tmp_path / "s.db"))
    from fastapi.testclient import TestClient
    from api.main import app
    with TestClient(app) as c:
        c._cfg = cfg_dir
        c._models_path = models_path
        c._env_local = env_local
        yield c


def test_settings_models_get_default_masked(client):
    r = client.get("/api/settings")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["models_source"] == "default"
    assert [m["id"] for m in d["models"]] == ["ark-plan", "ark-payg", "local"]
    assert "api_key" not in json.dumps(d["models"])
    print("settings GET default models: PASS")


def test_settings_models_put_add_reorder_key_masked(client):
    new_list = [
        {"name": "本地 Ollama", "type": "ollama", "model": "qwen3:4b",
         "base_url": "http://172.17.0.1:11434", "enabled": True},
        {"name": "自建网关", "type": "custom", "model": "gpt-4o-mini",
         "base_url": "https://api.example.com/v1", "enabled": True,
         "key": "sk-test-secret-12345"},
        {"name": "火山 Ark Plan", "type": "ark-plan",
         "model": "deepseek-v4-1-flash-261017", "enabled": True},
    ]
    r = client.put("/api/settings/models", json={"models": new_list})
    assert r.status_code == 200, r.text
    d = r.json()
    # 顺序即优先级：本地第一，其次自建网关，最后火山 Ark Plan
    assert [m["name"] for m in d["models"]] == ["本地 Ollama", "自建网关", "火山 Ark Plan"]
    assert [m["id"] for m in d["models"]] == ["ollama", "model-2", "ark-plan"]
    assert d["models"][0]["type"] == "ollama"
    assert d["models"][1]["type"] == "custom"
    # Key 掩码显示，明文绝不出现在任何返回里
    assert d["models"][1]["key_set"] is True
    assert d["models"][1]["key_masked"] == "sk****45"
    assert "sk-test-secret-12345" not in r.text
    # .env.local 里有 Key，模型列表文件里没有
    assert "sk-test-secret-12345" in client._env_local.read_text(encoding="utf-8")
    assert "sk-test-secret" not in client._models_path.read_text(encoding="utf-8")
    # .env.local 权限 600
    import stat
    assert stat.S_IMODE(client._env_local.stat().st_mode) == 0o600
    # GET 也不回显明文
    r2 = client.get("/api/settings")
    assert "sk-test-secret-12345" not in r2.text
    assert r2.json()["models"][1]["key_masked"] == "sk****45"
    assert r2.json()["models_source"] == "file"
    print("settings PUT models add/reorder/key masked: PASS")


def test_settings_models_put_key_kept_when_empty(client):
    r = client.put("/api/settings/models", json={"models": [
        {"name": "自建网关", "type": "custom", "model": "gpt-4o-mini",
         "base_url": "https://api.example.com/v1", "enabled": True,
         "key": "sk-keep-me-99999"},
        {"name": "本地 Ollama", "type": "ollama", "model": "qwen3:4b",
         "base_url": "http://172.17.0.1:11434", "enabled": True},
    ]})
    assert r.status_code == 200, r.text
    # 第二次保存不带 key -> 已存 Key 保留
    r = client.put("/api/settings/models", json={"models": [
        {"id": r.json()["models"][0]["id"], "name": "自建网关",
         "type": "custom", "model": "gpt-4o-mini",
         "base_url": "https://api.example.com/v1", "enabled": True},
        {"id": r.json()["models"][1]["id"], "name": "本地 Ollama",
         "type": "ollama", "model": "qwen3:4b",
         "base_url": "http://172.17.0.1:11434", "enabled": True},
    ]})
    assert r.status_code == 200, r.text
    assert r.json()["models"][0]["key_set"] is True
    assert r.json()["models"][0]["key_masked"] == "sk****99"
    print("settings PUT key kept when empty: PASS")


def test_settings_models_put_invalid(client):
    r = client.put("/api/settings/models", json={"models": [
        {"name": "坏的", "type": "nope", "model": "m1"}]})
    assert r.status_code == 400, r.text
    r = client.put("/api/settings/models", json={"models": [
        {"id": "dup", "name": "A", "type": "ollama", "model": "qwen3:4b"},
        {"id": "dup", "name": "B", "type": "ollama", "model": "qwen3:4b"}]})
    assert r.status_code == 400, r.text
    # 直接调 validate_models 时 Key 明文应被拒绝（API 层会先剥离 key 再校验）
    with pytest.raises(ValueError):
        LM.validate_models([{"name": "X", "type": "custom", "model": "m1",
                             "base_url": "https://x/v1", "key": "sk-x"}])
    print("settings PUT invalid: PASS")


def test_settings_key_compat_still_works(client):
    r = client.post("/api/settings/key",
                    json={"provider": "ark-plan", "key": "sk-compat-77777"})
    assert r.status_code == 200, r.text
    assert r.json()["masked"] == "sk****77"
    assert "sk-compat-77777" in client._env_local.read_text(encoding="utf-8")
    print("settings POST /key compat: PASS")
