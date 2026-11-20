#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llmkit —— 模型辅助层（v3.0 / provider 抽象）

能力：
  1) 规则判不准时，读文件名+正文，给出类目与标题；
  2) 用 bge-m3 向量对目录内文件做主题聚类，辅助「整目录归属」判断。

与 v2 的差别：把「怎么调模型」抽成 provider 链，按顺序自动降级。
任何一环不可用/超时/额度尽/被封，自动切下一个，主流程不阻塞。

0.1.2：provider 链改为用户可配的模型列表（common.llm_models，
config/llm_models.json，设置页写；顺序即优先级）。类型新增 custom
（自定义 OpenAI 兼容 endpoint，走 chat/completions，不带 thinking 字段）。

0.1.3：生效链改为三档（王震亲定）：本地 Ollama → 免费额度云模型 →
按量计费云模型；档内保持用户列表顺序。计费属性见模型条目的 billing
（free/payg；ark-plan 默认 free，ark-payg 默认 payg，ollama 强制 free，
custom 默认 payg、可标 free）。Key 为空的云模型在加载时即跳过
（Ark Plan 订阅 2026-11-25 到期后，其免费额度即失效——无 Key/额度耗尽
时该 provider 不进链，调用自动落到下一档，无需手动处理）。

约束：
  - 全链失败 -> 返回 None，调用方沿用规则结论；
  - key 不硬编码在逻辑里：只从环境变量（ARK_PLAN_KEY / ARK_PAYG_KEY）
    或 CURATOR_LLM_CONFIG 指定的配置文件读；本包内不设 llm.json，
    也不再从模块同级目录读任何密钥文件；
  - 0.1.2 补充：Key 也可从 CURATOR_CONFIG_DIR 下的 .env.local 读
    （设置页的写入位置；收件箱监听进程没有 os.environ 注入，靠此文件取 Key）。
    模型列表文件里绝不含 Key 明文，只记 key_env 变量名。
  - 只读：不产生任何文件改动。
"""
from __future__ import annotations

import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CURATOR_LLM_CONFIG") or ""  # 未指定则不读任何密钥文件

DEFAULT_HOST = "http://172.17.0.1:11434"
DEFAULT_MODEL = "qwen3:4b"
DEFAULT_EMBED = "bge-m3:latest"


def _configured_embed_model() -> str:
    """0.1.5：设置页配的向量模型（common.embedding）；读不到/非法回缺省。"""
    try:
        from common.embedding import load_embedding_config
        return load_embedding_config().get("model") or DEFAULT_EMBED
    except Exception:  # noqa: BLE001
        return DEFAULT_EMBED

ARK_BASE_PLAN = "https://ark.cn-beijing.volces.com/api/plan/v3"
ARK_BASE_PAYG = "https://ark.cn-beijing.volces.com/api/v3"

# 原因 -> 冷却秒数；None 表示本次进程内永久停用
COOLDOWN = {
    "auth": None,          # 401/403 key 无效或被封
    "model": None,         # 404 模型不存在/未开通
    "quota": None,         # 402/额度耗尽
    "ratelimit": 120,
    "server": 30,
    "timeout": 30,
    "empty": 30,
}


_ARK_ENV = {"ark-plan": "ARK_PLAN_KEY", "ark-payg": "ARK_PAYG_KEY"}


def _read_env_file():
    """读 .env.local（CURATOR_CONFIG_DIR 或项目 config/ 下）供 Key 解析。

    设置页写 Key 后，收件箱监听进程（无 os.environ 注入）靠此文件取 Key；
    CURATOR_CONFIG_DIR 指定时只读该目录下的 .env.local（容器内 /data/config）；
    未指定时读项目 config/ 下的 .env.local（开发态）。
    """
    cfg_dir = os.environ.get("CURATOR_CONFIG_DIR")
    bases = [cfg_dir] if cfg_dir else [os.path.join(HERE, "..", "config")]
    for base in bases:
        if not base:
            continue
        p = os.path.join(base, ".env.local")
        if not os.path.exists(p):
            continue
        out = {}
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            continue
        return out
    return {}


def _resolve_key(key_env):
    """Key 解析：环境变量优先，其次 .env.local；都没有返回 ''（该 provider 跳过）。"""
    if not key_env:
        return ""
    v = os.environ.get(key_env, "")
    if v:
        return v
    return _read_env_file().get(key_env, "")


def _providers_from_legacy(cfg):
    """legacy 来源：CURATOR_LLM_CONFIG 文件，或未指定时的环境变量默认。

    key 只从两个来源读：环境变量（ARK_PLAN_KEY / ARK_PAYG_KEY，
    配置文件里可用 api_key_env 另行指定变量名）或 CURATOR_LLM_CONFIG
    指定的配置文件内的 api_key；不读模块同级 llm.json。"""
    order = cfg.get("order") or ["ark-plan", "ark-payg", "local"]
    spec = cfg.get("providers") or {}
    out = []
    for name in order:
        p = dict(spec.get(name) or {})
        if name == "local":
            out.append({"name": "local", "kind": "ollama",
                        "base_url": p.get("base_url") or DEFAULT_HOST,
                        "model": p.get("model") or DEFAULT_MODEL,
                        "embed_model": p.get("embed_model") or _configured_embed_model(),
                        "billing": "free",
                        "timeout": p.get("timeout") or 25})
            continue
        env_name = p.get("api_key_env") or _ARK_ENV.get(name, "")
        key = _resolve_key(env_name) if env_name else ""
        key = key or p.get("api_key") or ""
        if not key:
            continue
        out.append({"name": name, "kind": "ark",
                    "base_url": (p.get("base_url") or
                                 (ARK_BASE_PLAN if "plan" in name else ARK_BASE_PAYG)).rstrip("/"),
                    "api_key": key,
                    "model": p.get("model") or "deepseek-v4-1-flash-261017",
                    # 0.1.3：legacy 链的计费档位与模型列表默认值一致
                    "billing": "free" if "plan" in name else "payg",
                    "timeout": p.get("timeout") or 30})
    if not any(p["kind"] == "ollama" for p in out):
        out.append({"name": "local", "kind": "ollama", "base_url": DEFAULT_HOST,
                    "model": DEFAULT_MODEL, "embed_model": _configured_embed_model(),
                    "billing": "free", "timeout": 25})
    return out


def _providers_from_models(models):
    """0.1.2 模型列表文件来源：按文件顺序读入（不强制补本地兜底）；
    0.1.3 生效顺序由 _load_config 的 order_providers 再按三档重排
    （本地 Ollama → 免费额度云模型 → 按量云模型；档内保持文件顺序）。

    type 映射：ark-plan/ark-payg -> kind "ark"；ollama -> "ollama"；
    custom -> kind "openai"（OpenAI 兼容 chat/completions，不带 thinking 字段）。
    未启用、或需 Key 但 Key 为空的条目跳过。计费档位取条目 billing
    （0.1.3；缺失时按类型默认值补）。
    """
    from common.llm_models import TYPE_BILLING_DEFAULT
    out = []
    for m in models or []:
        if not m.get("enabled", True):
            continue
        t = m.get("type")
        mid = m.get("id") or t
        billing = (m.get("billing") or TYPE_BILLING_DEFAULT.get(t, "payg"))
        if t == "ollama":
            billing = "free"
        if t in ("ark-plan", "ark-payg"):
            key = _resolve_key(m.get("key_env") or _ARK_ENV.get(t, ""))
            if not key:
                continue
            out.append({"name": mid, "kind": "ark",
                        "base_url": (m.get("base_url") or
                                     (ARK_BASE_PLAN if t == "ark-plan"
                                      else ARK_BASE_PAYG)).rstrip("/"),
                        "api_key": key,
                        "model": m.get("model") or "deepseek-v4-1-flash-261017",
                        "billing": billing,
                        "timeout": m.get("timeout") or 30})
        elif t == "ollama":
            out.append({"name": mid, "kind": "ollama",
                        "base_url": (m.get("base_url") or DEFAULT_HOST).rstrip("/"),
                        "model": m.get("model") or DEFAULT_MODEL,
                        "embed_model": m.get("embed_model") or _configured_embed_model(),
                        "billing": "free",
                        "timeout": m.get("timeout") or 25})
        elif t == "custom":
            key = _resolve_key(m.get("key_env") or "")
            if not key or not (m.get("base_url") or "").strip():
                continue
            out.append({"name": mid, "kind": "openai",
                        "base_url": m["base_url"].rstrip("/"),
                        "api_key": key,
                        "model": m.get("model") or "",
                        "billing": billing,
                        "timeout": m.get("timeout") or 30})
    return out


def _chain_tier(p: dict) -> int:
    """0.1.3 生效链档位：0=本地 Ollama，1=免费额度云模型，2=按量云模型。"""
    if p.get("kind") == "ollama":
        return 0
    return 1 if p.get("billing") == "free" else 2


def order_providers(providers) -> list:
    """0.1.3：把 provider 链按三档重排（档内保持原顺序，sorted 稳定排序）。
    不改输入列表。"""
    return sorted(providers or [], key=_chain_tier)


def effective_chain() -> list:
    """供设置页显示：当前实际生效的调用顺序（已跳过无 Key 的云模型）。
    返回 [{"name","kind","billing"}]，不含 Key。"""
    try:
        return [{"name": p["name"], "kind": p["kind"],
                 "billing": p.get("billing", "free" if p["kind"] == "ollama" else "payg")}
                for p in _load_config()]
    except Exception:
        return []


def _load_config():
    """返回 provider 列表，按 0.1.3 生效顺序排列（本地 Ollama → 免费额度云模型
    → 按量云模型；档内保持列表顺序）。

    来源（按优先）：1) CURATOR_LLM_CONFIG 指定的 legacy 配置文件；
    2) 模型列表文件（0.1.2 设置页写，见 common.llm_models）；
    3) 环境变量默认（ark-plan -> ark-payg -> local）。
    缺失配置时退化为仅本地。
    """
    if CONFIG_PATH and os.path.exists(CONFIG_PATH):
        cfg = {}
        try:
            cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
        except Exception:
            cfg = {}
        return order_providers(_providers_from_legacy(cfg))
    try:
        from common.llm_models import load_models
        models, source = load_models()
    except Exception:  # noqa: BLE001
        models, source = [], "default"
    if source == "file":
        return order_providers(_providers_from_models(models))
    return order_providers(_providers_from_legacy({}))


class LLM:
    def __init__(self, host=DEFAULT_HOST, model=DEFAULT_MODEL, embed_model=None,
                 timeout=25, providers=None):
        # host/model 保留为本地兜底参数，兼容 v2 调用签名
        # 0.1.5：embed_model 缺省 None 时改走设置页配的向量模型（common.embedding）
        self._local_host = (host or DEFAULT_HOST).rstrip("/")
        self._local_model = model or DEFAULT_MODEL
        self.embed_model = embed_model or _configured_embed_model()
        self.timeout = timeout
        self.providers = providers if providers is not None else _load_config()
        for p in self.providers:
            if p["kind"] == "ollama":
                p.setdefault("base_url", self._local_host)
                p.setdefault("model", self._local_model)
                p.setdefault("embed_model", self.embed_model)
        self._down = {}      # name -> 恢复时间戳（inf 表示本次进程内不再用）
        self._alive = None
        self.stats = {}      # name -> {"ok":n, "fail":n, "ms":[], "err":last}
        self.active = None   # 最近一次成功调用的 provider

    # ---------- provider 状态 ----------
    def _available(self):
        now = time.time()
        out = []
        for p in self.providers:
            until = self._down.get(p["name"])
            if until is None or until <= now:
                out.append(p)
        return out

    def _mark(self, name, reason):
        sec = COOLDOWN.get(reason, 30)
        self._down[name] = float("inf") if sec is None else time.time() + sec
        s = self.stats.setdefault(name, {"ok": 0, "fail": 0, "ms": []})
        s["fail"] += 1
        s.setdefault("err", reason)

    def alive(self):
        """任一 provider 可用（本地探活或 ark/openai 配置存在）即视为可用。"""
        if self._alive is not None:
            return self._alive
        ok = False
        for p in self._available():
            if p["kind"] in ("ark", "openai") and p.get("api_key"):
                ok = True
                break
            if p["kind"] == "ollama" and self._ollama_alive(p):
                ok = True
                break
        self._alive = ok
        return ok

    def _ollama_alive(self, p):
        try:
            import requests
            return requests.get(p["base_url"] + "/api/tags", timeout=4).status_code == 200
        except Exception:
            return False

    # ---------- 单 provider 调用 ----------
    def _call_ark(self, p, messages, json_mode, timeout, max_tokens,
                  use_thinking=True):
        import requests
        body = {
            "model": p["model"],
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            # deepseek/glm/kimi 系：必须显式关思考，否则 content 为空
            "thinking": {"type": "disabled"},
        }
        if not use_thinking:
            # 自定义 OpenAI 兼容端点：不带 thinking 字段（未知字段可能被拒）
            body.pop("thinking", None)
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        url = p["base_url"] + "/chat/completions"
        hdr = {"Authorization": "Bearer " + p["api_key"],
               "Content-Type": "application/json"}
        tmo = timeout or p.get("timeout") or self.timeout
        # Plan 套餐并发受限：429 时原地退避重试，不要立刻降级到按量付费（避免重复计费）
        for attempt in range(3):
            r = requests.post(url, json=body, headers=hdr, timeout=tmo)
            if r.status_code != 429:
                break
            wait = 2.0 * (attempt + 1)
            try:
                wait = min(8.0, float(r.headers.get("retry-after") or wait))
            except Exception:
                pass
            time.sleep(wait)
        if r.status_code != 200:
            txt = (r.text or "")[:300]
            code = r.status_code
            reason = ("auth" if code in (401, 403) else
                      "quota" if code == 402 else
                      "ratelimit" if code == 429 else
                      "model" if code in (400, 404) and
                      ("InvalidEndpointOrModel" in txt or "ModelNotOpen" in txt or
                       "does not exist" in txt or "not activated" in txt) else
                      "model" if code == 404 else "server")
            return None, reason + f":{code}" + ("|" + txt[:120] if txt else "")
        data = r.json()
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        content = msg.get("content") or ""
        if isinstance(content, list):  # 少数接口返回分片
            content = "".join(str(c.get("text", "")) if isinstance(c, dict) else str(c)
                              for c in content)
        return (content or None), (None if content else "empty")

    def _call_ollama(self, p, messages, json_mode, timeout, max_tokens):
        import requests
        if not self._ollama_alive(p):
            return None, "server:offline"
        body = {"model": p["model"], "messages": messages, "stream": False,
                "think": False,
                "options": {"temperature": 0, "num_predict": max_tokens, "num_ctx": 8192}}
        if json_mode:
            body["format"] = "json"
        try:
            r = requests.post(p["base_url"] + "/api/chat", json=body,
                              timeout=timeout or p.get("timeout") or self.timeout)
        except Exception as e:
            return None, "timeout" if "timeout" in str(e).lower() else "server"
        if r.status_code != 200:
            return None, f"server:{r.status_code}"
        msg = r.json().get("message") or {}
        content = msg.get("content") or ""
        if not content and msg.get("thinking"):
            content = msg.get("thinking", "")
        return (content or None), (None if content else "empty")

    # ---------- 路由 ----------
    def chat(self, prompt, system="", fmt="json", timeout=None, num_predict=200,
             json_mode=None):
        """按 provider 顺序尝试，成功即返回文本；全链失败返回 None。"""
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        jm = bool(fmt) if json_mode is None else bool(json_mode)
        last = None
        for p in self._available():
            t0 = time.time()
            try:
                if p["kind"] == "ark":
                    content, err = self._call_ark(p, messages, jm, timeout, num_predict)
                elif p["kind"] == "openai":
                    # 自定义 OpenAI 兼容：同 chat/completions，不带 thinking 字段
                    content, err = self._call_ark(p, messages, jm, timeout,
                                                  num_predict, use_thinking=False)
                else:
                    content, err = self._call_ollama(p, messages, jm, timeout, num_predict)
            except Exception as e:
                content, err = None, ("timeout" if "timeout" in str(e).lower() else "server")
            dt = (time.time() - t0) * 1000
            if content:
                s = self.stats.setdefault(p["name"], {"ok": 0, "fail": 0, "ms": []})
                s["ok"] += 1
                s["ms"].append(dt)
                self.active = p["name"]
                return content
            self._mark(p["name"], err.split(":")[0])
            last = (p["name"], err)
        if last:
            self.stats.setdefault("_last_error", {})["err"] = last
        return None

    def _json(self, text):
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                return None
            try:
                return json.loads(m.group(0))
            except Exception:
                return None

    # ---------- 分类 + 命名 ----------
    def classify(self, filename, text, categories, lang="zh", timeout=None):
        """categories: [{"path": "...", "desc": "...", "keywords": [...]}, ...]"""
        if not self.alive():
            return None
        cats = "\n".join(
            f"- {c['path']}" + (f"：{c.get('desc') or ''}".rstrip("：")
                                if (c.get('desc') or c.get('keywords')) else "")
            + (f"（关键词：{'、'.join(c.get('keywords', [])[:6])}）"
               if c.get('keywords') else "")
            for c in categories)
        body = (text or "")[:900]
        prompt = (
            "你是文件归档专家。根据文件名与正文，判断该文件属于哪个类目，并给出一个准确的中文标题。\n"
            f"可选类目（只能选其一，原样输出路径）：\n{cats}\n\n"
            f"文件名：{filename}\n"
            f"正文片段：\n{body}\n\n"
            '只输出 JSON：{"category":"类目路径","title":"不超过25字的中文标题","confidence":0.0}'
        )
        out = self._json(self.chat(prompt, "只输出 JSON，不要解释。", timeout=timeout))
        return self._norm_one(out, categories)

    def _norm_one(self, out, categories):
        if not isinstance(out, dict):
            return None
        cat = str(out.get("category", "")).strip()
        valid = {c["path"] for c in categories}
        if cat not in valid:
            for v in valid:
                if cat and (cat in v or v.split("/")[-1] in cat):
                    cat = v
                    break
            else:
                return None
        try:
            conf = float(out.get("confidence", 0.6))
        except Exception:
            conf = 0.6
        return {"category": cat, "title": str(out.get("title", ""))[:40],
                "confidence": max(0.0, min(1.0, conf))}

    def name_file(self, filename, text, category="", timeout=None):
        body = (text or "")[:900]
        prompt = (
            "给这个文件起一个准确、简洁的中文名（不含扩展名，不加引号，不超过30字，"
            "包含关键主体与时间，不要用「文档」「文件」这类空泛词）。\n"
            f"原文件名：{filename}\n所属类目：{category}\n正文：\n{body}\n\n"
            '只输出 JSON：{"title":"文件名"}'
        )
        out = self._json(self.chat(prompt, "只输出 JSON。", timeout=timeout, num_predict=80))
        if not out:
            return None
        t = str(out.get("title", "")).strip().strip('"“”')
        t = re.sub(r'\.(pdf|docx?|xlsx?|pptx?|txt|md|jpg|png)$', '', t, flags=re.I)
        return t or None

    def ask_json(self, prompt, system="只输出 JSON，不要解释。", timeout=None,
                 num_predict=400):
        return self._json(self.chat(prompt, system, timeout=timeout,
                                    num_predict=num_predict))

    def learn_keywords(self, cat_name, samples, filenames=(), timeout=120):
        """读一类文件的文件名+正文，归纳「能区分这一类」的关键词。"""
        body = "\n---\n".join((s or "")[:500] for s in samples[:6] if s)
        names = "、".join(list(filenames)[:25])
        if len(body) < 60 and len(names) < 10:
            return None
        prompt = (
            "下面是同一个归档类目下的真实文件（含文件名与实际正文片段）。请归纳这类文件的业务特征。\n\n"
            f"类目名：{cat_name}\n"
            f"文件名样例：{names or '（无）'}\n"
            f"正文片段：\n{body or '（无，多为图片/扫描件）'}\n\n"
            "要求：\n"
            "1. keywords：8-14 个中文关键词，用于以后把新文件判进这一类。必须是能区分该类与其他类的业务词，"
            "优先使用文件名里出现的实词（如「营业执照」「经营许可证」「注册证」「授权书」「中标价」「验收单」）；"
            "不要公司名、不要「有限公司」「文件」「资料」这类过泛词、不要残词碎片；\n"
            "2. file_types：这类里常见的具体文件类型/证件名称（3-8 个）；\n"
            "3. name：更规范的中文类目名（4-8 字，不带编号）；\n"
            "4. desc：一句话说明这类放什么。\n"
            '只输出 JSON：{"keywords":["..."],"file_types":["..."],"name":"...","desc":"..."}'
        )
        out = self.ask_json(prompt, timeout=timeout, num_predict=700)
        if not isinstance(out, dict):
            return None

        def clean(lst):
            r = [str(k).strip() for k in (lst or []) if str(k).strip()]
            return [k for k in r if 2 <= len(k) <= 10]
        kws = clean(out.get("keywords")) + clean(out.get("file_types"))
        seen, merged = set(), []
        for k in kws:
            if k not in seen:
                seen.add(k)
                merged.append(k)
        return {"keywords": merged[:14], "name": str(out.get("name") or "").strip(),
                "desc": str(out.get("desc") or "").strip()}

    def review_keywords(self, cat_path, desc, keywords, samples=None, timeout=90):
        """画像清理：让模型剔除与类目语义不符的关键词，返回 (kept, desc)。

        只做「删」不做「造」，避免模型凭空引入原文没有的词导致判类依据失真。
        """
        if not self.alive() or not keywords:
            return keywords, desc
        prompt = (
            "下面是一个文件类目画像，以及从该类目样例文件里抽到的关键词。\n"
            f"类目：{cat_path}\n类目说明：{desc or '（无）'}\n"
            f"样例文件名：{'、'.join(samples or []) or '（无）'}\n"
            f"候选关键词：{'、'.join(keywords)}\n\n"
            "请判断每个关键词是否真的属于这个类目的核心主题。\n"
            "剔除规则：属于其他业务领域（如采购类里的社保/人事）、纯机构名/公司名、碎片词、"
            "过泛（合同/文件/资料/证明）、与本类目说明无关的词。\n"
            "只能从候选词里保留，禁止新增词；至少保留 3 个，最多保留 8 个。\n"
            '只输出 JSON：{"kept":["词1","词2"],"desc":"不超过60字的类目说明"}'
        )
        out = self.ask_json(prompt, timeout=timeout, num_predict=400)
        if not isinstance(out, dict):
            return keywords, desc
        kept_raw = out.get("kept")
        if not isinstance(kept_raw, list):
            return keywords, desc
        keep = [str(x).strip() for x in kept_raw if str(x).strip() in keywords]
        keep = list(dict.fromkeys(keep))
        nd = str(out.get("desc") or "").strip()
        nd = re.sub(r"\s+", "", nd)[:60] or desc
        if len(keep) < 3:
            return keywords, desc
        return keep, nd

    def _probe(self):
        """供调用方判断当前走的是哪个 provider。"""
        a = self.active
        if a:
            return a
        av = [p["name"] for p in self._available()]
        return av[0] if av else None

    def classify_batch(self, items, categories, chunk=6, timeout=180, parallel=2,
                       min_text=20, low_text_cap=0.45, name_first=True, use_samples=True):
        """一次判多条。items=[(filename, text)]。

        增强点（v3.1.0 判类策略）：
        1. few-shot：类目附 2-3 个同类真实样例文件名（categories[].samples），实测 +2pp；
        2. 无正文/机器名（纯哈希、纯数字、IMG_1234 等）→ 不猜，直接 skipped；
        3. 正文过短但文件名可读 → 允许判，但置信度封顶 low_text_cap 并标 low_text。
        返回与 items 等长列表；跳过位为 {"category":None,"skipped":True,...}。
        """
        if not items:
            return []
        if not self.alive():
            return [None] * len(items)

        def readable(fn):
            stem = os.path.splitext(os.path.basename(fn or ""))[0]
            if not stem:
                return False
            if re.fullmatch(r"[0-9a-fA-F]{16,}", stem) or re.fullmatch(r"[0-9a-fA-F\-]{32,36}", stem):
                return False
            if re.fullmatch(r"[\d\s_\-().]+", stem):
                return False
            # 机器名：IMG_20220513_141233 / DSC1234 / 微信图片_2022... / 纯编号
            if re.match(r"(?i)^(img|dsc|pxl|mvimg|screenshot|scan|doc|file|新建|微信图片|录屏|扫描件|扫描|照片|图片|截图|未命名)[ _\-]?\d*$",
                        stem) or re.fullmatch(r"(?i).*[ _\-]\d{6,}$", stem):
                return False
            return bool(re.search(r"[\u4e00-\u9fa5]", stem) or re.search(r"[A-Za-z]{3,}", stem))

        plans = []          # 与 items 对齐：None=跳过，否则 (fn, txt, low_text)
        results = [None] * len(items)
        for i, (fn, txt) in enumerate(items):
            t = (txt or "").strip()
            ok_name = readable(fn)
            if len(t) < min_text and not ok_name:
                results[i] = {"category": None, "title": "", "confidence": 0.0,
                              "skipped": True, "reason": "no-text+machine-name"}
                plans.append(None)
            elif len(t) < min_text:
                plans.append((fn, t, True))
            else:
                plans.append((fn, t, False))

        live = [(i, p) for i, p in enumerate(plans) if p]
        if not live:
            return results

        def cat_block(c):
            head = f"- {c['path']}" + (f"：{c.get('desc')}" if c.get("desc") else "")
            # v3.1.1：样例默认注入。152 条真实样本两轮验证（E 47% / F 49% / G 46%）：
            # 文件名优先 + 样例为最优组合，样例在 152 条上是 +4 条的正收益（41 条小样本看不出）。
            smp = [x for x in (c.get("samples") or []) if x][:3] if use_samples else []
            if smp:
                head += "｜样例文件：" + "、".join(smp)
            return head
        cats = "\n".join(cat_block(c) for c in categories)
        valid = [c["path"] for c in categories]

        parts = [live[k:k + chunk] for k in range(0, len(live), chunk)]

        def run_part(part):
            lines = []
            for j, (gi, (fn, txt, low)) in enumerate(part, 1):
                body = (txt or "")[:420] if txt else "（无正文）"
                lines.append(f"[{j}] 文件名：{fn}\n正文：{body}")
            prompt = (
                "你是文件归档专家。请逐条判断下面每个文件属于哪个类目，并给出准确的中文标题。\n"
                + ("判断依据优先级：文件名里的业务类型 ≥ 正文内容——文件名含「合同/协议/授权书/申报表/"
                   "发票/简历/周报/纪要/招标/中标/名单/花名册/证照/许可证/营业执照」等业务类型词时，"
                   "以该类型定类，正文只用于细分与起标题，不要因正文提到别的业务就改判；"
                   "文件名是哈希/编号/IMG 类机器名时，才完全依据正文。样例只用于理解该类目收什么，"
                   "不要照抄名字。\n" if name_first else
                   "判断依据优先级：正文内容 > 文件名语义；样例文件只用于理解该类目收什么，不要照抄名字。\n")
                + f"可选类目（只能从这些里选，原样输出）：\n{cats}\n\n"
                + "\n\n".join(lines) +
                "\n\n要求：\n"
                "1. 正文为空或只有乱码时，仅按文件名判断；若文件名也无法表达内容，confidence 必须 <=0.4；\n"
                "2. 不确定就压低 confidence，不要猜。\n"
                '只输出 JSON：{"results":[{"id":1,"category":"类目路径","title":"标题","confidence":0.9}]}'
            )
            out = self.ask_json(prompt, timeout=timeout,
                                num_predict=min(1600, 120 * len(part) + 150))
            got = {}
            if isinstance(out, dict) and isinstance(out.get("results"), list):
                for r in out["results"]:
                    if not isinstance(r, dict):
                        continue
                    try:
                        idx = int(r.get("id")) - 1
                    except Exception:
                        continue
                    if not 0 <= idx < len(part):
                        continue
                    cat = str(r.get("category", "")).strip()
                    if cat not in valid:
                        for v in valid:
                            if cat and (cat in v or v.split("/")[-1] in cat):
                                cat = v
                                break
                        else:
                            continue
                    try:
                        conf = float(r.get("confidence", 0.6))
                    except Exception:
                        conf = 0.6
                    conf = max(0.0, min(1.0, conf))
                    if part[idx][1][2]:
                        conf = min(conf, low_text_cap)
                    got[idx] = {"category": cat, "title": str(r.get("title", ""))[:40],
                                "confidence": conf, "low_text": part[idx][1][2]}
            return got

        use_parallel = parallel > 1 and any(
            p["kind"] in ("ark", "openai") for p in self._available())
        if use_parallel:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(parallel, len(parts))) as ex:
                outs = list(ex.map(run_part, parts))
        else:
            outs = [run_part(pt) for pt in parts]
        for bi, got in enumerate(outs):
            for idx, val in got.items():
                gi = parts[bi][idx][0]
                results[gi] = val
        return results

    # ---------- 向量（固定本地，不外发） ----------
    def _ollama_node(self):
        for p in self.providers:
            if p["kind"] == "ollama":
                return p
        return {"base_url": self._local_host, "model": self._local_model,
                "embed_model": self.embed_model, "kind": "ollama", "name": "local"}

    def embed(self, texts, timeout=30):
        if not texts:
            return None
        p = self._ollama_node()
        try:
            import requests
            r = requests.post(p["base_url"] + "/api/embed",
                              json={"model": p.get("embed_model") or self.embed_model,
                                    "input": texts},
                              timeout=timeout)
            if r.status_code != 200:
                return None
            return r.json().get("embeddings")
        except Exception:
            return None

    def topic_cluster(self, labels, texts):
        """返回 {label: cluster_id}，用于判断一个目录里的文件是否讲同一件事。"""
        embs = self.embed([t[:1200] for t in texts])
        if not embs or len(embs) != len(texts):
            return None
        import math

        def cos(a, b):
            s = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(y * y for y in b))
            return s / (na * nb) if na and nb else 0.0

        seeds = [embs[0]]
        return {labels[i]: (0 if cos(embs[i], seeds[0]) >= 0.72 else 1)
                for i in range(len(labels))}

    def report(self):
        """统计：各 provider 成功次数、平均耗时、失败原因。"""
        lines = []
        for name, s in self.stats.items():
            if name.startswith("_"):
                continue
            ms = s.get("ms") or []
            avg = sum(ms) / len(ms) if ms else 0
            lines.append(f"{name}: ok={s.get('ok',0)} fail={s.get('fail',0)} "
                         f"avg={avg:.0f}ms" + (f" err={s.get('err')}" if s.get('err') else ""))
        return " | ".join(lines) or "no-call"
