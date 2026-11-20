"""api.upgrade —— 半自动升级 API（0.1.4）。

三端点：
- GET  /api/upgrade/check   新版本检查（读缓存，过期时后台刷新，不阻塞页面）
- POST /api/upgrade/request  提交升级请求（只写标志文件，不执行升级）
- GET  /api/upgrade/status  升级进度（读 NAS 侧 watcher 写的状态文件）

升级本身绝不在容器内执行（容器不能重启自己，也不挂 docker socket）：
Web 端把请求写进数据卷 /data/upgrade/request.json，NAS 侧 systemd timer
每分钟跑的 watcher 拾取后执行 scripts/upgrade.sh，进度写
/data/upgrade/status.json 供本接口读取。升级只在用户点按钮时触发。
"""
from __future__ import annotations

import json
import os
import time

from fastapi import APIRouter, Depends, HTTPException

from common import upgrade_check as UC
from common.version import compare_versions, parse_version, read_local_version
from .deps import get_db_path, get_upgrade_dir

router = APIRouter(prefix="/api/upgrade", tags=["upgrade"])


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _current_version() -> str:
    return read_local_version()["version"]


@router.get("/check")
def check_update():
    """新版本检查：返回缓存结果；缓存过期时后台刷新并标记 refreshing。"""
    current = _current_version()
    upgrade_dir = get_upgrade_dir()
    result = UC.check_latest(upgrade_dir, current)
    cached_at = int(result.get("checked_at") or 0)
    stale = (not cached_at) or (int(time.time()) - cached_at >= UC.CHECK_TTL_SECONDS)
    if stale and not result.get("error"):
        # 缓存缺失或过期：后台刷新，本次先回旧值（不阻塞页面）
        UC.refresh_in_background(upgrade_dir, current)
        result = dict(result)
        result["refreshing"] = True
    else:
        result["refreshing"] = False
    return result


@router.get("/status")
def upgrade_status():
    """升级进度：合并请求文件与 watcher 写的状态文件。无文件时 state=idle。"""
    current = _current_version()
    upgrade_dir = get_upgrade_dir()
    req = _read_json(os.path.join(upgrade_dir, "request.json")) or {}
    st = _read_json(os.path.join(upgrade_dir, "status.json")) or {}
    state = st.get("state") or req.get("status") or "idle"
    if state == "requested":
        state = "requested"
    return {
        "state": state,  # idle | requested | running | done | failed | dry-run-ok
        "stage": st.get("stage") or "",
        "message": st.get("message") or "",
        "target_version": st.get("target_version") or req.get("target_version") or "",
        "current_version": current,
        "started_at": st.get("started_at") or "",
        "finished_at": st.get("finished_at") or "",
        "error": st.get("error") or "",
        "rolled_back": bool(st.get("rolled_back")),
    }


@router.post("/request", status_code=202)
def request_upgrade(body: dict):
    """提交升级请求：校验目标版本 > 当前版本、无进行中的升级后写标志文件。"""
    target = str((body or {}).get("target_version") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="缺少 target_version")
    try:
        parse_version(target)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法版本号：{target}")
    current = _current_version()
    try:
        if compare_versions(target, current) <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"目标版本 {target} 不高于当前版本 {current}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    upgrade_dir = get_upgrade_dir()
    st = _read_json(os.path.join(upgrade_dir, "status.json")) or {}
    if st.get("state") in ("requested", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"已有升级在进行中（{st.get('state')}，目标 "
                   f"{st.get('target_version') or ''}），请稍后再试")
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    req = {"target_version": target, "requested_at": now, "status": "requested"}
    _write_json_atomic(os.path.join(upgrade_dir, "request.json"), req)
    _write_json_atomic(os.path.join(upgrade_dir, "status.json"), {
        "state": "requested", "stage": "queued", "message": "已提交升级请求，等待 NAS 侧 watcher 拾取",
        "target_version": target, "current_version": current,
        "started_at": now, "finished_at": "", "error": "", "rolled_back": False})
    return {"ok": True, "target_version": target, "state": "requested"}
