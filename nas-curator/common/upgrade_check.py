#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.upgrade_check —— 新版本检查（0.1.4）：从 GitHub 读 nas-curator/VERSION。

检查源：GitHub Contents API
  https://api.github.com/repos/bjornwz-cn/muse/contents/nas-curator/VERSION
（raw.githubusercontent.com 在 NAS 出站方向不通，见 0.1.4 报告；git 协议与
api.github.com 均通；未登录 API 限额 60 次/小时，本检查每 6 小时最多一次，
远低于限额。响应 JSON 的 content 字段为 base64 编码的 VERSION 文件内容。）

缓存：/data/upgrade/check.json（与升级请求/状态同目录），TTL 6 小时；
缓存新鲜时不发请求；过期时由调用方在后台线程刷新，前端拿到的永远是缓存值，
检查绝不阻塞页面。检查失败（断网/404/解析错）时返回带 error 的结果，
update_available 按缓存的最新版判断，无缓存时为 False。
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

from .version import compare_versions, parse_version_payload

GITHUB_API_URL = (
    "https://api.github.com/repos/bjornwz-cn/muse/contents/nas-curator/VERSION")
CHECK_TTL_SECONDS = 6 * 3600  # 6 小时
FETCH_TIMEOUT = 10  # 秒：检查带超时，不阻塞


def _cache_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "check.json")


def _read_cache(cache_dir: str) -> dict | None:
    try:
        with open(_cache_path(cache_dir), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("latest"):
            return data
    except (OSError, ValueError):
        pass
    return None


def _write_cache(cache_dir: str, payload: dict) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    tmp = _cache_path(cache_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, _cache_path(cache_dir))


def _fetch_latest(timeout: float = FETCH_TIMEOUT,
                  urlopen=None) -> dict:
    """向 GitHub Contents API 取最新 VERSION，返回归一化的 payload。"""
    opener = urlopen or urllib.request.urlopen
    req = urllib.request.Request(
        GITHUB_API_URL,
        headers={"User-Agent": "nas-curator",
                 "Accept": "application/vnd.github+json"})
    with opener(req, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8"))
    content = body.get("content") or ""
    if not content:
        raise ValueError("GitHub 返回的 VERSION 内容为空")
    text = base64.b64decode("".join(content.split())).decode("utf-8")
    return parse_version_payload(json.loads(text))


def check_latest(cache_dir: str, current_version: str,
                 force: bool = False, timeout: float = FETCH_TIMEOUT,
                 urlopen=None) -> dict:
    """检查新版本。返回：
    {current, latest, update_available, date, notes, checked_at,
     cached(bool), error(str|None)}。永不抛错（失败时 error 字段说明原因）。"""
    now = int(time.time())
    cached = _read_cache(cache_dir)
    if cached and not force:
        age = now - int(cached.get("checked_at") or 0)
        if age < CHECK_TTL_SECONDS:
            latest = cached.get("latest") or ""
            try:
                avail = bool(latest) and compare_versions(latest, current_version) > 0
            except ValueError:
                avail = False
            return {"current": current_version, "latest": latest,
                    "update_available": avail,
                    "date": cached.get("date") or "",
                    "notes": cached.get("notes") or "",
                    "checked_at": int(cached.get("checked_at") or 0),
                    "cached": True, "error": None}
    try:
        latest_payload = _fetch_latest(timeout=timeout, urlopen=urlopen)
    except Exception as e:  # noqa: BLE001 —— 检查失败不阻塞调用方
        latest = (cached or {}).get("latest") or ""
        try:
            avail = bool(latest) and compare_versions(latest, current_version) > 0
        except ValueError:
            avail = False
        return {"current": current_version, "latest": latest,
                "update_available": avail,
                "date": (cached or {}).get("date") or "",
                "notes": (cached or {}).get("notes") or "",
                "checked_at": int((cached or {}).get("checked_at") or 0),
                "cached": bool(cached), "error": f"{type(e).__name__}: {e}"}
    latest = latest_payload["version"]
    try:
        avail = compare_versions(latest, current_version) > 0
    except ValueError:
        avail = False
    payload = {"current": current_version, "latest": latest,
               "update_available": avail,
               "date": latest_payload.get("date") or "",
               "notes": latest_payload.get("notes") or "",
               "checked_at": now, "cached": False, "error": None}
    try:
        _write_cache(cache_dir, payload)
    except OSError:
        pass  # 缓存写失败不影响本次返回
    return payload


def refresh_in_background(cache_dir: str, current_version: str,
                          timeout: float = FETCH_TIMEOUT) -> threading.Thread:
    """后台线程刷新检查结果（供 API 在缓存过期时调用，不阻塞请求）。"""
    t = threading.Thread(target=check_latest, kwargs={
        "cache_dir": cache_dir, "current_version": current_version,
        "force": True, "timeout": timeout}, daemon=True)
    t.start()
    return t
