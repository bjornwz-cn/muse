#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.1.5：向量模型独立配置（common.embedding）。

向量模型只做语义预筛 / 聚类，不进语言调用链；缺省 bge-m3:latest 走 GPU
（SEM_HOST）；模型名可配，endpoint 只读（环境变量优先）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from common import embedding as EM  # noqa: E402


@pytest.fixture()
def cfgdir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setenv("CURATOR_CONFIG_DIR", str(d))
    monkeypatch.delenv("SEM_HOST", raising=False)
    EM.clear_cache()
    yield d
    EM.clear_cache()


def _write_cfg(d, me):
    cfg = {"libraries": [], "model_endpoints": me}
    (d / "libraries.json").write_text(json.dumps(cfg, ensure_ascii=False),
                                       encoding="utf-8")


def test_default():
    cfg = EM.load_embedding_config()
    assert cfg["model"] == "bge-m3:latest"
    assert cfg["host"] == "http://172.17.0.1:11500"


def test_validate_model():
    assert EM.validate_embedding_model("  bge-m3:latest ") == "bge-m3:latest"
    assert EM.validate_embedding_model("org/model:tag") == "org/model:tag"
    for bad in ("", "  ", "has space", "a" * 129, None, 123):
        with pytest.raises(ValueError):
            EM.validate_embedding_model(bad)


def test_load_model_from_config(cfgdir):
    _write_cfg(cfgdir, {"embedding_model": "nomic-embed-text:latest"})
    assert EM.load_embedding_config()["model"] == "nomic-embed-text:latest"


def test_invalid_model_falls_back(cfgdir):
    _write_cfg(cfgdir, {"embedding_model": "has space"})
    assert EM.load_embedding_config()["model"] == "bge-m3:latest"


def test_host_env_priority(cfgdir, monkeypatch):
    _write_cfg(cfgdir, {"sem_host": "http://10.0.0.1:11500"})
    assert EM.load_embedding_config()["host"] == "http://10.0.0.1:11500"
    monkeypatch.setenv("SEM_HOST", "http://10.0.0.2:11500")
    EM.clear_cache()
    assert EM.load_embedding_config()["host"] == "http://10.0.0.2:11500"


def test_jobs_uses_configured_model(cfgdir):
    """api/jobs 的 plan 命名空间用配置的向量模型，不再写死 bge-m3。"""
    from api import jobs
    _write_cfg(cfgdir, {"embedding_model": "nomic-embed-text:latest"})
    EM.clear_cache()
    ns = jobs._plan_namespace(
        jobs.CreateJobBody(root="/tmp", library=None), "/tmp", ":memory:")
    assert ns.sem_model == "nomic-embed-text:latest"


def test_llm_default_embed_model_follows_config(cfgdir):
    """LLM() 不传 embed_model 时用配置值；显式传参仍优先。"""
    from curator_core.llmkit import LLM
    _write_cfg(cfgdir, {"embedding_model": "nomic-embed-text:latest"})
    EM.clear_cache()
    assert LLM(providers=[]).embed_model == "nomic-embed-text:latest"
    assert LLM(embed_model="custom-embed", providers=[]).embed_model == "custom-embed"


def test_llm_provider_embed_model_follows_config(cfgdir):
    """模型列表里 ollama 条目未写 embed_model 时，用配置值而非写死缺省。"""
    from curator_core.llmkit import _providers_from_models
    _write_cfg(cfgdir, {"embedding_model": "nomic-embed-text:latest"})
    EM.clear_cache()
    ps = _providers_from_models([{
        "id": "local", "name": "本地 Ollama", "type": "ollama",
        "model": "qwen3:4b", "base_url": "http://127.0.0.1:11434",
        "key_env": "", "enabled": True, "billing": "free"}])
    assert ps[0]["embed_model"] == "nomic-embed-text:latest"
