"""api.review —— 复核队列：列表 / 单条裁定 / 批量裁定。

approved 时：
- dup：移入同库 99-待确认/06-重复待确认 holding 区（绝不删除文件），
  成功则写 operations 并把对应 plan_item 置 done；源文件不存在则只改状态。
- 其他：若复核 payload 里有 suggested_dst 且源文件仍存在，用
  curator_core.engine.apply_plan 单条执行移动（dry_run=False，uid/gid 1000），
  成功则写 operations 并把对应 plan_item 置 done；源文件不存在则只改状态。
- 下拉「改分类」改了顶层（工作/家庭/媒体）时重算 dst：有 suggested_dst
  则只换库根，否则按新顶层默认一级目录落点；下拉未改则沿用原建议。
rejected：只改状态。绝不删除文件。
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from common import db as DB
from common import routing as RT
from .deps import get_db, get_settings

router = APIRouter(prefix="/api/review", tags=["review"])

MEDIA_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".mpg", ".mpeg",
                    ".m4v", ".webm", ".wmv", ".flv"}

DUP_CATEGORY = "99-待确认/06-重复待确认"


def _roots() -> dict:
    """libraries.json -> {top_id: root}（库根缺失时返回空 dict，行为退化为旧口径）。"""
    try:
        cfg = get_settings().get("_raw") or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    return RT.roots_from_config(cfg)


def _recompute_dst_for_top(src: str, suggested_dst: str, new_top: str,
                           roots: dict) -> str:
    """下拉改顶层后重算 dst：有 suggested_dst 则只换库根；否则按新顶层默认落点：
    工作->归档/，家庭-><人>/（取源路径的人，无则待分人），媒体->照片/视频/。"""
    name = os.path.basename(src or "")
    if suggested_dst:
        old_top = RT.library_top(suggested_dst, roots)
        if old_top and old_top in roots:
            rel = os.path.relpath(suggested_dst, roots[old_top])
            if rel not in (".", ""):
                return os.path.join(roots[new_top], rel)
        return os.path.join(roots[new_top], name)
    if new_top == "work":
        return os.path.join(roots["work"], "归档", name)
    if new_top == "family":
        person = RT.family_person_of(src, roots.get("family")) or RT.UNASSIGNED_PERSON
        return os.path.join(roots["family"], person, name)
    if new_top == "media":
        ext = os.path.splitext(name)[1].lower()
        sub = "视频" if ext in MEDIA_VIDEO_EXTS else "照片"
        return os.path.join(roots["media"], sub, name)
    return ""


def _enrich(rows) -> list:
    """列表每行补 current_top（中文）/ current_top_id（供下拉默认选中）。"""
    roots = _roots()
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("payload"), str):
            try:
                d["payload"] = json.loads(d["payload"])
            except ValueError:
                pass
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        top = RT.library_top(p.get("src") or "", roots)
        d["current_top"] = RT.top_label(top)
        d["current_top_id"] = top or ""
        out.append(d)
    return out


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


def _execute_move(conn, review, src, dst, category, sha256, tags):
    """apply_plan 单条移动 + 写 operations + plan_item 置 done。
    返回 (moved, dst_path|失败原因)。"""
    from curator_core import engine as CE
    item = {"src": src, "dst": dst, "action": "move",
            "category": category or "06-其他", "sha256": sha256 or ""}
    root = os.path.dirname(os.path.abspath(src)) or "/"
    operations, report = CE.apply_plan([item], root, dry_run=False, uid=1000, gid=1000)
    if not operations:
        if report.get("already"):
            return False, "已在目标位置，无需移动"
        failed = (report.get("failed") or [{}])[0]
        return False, f"移动失败：{failed.get('reason', '未知原因')}"
    op = operations[0]
    DB.add_operation(conn, review.get("plan_id"), op["src_path"], op["dst_path"],
                     op.get("sha256"))
    _mark_plan_item_done(conn, review.get("plan_id"), src)
    if tags:
        _persist_tags(conn, op["dst_path"], tags)
    return True, op["dst_path"]


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
    kind = review.get("kind") or ""
    roots = _roots()
    tags = body.tags if body and body.tags else None
    # dup：批准 = 移入 holding 区，绝不删除（0.1.2）。下拉改顶层对 dup 不生效，
    # holding 区按源文件所在顶层定。
    if kind == "dup":
        if not src or not os.path.exists(src):
            return {"ok": True, "id": review_id, "moved": False,
                    "note": "源文件不存在，仅更新复核状态，未移动"}
        dst = RT.dup_holding_dst(src, roots)
        moved, res = _execute_move(
            conn, review, src, dst,
            payload.get("category") or DUP_CATEGORY,
            payload.get("sha256") or "", tags)
        if not moved:
            return {"ok": True, "id": review_id, "moved": False, "note": res}
        return {"ok": True, "id": review_id, "moved": True, "dst": res,
                "note": "已移入重复待确认区（未删除，可找回）"}

    dst = (body.suggested_dst if body and body.suggested_dst
           else payload.get("suggested_dst") or "")
    # 下拉改顶层（只认 工作/家庭/媒体 三个顶层名）时重算 dst。
    if body and body.category and body.category in RT.LABEL_TO_TOP:
        new_top = RT.LABEL_TO_TOP[body.category]
        current_top = RT.library_top(src, roots)
        if new_top != current_top and new_top in roots:
            dst = _recompute_dst_for_top(src, dst, new_top, roots)
    if body and body.new_name and dst:
        dst = os.path.join(os.path.dirname(dst), body.new_name)
    if not src or not dst:
        return {"ok": True, "id": review_id, "moved": False,
                "note": "无 suggested_dst 或源路径，仅更新状态"}
    if not os.path.exists(src):
        return {"ok": True, "id": review_id, "moved": False,
                "note": "源文件不存在，仅更新复核状态，未移动"}
    moved, res = _execute_move(
        conn, review, src, dst,
        (body.category if body and body.category
         else payload.get("category") or "06-其他"),
        payload.get("sha256") or "", tags)
    if not moved:
        return {"ok": True, "id": review_id, "moved": False, "note": res}
    return {"ok": True, "id": review_id, "moved": True, "dst": res}


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
        return _enrich(rows)
    if status not in ("pending", "approved", "rejected"):
        raise HTTPException(status_code=400, detail="status 须为 pending|approved|rejected|all")
    return _enrich(DB.list_review(conn, status=status, kind=kind or None))


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
