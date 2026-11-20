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

0.1.6 文件夹级复核：GET /api/review/folders 按文件夹分组返回
（同一文件夹 = 库根下第一级目录，通用容器如 下载 下钻一级，
见 common.routing.review_folder_for）；POST /api/review/folders/approve
与 /reject 对文件夹下全部待复核项逐条走 decide_one（改顶层复用
_recompute_dst_for_top，dup 仍按 holding 区规则）。
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

# 0.1.5：每条复核按 kind 给一句白话“需要你决定什么”（放卡片顶部）。
REVIEW_NOTES = {
    "dup": "这份与另一份内容完全一样，批准即移到重复待确认区，不删",
    "low_conf": "系统没把握、置信度低，请确认分到哪类",
    "blur": "图太糊判不准，请看一眼",
    "ambiguous": "像两类，请选一个",
    "sensitive": "可能含敏感内容，请确认",
    "shell": "空壳文件（内容极小或已损坏），请确认如何处理",
}
DEFAULT_REVIEW_NOTE = "请看一眼再决定"


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
        d["review_note"] = REVIEW_NOTES.get(d.get("kind"), DEFAULT_REVIEW_NOTE)
        out.append(d)
    return out


class DecideBody(BaseModel):
    decision: str  # approved | rejected
    category: str | None = None
    new_name: str | None = None
    tags: list[str] | None = None
    suggested_dst: str | None = None


class FolderDecideBody(BaseModel):
    folder: str  # 分组键 "<top>:<rel_folder>"（见 review_folder_for）
    category: str | None = None  # 整包改顶层（工作/家庭/媒体…）；不填则沿用各条原建议
    tags: list[str] | None = None
    kind: str | None = None  # 只裁定该 kind；不填则整包全部 kind


def _group_rows(rows) -> list:
    """复核行按文件夹分组（0.1.6）：同一文件夹合并为一组，组内保留单条明细
    （前端点开展开逐条处理）。组顺序按首条出现顺序；新入库的项下次列表
    即按同一口径分组，无需额外状态。"""
    roots = _roots()
    groups: dict[str, dict] = {}
    order: list[str] = []
    for r in _enrich(rows):
        p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        key, top, rel, display = RT.review_folder_for(p.get("src") or "", roots)
        g = groups.get(key)
        if g is None:
            g = {"folder": key, "top": top or "", "top_label": RT.top_label(top),
                 "path": rel, "display": display, "count": 0, "kinds": {},
                 "items": []}
            groups[key] = g
            order.append(key)
        g["count"] += 1
        g["kinds"][r.get("kind")] = g["kinds"].get(r.get("kind"), 0) + 1
        g["items"].append(r)
    return [groups[k] for k in order]


def _folder_rows(conn, folder: str, status: str, kind: str | None) -> list:
    """取某文件夹下符合状态/类型的复核行 id（与 _group_rows 同一口径算
    folder_key；整包裁定只对 status='pending' 的行生效）。"""
    if status == "all":
        sql = "SELECT * FROM review_queue"
        args: list = []
        if kind:
            sql += " WHERE kind=?"
            args.append(kind)
        sql += " ORDER BY id"
        rows = conn.execute(sql, args).fetchall()
    else:
        rows = DB.list_review(conn, status=status, kind=kind or None)
    roots = _roots()
    ids = []
    for r in _enrich(rows):
        p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        if RT.review_folder_for(p.get("src") or "", roots)[0] == folder:
            ids.append(r["id"])
    return ids


def _folder_decide(conn, body: FolderDecideBody, decision: str) -> dict:
    """整包裁定（0.1.6）：文件夹下全部待复核项逐条走 decide_one——改顶层
    复用单条的 _recompute_dst_for_top（只换库根、子路径保留），dup 仍按
    holding 区规则、不受改顶层影响；拒绝只改状态、不动文件。"""
    folder = (body.folder or "").strip()
    if not folder:
        raise HTTPException(status_code=400, detail="folder 不能为空")
    if body.category and body.category not in RT.LABEL_TO_TOP:
        raise HTTPException(status_code=400,
                            detail="category 须为 工作/家庭/媒体/收件箱/笔记本备份")
    if body.kind and body.kind not in REVIEW_NOTES:
        raise HTTPException(status_code=400, detail="kind 非法")
    ids = _folder_rows(conn, folder, "pending", body.kind or None)
    results = []
    for rid in ids:
        try:
            decide_body = DecideBody(decision=decision, category=body.category,
                                     tags=body.tags)
            results.append(decide_one(conn, rid, decision, decide_body))
        except HTTPException as e:
            results.append({"ok": False, "id": rid, "moved": False,
                            "note": e.detail})
    return {"folder": folder, "decision": decision, "total": len(ids),
            "results": results}


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


@router.get("/folders")
def list_folders(status: str = "pending", kind: str | None = None,
                 conn=Depends(get_db)):
    """0.1.6：复核队列按文件夹分组返回（组内含单条明细；status=all 用于
    对已裁定行验证分组逻辑，不动任何状态）。"""
    if status == "all":
        sql = "SELECT * FROM review_queue"
        args: list = []
        if kind:
            sql += " WHERE kind=?"
            args.append(kind)
        sql += " ORDER BY id"
        rows = conn.execute(sql, args).fetchall()
        return _group_rows(rows)
    if status not in ("pending", "approved", "rejected"):
        raise HTTPException(status_code=400, detail="status 须为 pending|approved|rejected|all")
    return _group_rows(DB.list_review(conn, status=status, kind=kind or None))


@router.post("/folders/approve")
def folder_approve(body: FolderDecideBody, conn=Depends(get_db)):
    return _folder_decide(conn, body, "approved")


@router.post("/folders/reject")
def folder_reject(body: FolderDecideBody, conn=Depends(get_db)):
    return _folder_decide(conn, body, "rejected")


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
