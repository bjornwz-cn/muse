"""api.review —— 复核队列：列表 / 单条裁定 / 批量裁定 / 重分类。

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
0.2.5：列表与文件夹接口分页（§5.1）；review_suggest 生效，队列按
suggest_level 二分（§3.3）；杂项 kind 细分且 Web 默认过滤（§3.4）；
POST /api/review/reclassify 按新阈值重评积压行（§4.3-4）；
GET /api/review/dup/dry-run 为 dup 整批批准先报数（§3.6）。
"""
from __future__ import annotations

import datetime
import json
import os
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from common import db as DB
from common import routing as RT
from .deps import get_db, get_settings, load_media_config

router = APIRouter(prefix="/api/review", tags=["review"])

MEDIA_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".mpg", ".mpeg",
                    ".m4v", ".webm", ".wmv", ".flv"}

DUP_CATEGORY = "99-待确认/06-重复待确认"

# 0.1.5：每条复核按 kind 给一句白话“需要你决定什么”（放卡片顶部）。
# 0.2.5 §3.4：unreadable/unsupported/no_text 从 low_conf 细分出来。
REVIEW_NOTES = {
    "dup": "这份与另一份内容完全一样，批准即移到重复待确认区，不删",
    "low_conf": "系统没把握、置信度低，请确认分到哪类",
    "blur": "图太糊判不准，请看一眼",
    "ambiguous": "像两类，请选一个",
    "sensitive": "可能含敏感内容，请确认",
    "shell": "空壳文件（内容极小或已损坏），请确认如何处理",
    "unreadable": "文件读不了（空或损坏），请确认如何处理",
    "unsupported": "格式不支持，请确认如何处理",
    "no_text": "没有可读正文，请确认如何处理",
}
DEFAULT_REVIEW_NOTE = "请看一眼再决定"

# 0.2.5 §3.4：杂项 kind（Web 默认过滤，不占人工队列）。
MISC_KINDS = ("unreadable", "unsupported", "no_text")


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
    """列表每行补 current_top（中文）/ current_top_id（供下拉默认选中）、
    confidence_original（压之前的原始置信度；老行从 reason/evidence 恢复）
    与 suggest_level（0.2.5 §3.3：原始置信度 >= review_suggest 为
    'suggest' 建议批准，否则 'manual' 需人工判断）。"""
    roots = _roots()
    try:
        review_suggest = float((get_settings().get("thresholds") or {}).get(
            "review_suggest", 0.60))
    except (TypeError, ValueError):
        review_suggest = 0.60
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
        conf_orig = RT.recover_confidence_original(p)
        p["confidence_original"] = conf_orig
        d["confidence_original"] = conf_orig
        d["suggest_level"] = ("suggest" if conf_orig >= review_suggest
                              else "manual")
        # 建议目标的顶层（供下拉默认选中；无建议目标时为空）。
        sug_top = RT.library_top(p.get("suggested_dst") or "", roots)
        d["suggested_top"] = RT.top_label(sug_top) if sug_top else ""
        d["suggested_top_id"] = sug_top or ""
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


def _group_rows(rows, items_per_group: int = 20) -> list:
    """复核行按文件夹分组（0.1.6）：同一文件夹合并为一组，组内保留单条明细
    （前端点开展开逐条处理）。组顺序按首条出现顺序；新入库的项下次列表
    即按同一口径分组，无需额外状态。
    0.2.5 §5.1：每组只带前 items_per_group 条明细（has_more 标明还有更多，
    前端展开时按文件夹调 /api/review?folder=…&offset=… 续取），避免
    单组上千条把响应撑爆。"""
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
                 "items": [], "has_more": False}
            groups[key] = g
            order.append(key)
        g["count"] += 1
        g["kinds"][r.get("kind")] = g["kinds"].get(r.get("kind"), 0) + 1
        if len(g["items"]) < items_per_group:
            g["items"].append(r)
        else:
            g["has_more"] = True
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
    category: str | None = None  # 0.2.5 §3.2：批量批准可带分类，透传给 decide_one


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


def decide_one(conn, review_id: int, decision: str, body: DecideBody | None = None,
               decided_by: str = "user") -> dict:
    """单条裁定（供单条与批量共用）。返回 {ok, moved, dst?, note?}。
    decided_by 缺省 'user'；重分类自动批准时传 'system:reclassify-0.2.5'。"""
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
    DB.decide_review(conn, review_id, decision, decided_by=decided_by)
    # v0.2.2 自学习：记决策留痕（预测 vs 用户最终判定）；学习失败不影响裁定。
    try:
        from common import learn as _learn
        _learn.record_review_decision(
            conn, review, payload, decision,
            final_category=(body.category if body and body.category else ""),
            final_dst=(body.suggested_dst if body and body.suggested_dst else ""))
    except Exception:
        pass
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


def _load_rows(conn, status: str, kind: str | None) -> list:
    """按状态/类型取复核行（dict 列表，payload 已解析为 dict）。"""
    if status == "all":
        sql = "SELECT * FROM review_queue"
        args: list = []
        if kind:
            sql += " WHERE kind=?"
            args.append(kind)
        sql += " ORDER BY id"
        rows = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            if isinstance(d.get("payload"), str):
                try:
                    d["payload"] = json.loads(d["payload"])
                except ValueError:
                    pass
            rows.append(d)
        return rows
    if status not in ("pending", "approved", "rejected"):
        raise HTTPException(status_code=400, detail="status 须为 pending|approved|rejected|all")
    return DB.list_review(conn, status=status, kind=kind or None)


def _paginate(rows: list, limit: int, offset: int) -> tuple[list, int]:
    return rows[offset:offset + limit], len(rows)


@router.get("")
def list_review(status: str = "pending", kind: str | None = None,
                folder: str | None = None, hide_misc: bool = False,
                limit: int = 100, offset: int = 0,
                conn=Depends(get_db)):
    """0.2.5 §5.1：分页返回 {"items", "total", "limit", "offset"}；
    folder 只取某文件夹下的行；hide_misc 过滤杂项 kind（§3.4）。"""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    rows = _load_rows(conn, status, kind)
    if hide_misc:
        rows = [r for r in rows if r.get("kind") not in MISC_KINDS]
    if folder:
        roots = _roots()
        kept = []
        for r in rows:
            p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
            if RT.review_folder_for(p.get("src") or "", roots)[0] == folder:
                kept.append(r)
        rows = kept
    page, total = _paginate(rows, limit, offset)
    return {"items": _enrich(page), "total": total, "limit": limit,
            "offset": offset}


@router.get("/folders")
def list_folders(status: str = "pending", kind: str | None = None,
                 hide_misc: bool = False,
                 limit: int = 20, offset: int = 0,
                 conn=Depends(get_db)):
    """0.1.6：复核队列按文件夹分组返回（组内含前 20 条明细，has_more 标明
    还有更多；0.2.5 §5.1 分页返回 {"groups", "total", "limit", "offset"}，
    total 为组数；hide_misc 过滤杂项 kind）。"""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    rows = _load_rows(conn, status, kind)
    if hide_misc:
        rows = [r for r in rows if r.get("kind") not in MISC_KINDS]
    groups = _group_rows(rows)
    page, total = _paginate(groups, limit, offset)
    return {"groups": page, "total": total,
            "total_items": sum(g["count"] for g in groups),
            "limit": limit, "offset": offset}


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
            # 0.2.5 §3.2：把 category 透传给 decide_one（此前不传 body，
            # 批量批准只改状态、不搬文件）。
            results.append(decide_one(
                conn, rid, body.decision,
                DecideBody(decision=body.decision, category=body.category)))
        except HTTPException as e:
            results.append({"ok": False, "id": rid, "moved": False,
                            "note": e.detail})
    return {"results": results}


# ---------------------------------------------------------------- 0.2.5 §4.3-4：重分类

def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _rebuild_dst(src: str, category: str, payload: dict, roots: dict) -> str:
    """按 category 重建建议目标（完整文件路径；重建不出返回 ""）。
    媒体：媒体库 + target_dir(category)；文档：工作库 + 类目；家庭成员
    工作内容：家庭库 + 人名 + 工作 + 内容子目录。"""
    name = os.path.basename(src or "")
    if not name:
        return ""
    ext = os.path.splitext(name)[1].lower()
    if ext in RT.MEDIA_EXTS:
        try:
            from media_core.curate import target_dir as _target_dir
            media_cfg = load_media_config()
            cfg = {"photo_dirs": media_cfg.get("photo_dirs") or {},
                   "video_dirs": media_cfg.get("video_dirs") or {},
                   "review_dirs": media_cfg.get("review_dirs")
                   or {"待确认": "_Review/待确认"}}
            kind_cn = "照片" if ext in RT.MEDIA_IMAGE_EXTS else "视频"
            rel = _target_dir(cfg, kind_cn, category or "待确认")
            if roots.get("media"):
                return os.path.join(roots["media"], rel, name)
        except Exception:
            return ""
        return ""
    if category and re.match(r"^\d{2}-", category):
        if roots.get("work"):
            return os.path.join(roots["work"], category, name)
        return ""
    person = (payload or {}).get("doc_party") or ""
    if person in RT.FAMILY_PERSONS and roots.get("family"):
        return RT.rewrite_family_work_dst(roots["family"], person, name,
                                          category or "06-其他")
    return ""


def _normalize_suggested_dst(suggested: str, src: str, category: str,
                             payload: dict, roots: dict) -> str:
    """把 payload 里的 suggested_dst 归一化为完整文件路径：
    - 绝对路径且以文件名为结尾 -> 直接用；
    - 绝对路径但不以文件名为结尾 -> 末段带扩展名视为错名文件路径（换名），
      否则视为目录（拼上文件名）；
    - 相对路径（批量脚本留下的 '视频/儿童成长' 式）-> 按首段拼到对应库根；
    - 空或无法识别 -> 按 category 重建；重建不出返回 ""。"""
    name = os.path.basename(src or "")
    if not name:
        return ""
    sug = (suggested or "").strip()
    if sug and not os.path.isabs(sug):
        first = sug.replace("\\", "/").split("/")[0]
        if first in ("照片", "视频") and roots.get("media"):
            sug = os.path.join(roots["media"], sug)
        elif re.match(r"^\d{2}-", first) and roots.get("work"):
            sug = os.path.join(roots["work"], sug)
        elif first in RT.FAMILY_PERSONS and roots.get("family"):
            sug = os.path.join(roots["family"], sug)
        else:
            sug = ""
    if sug and os.path.isabs(sug):
        if sug.endswith(name):
            return sug
        last = sug.rstrip("/").rsplit("/", 1)[-1]
        if "." in last and not last.startswith("."):
            return os.path.join(os.path.dirname(sug), name)
        return os.path.join(sug, name)
    return _rebuild_dst(src, category, payload, roots)


class ReclassifyBody(BaseModel):
    ids: list[int] | None = None
    folder: str | None = None
    apply: bool = False
    limit: int = 200


@router.post("/reclassify")
def reclassify(body: ReclassifyBody, conn=Depends(get_db)):
    """0.2.5 §4.3-4：按新阈值重评已在队列的复核行（解决 _ReviewSkipper
    让改阈值后旧文件不重分类的问题；不重跑模型、不清指纹）。
    - 恢复原始置信度（confidence_original / reason / evidence）；
    - 按分模态阈值判定是否可自动，归一化/重建 suggested_dst；
    - 默认只重评并写回 payload（不移动文件）；apply=true 时对可自动的
      行走正常批准流程移动文件（写 operations，可回滚）。
    - dup 行不参与（另走 dup dry-run/批量批准）；无有效分类（待确认/
      模糊）或源文件不存在的行跳过。单次上限 1000 行。"""
    limit = max(1, min(int(body.limit or 200), 1000))
    thresholds = (get_settings().get("thresholds") or {})
    roots = _roots()
    if body.ids is not None:
        if not body.ids:
            return {"total": 0, "eligible": 0, "applied": 0, "moved": 0,
                    "not_eligible": 0, "skipped": 0, "results": []}
        placeholders = ",".join("?" for _ in body.ids)
        rows = conn.execute(
            "SELECT * FROM review_queue WHERE id IN (%s)"
            " AND status='pending' ORDER BY id" % placeholders,
            list(body.ids)).fetchall()
        rows = [dict(r) for r in rows]
        for r in rows:
            if isinstance(r.get("payload"), str):
                try:
                    r["payload"] = json.loads(r["payload"])
                except ValueError:
                    r["payload"] = {}
    else:
        rows = DB.list_review(conn, status="pending")
        if body.folder:
            kept = []
            for r in rows:
                p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
                if RT.review_folder_for(p.get("src") or "", roots)[0] == body.folder:
                    kept.append(r)
            rows = kept
    rows = rows[:limit]
    results = []
    eligible_n = applied_n = moved_n = not_eligible_n = skipped_n = 0
    for r in rows:
        rid = r["id"]
        kind = r.get("kind") or ""
        payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        src = payload.get("src") or ""
        category = payload.get("category") or ""
        if kind == "dup":
            skipped_n += 1
            results.append({"id": rid, "ok": False, "eligible": False,
                            "note": "dup 行不参与重分类，另走 dup 批量批准"})
            continue
        if not src or not os.path.exists(src):
            skipped_n += 1
            results.append({"id": rid, "ok": False, "eligible": False,
                            "note": "源文件不存在"})
            continue
        if category in ("", "待确认", "模糊"):
            skipped_n += 1
            results.append({"id": rid, "ok": False, "eligible": False,
                            "note": "无有效分类"})
            continue
        conf_orig = RT.recover_confidence_original(payload)
        ext = os.path.splitext(src)[1].lower()
        item = {"action": "move",
                "kind": "media" if ext in RT.MEDIA_EXTS else "document",
                "ext": ext, "confidence": conf_orig,
                "reason": payload.get("reason") or "",
                "evidence": payload.get("evidence") or ""}
        dst = _normalize_suggested_dst(payload.get("suggested_dst") or "",
                                       src, category, payload, roots)
        thr = RT.auto_threshold_for(item, thresholds)
        eligible = bool(dst) and RT.is_auto_apply_eligible(item, thresholds=thresholds)
        payload["confidence_original"] = conf_orig
        payload["suggested_dst"] = dst
        payload["reclassify"] = {"eligible": eligible, "threshold": thr,
                                 "at": _now_iso()}
        conn.execute("UPDATE review_queue SET payload=? WHERE id=?",
                     (json.dumps(payload, ensure_ascii=False), rid))
        conn.commit()
        if not eligible:
            not_eligible_n += 1
            results.append({"id": rid, "ok": True, "eligible": False,
                            "confidence_original": conf_orig, "threshold": thr,
                            "note": "未达新阈值" if dst else "重建不出建议目标"})
            continue
        eligible_n += 1
        if not body.apply:
            results.append({"id": rid, "ok": True, "eligible": True,
                            "confidence_original": conf_orig, "threshold": thr,
                            "suggested_dst": dst, "note": "可自动（未执行）"})
            continue
        applied_n += 1
        try:
            res = decide_one(conn, rid, "approved",
                             DecideBody(decision="approved", suggested_dst=dst),
                             decided_by="system:reclassify-0.2.5")
        except HTTPException as e:
            results.append({"id": rid, "ok": False, "eligible": True,
                            "note": "批准失败：%s" % e.detail})
            continue
        if res.get("moved"):
            moved_n += 1
        results.append({"id": rid, "ok": True, "eligible": True,
                        "moved": bool(res.get("moved")), "dst": res.get("dst") or dst,
                        "note": res.get("note") or ""})
    return {"total": len(rows), "eligible": eligible_n, "applied": applied_n,
            "moved": moved_n, "not_eligible": not_eligible_n, "skipped": skipped_n,
            "results": results}


@router.get("/dup/dry-run")
def dup_dry_run(limit: int = 100, conn=Depends(get_db)):
    """0.2.5 §3.6：dup 整批批准 dry-run——只报数，不移动、不删除。
    dup 批准语义仍是移入 holding 区（不删除）。"""
    rows = DB.list_review(conn, status="pending", kind="dup")
    total = len(rows)
    exists = 0
    sample = []
    for r in rows:
        p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        src = p.get("src") or ""
        if src and os.path.exists(src):
            exists += 1
            if len(sample) < limit:
                sample.append({"id": r["id"], "src": src,
                               "holding_dst": RT.dup_holding_dst(src, _roots())})
    return {"total": total, "src_exists": exists, "src_gone": total - exists,
            "sample": sample,
            "note": "批准后移入 99-待确认/06-重复待确认 holding 区，不删除文件"}
