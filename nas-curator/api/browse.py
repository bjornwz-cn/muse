"""api.browse —— 按分类/标签浏览 + 文件详情（只读）。"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, HTTPException

from common import db as DB
from .deps import get_db, get_settings

router = APIRouter(prefix="/api/browse", tags=["browse"])


def _fs_entries(d: str) -> list[dict]:
    """DB 为空时的实时列目录（只读 scandir，不递归）。"""
    entries = []
    try:
        with os.scandir(d) as it:
            for e in sorted(it, key=lambda x: x.name):
                try:
                    st = e.stat(follow_symlinks=False)
                    entries.append({"path": e.path, "name": e.name,
                                    "is_dir": e.is_dir(follow_symlinks=False),
                                    "size": st.st_size, "mtime": st.st_mtime,
                                    "ext": os.path.splitext(e.name)[1].lower(),
                                    "kind": None})
                except OSError:
                    continue
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"无法列目录 {d}：{exc}")
    return entries


@router.get("")
def browse(dir: str | None = None, tag: str | None = None, q: str | None = None,
           limit: int = 100, conn=Depends(get_db)):
    total = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    if total == 0:
        # DB 为空：实时列目录。未给 dir 则列各库根目录本身。
        if not dir:
            settings = get_settings()
            return {"source": "fs", "entries": [
                {"path": lib.get("root"), "name": lib.get("name"),
                 "is_dir": True, "size": None, "mtime": None,
                 "ext": "", "kind": lib.get("kind")}
                for lib in settings["libraries"]]}
        return {"source": "fs", "entries": _fs_entries(dir)[:limit]}

    sql = ("SELECT f.* FROM files f")
    args: list = []
    wheres = []
    if tag:
        sql += (" JOIN file_tags ft ON ft.file_id=f.id"
                " JOIN tags t ON t.id=ft.tag_id")
        wheres.append("t.name=?")
        args.append(tag)
    if dir:
        d = dir.rstrip("/")
        wheres.append("(f.path=? OR f.path LIKE ?)")
        args.extend([d, d + "/%"])
    if q:
        wheres.append("f.path LIKE ?")
        args.append(f"%{q}%")
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY f.path LIMIT ?"
    args.append(limit)
    rows = [dict(r) for r in conn.execute(sql, args)]
    return {"source": "db", "entries": rows}


@router.get("/file")
def file_detail(path: str, conn=Depends(get_db)):
    row = DB.get_file_by_path(conn, path)
    if row:
        info = dict(row)
    elif os.path.exists(path):
        st = os.stat(path)
        info = {"path": path, "size": st.st_size, "mtime": st.st_mtime,
                "sha256": None, "kind": None, "ext": os.path.splitext(path)[1].lower(),
                "status": None}
    else:
        raise HTTPException(status_code=404, detail=f"文件不存在：{path}")
    # 分类依据：最近一条涉及该路径的 plan_item（源或目标）。
    cat = reason = evidence = None
    item = conn.execute(
        "SELECT * FROM plan_items WHERE src_path=? OR dst_path=?"
        " ORDER BY id DESC LIMIT 1", (path, path)).fetchone()
    if item:
        cat, reason, evidence = item["category"], item["reason"], item["evidence"]
    tags = []
    if row:
        tags = [r["name"] for r in conn.execute(
            "SELECT t.name FROM file_tags ft JOIN tags t ON t.id=ft.tag_id"
            " WHERE ft.file_id=? ORDER BY t.name", (row["id"],))]
    history = [dict(r) for r in conn.execute(
        "SELECT * FROM operations WHERE src_path=? OR dst_path=? ORDER BY id",
        (path, path))]
    return {**info, "category": cat, "reason": reason, "evidence": evidence,
            "tags": tags, "history": history}
