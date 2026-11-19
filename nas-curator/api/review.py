"""api.review —— 复核队列：列表 / 单条裁定 / 批量裁定。

approved 时：若复核 payload 里有 suggested_dst 且源文件仍存在，用
curator_core.engine.apply_plan 单条执行移动（dry_run=False，uid/gid 1000），
成功则写 operations 并把对应 plan_item 置 done；源文件不存在则只改状态。
rejected：只改状态。绝不删除文件。
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from common import db as DB
from .deps import get_db

router = APIRouter(prefix="/api/review", tags=["review"])


class DecideBody(BaseModel):
    decision: str  # approved | rejected
    category: str | None = None
    new_name: str | None = None
    tags: list[str] | None = None
    suggested_dst: str | None = None


class BulkBody(BaseModel):
    ids: list[int]
    decision: str


def _mark_plan_item_done(conn, plan_id, src_path):
    """把同一 plan 下该源文件的 plan_item 置 done（只改 status，不删行）。"""
    if plan_id is None or not src_path:
        return
    conn.execute(
        "UPDATE plan_items SET status='done' WHERE plan_id=? AND src_path=?",
        (plan_id, src_path))
    conn.commit()


def _persist_tags(conn, file_path, tags):
    """复核时用户给的 tags：文件已在 files 表登记时才写 file_tags；未登记则忽略。"""
    if not tags or not file_path:
        return
    try:
        row = DB.get_file_by_path(conn, file_path)
        if not row:
            return
        for name in tags:
            tag_id = DB.ensure_tag(conn, name)
            conn.execute(
                "INSERT OR IGNORE INTO file_tags (file_id, tag_id, source)"
                " VALUES (?, ?, 'manual')", (row["id"], tag_id))
        conn.commit()
    except Exception:
        pass  # 标签写失败不阻塞复核裁定本身


def decide_one(conn, review_id: int, decision: str, body: DecideBody | None = None) -> dict:
    """单条裁定（供单条与批量共用）。返回 {ok, moved, dst?, note?}。"""
    if decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision 须为 approved|rejected")
    row = conn.execute("SELECT * FROM review_queue WHERE id=?", (review_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"复核 #{review_id} 不存在")
    review = dict(row)
    payload = {}
    if review.get("payload"):
        try:
            payload = json.loads(review["payload"])
        except ValueError:
            payload = {}
    DB.decide_review(conn, review_id, decision, decided_by="user")
    if decision == "rejected":
        return {"ok": True, "id": review_id, "moved": False, "note": "已驳回，仅更新状态"}

    src = payload.get("src") or ""
    dst = (body.suggested_dst if body and body.suggested_dst
           else payload.get("suggested_dst") or "")
    if body and body.new_name and dst:
        dst = os.path.join(os.path.dirname(dst), body.new_name)
    if not src or not dst:
        return {"ok": True, "id": review_id, "moved": False,
                "note": "无 suggested_dst 或源路径，仅更新状态"}
    if not os.path.exists(src):
        return {"ok": True, "id": review_id, "moved": False,
                "note": "源文件不存在，仅更新复核状态，未移动"}
    from curator_core import engine as CE
    item = {"src": src, "dst": dst, "action": "move",
            "category": (body.category if body and body.category
                         else payload.get("category") or "06-其他"),
            "sha256": payload.get("sha256") or ""}
    root = os.path.dirname(os.path.abspath(src)) or "/"
    operations, report = CE.apply_plan([item], root, dry_run=False, uid=1000, gid=1000)
    if not operations:
        failed = (report.get("failed") or [{}])[0]
        return {"ok": True, "id": review_id, "moved": False,
                "note": f"移动失败：{failed.get('reason', '未知原因')}"}
    op = operations[0]
    DB.add_operation(conn, review.get("plan_id"), op["src_path"], op["dst_path"],
                     op.get("sha256"))
    _mark_plan_item_done(conn, review.get("plan_id"), src)
    if body and body.tags:
        _persist_tags(conn, op["dst_path"], body.tags)
    return {"ok": True, "id": review_id, "moved": True, "dst": op["dst_path"]}


@router.get("")
def list_review(status: str = "pending", kind: str | None = None,
                conn=Depends(get_db)):
    if status == "all":
        sql = "SELECT * FROM review_queue"
        args: list = []
        if kind:
            sql += " WHERE kind=?"
            args.append(kind)
        sql += " ORDER BY id"
        rows = conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("payload"):
                try:
                    d["payload"] = json.loads(d["payload"])
                except ValueError:
                    pass
            out.append(d)
        return out
    if status not in ("pending", "approved", "rejected"):
        raise HTTPException(status_code=400, detail="status 须为 pending|approved|rejected|all")
    return DB.list_review(conn, status=status, kind=kind or None)


@router.post("/{review_id}/decide")
def decide(review_id: int, body: DecideBody, conn=Depends(get_db)):
    return decide_one(conn, review_id, body.decision, body)


@router.post("/bulk")
def bulk(body: BulkBody, conn=Depends(get_db)):
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision 须为 approved|rejected")
    results = []
    for rid in body.ids:
        try:
            results.append(decide_one(conn, rid, body.decision))
        except HTTPException as e:
            results.append({"ok": False, "id": rid, "moved": False,
                            "note": e.detail})
    return {"results": results}
