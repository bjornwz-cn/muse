"""api.rules —— 规则查看与编辑。

GET 返回三份规则文件的当前内容 + 阈值。
PUT 只允许 work / family / media 三个名字：先写 DB（rules 表版本化），
再回写对应的项目内 JSON 文件（写前先备份 .bak）。只写项目内规则文件。
"""
from __future__ import annotations

import json
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from common import db as DB
from .deps import get_db, get_settings

router = APIRouter(prefix="/api/rules", tags=["rules"])

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RULE_FILES = {
    "work": os.path.join(PROJECT_ROOT, "curator_core", "rules.baijing-supply-v8.json"),
    "family": os.path.join(PROJECT_ROOT, "curator_core", "rules.family.json"),
    "media": os.path.join(PROJECT_ROOT, "media_core", "config.json"),
}


class RulesPutBody(BaseModel):
    payload: dict


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@router.get("")
def get_rules():
    settings = get_settings()
    return {
        "work": _read_json(RULE_FILES["work"]),
        "family": _read_json(RULE_FILES["family"]),
        "media": _read_json(RULE_FILES["media"]),
        "thresholds": settings["thresholds"],
    }


@router.put("/{name}")
def put_rules(name: str, body: RulesPutBody, conn=Depends(get_db)):
    if name not in RULE_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"未知规则集：{name}（可选：{', '.join(RULE_FILES)}）")
    rule_id, version = DB.save_rules(conn, name, body.payload, activate=True)
    path = RULE_FILES[name]
    try:
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(body.payload, f, ensure_ascii=False, indent=2)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"回写规则文件失败：{e}")
    return {"ok": True, "name": name, "id": rule_id, "version": version,
            "file": path}
