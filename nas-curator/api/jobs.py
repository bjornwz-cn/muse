"""api.jobs —— 整理任务：发起 plan / 查任务 / apply / rollback。

- POST /api/jobs：guard 校验后建 job，后台线程调 curator.cmd_plan，立即返回
  {job_id, plan_id: null, status: 'running'}，前端轮询 GET /api/jobs/{id}。
  cmd_plan 不返回 plan_id，后台线程以「执行前最大 plan id」为基线，
  完成后取新出现的最大 plan id 写入 job summary。
- apply：默认走 curator.cmd_apply（plan_id 路径，执行全部 move/rename 项）。
  例外——统一收件箱模式（plan.library == 'inbox'）且设置 auto_apply 开启时，
  只执行 is_auto_apply_eligible 判定的项（confidence >= auto_threshold
  且 kind 不是整目录 unit；auto_exempt 例外已废除，2026-11-24 计划 #160
  修复）：不满足的 plan_item 先置 skipped，再直接调 CE.apply_plan(过滤后项)
  + 写 operations + 置 plan done，不走 cmd_apply（它按 action 取项、不看 status）。
- rollback：后台线程调 curator.cmd_rollback。
- 默认 use_llm/use_sem=false（规则层）：本机连不上 NAS 的模型端点；
  模型端点只读配置，不主动连接。
"""
from __future__ import annotations

import argparse
import json
import os
import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import curator as CUR
from common import db as DB
from common import guard, progress as PROG
from common import routing as RT
from .deps import get_db, get_db_path, get_settings, load_libraries

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


class CreateJobBody(BaseModel):
    root: str | None = None
    library: str | None = None
    dry_run: bool = True
    use_llm: bool = False
    use_sem: bool = False
    rename: bool = False


def _resolve_root(body: CreateJobBody) -> str:
    """root 优先；否则按 library 查 libraries.json / input_sources；都没有则用统一收件箱。"""
    cfg = load_libraries()
    if body.root:
        return os.path.abspath(os.path.expanduser(body.root))
    if body.library:
        for lib in cfg.get("libraries", []):
            if body.library in (lib.get("id"), lib.get("name")):
                return lib["root"]
        for src in cfg.get("input_sources", []):
            if body.library in (src.get("id"), src.get("name")):
                return src["root"]
        raise HTTPException(status_code=400, detail=f"未知库：{body.library}")
    inbox = (cfg.get("inbox") or {}).get("path", "")
    if not inbox:
        raise HTTPException(status_code=400, detail="未指定 root/library，且未配置统一收件箱")
    return inbox


def _plan_namespace(body: CreateJobBody, root: str, db_path: str) -> argparse.Namespace:
    """补齐 cmd_plan 需要的全部字段（与 curator.build_parser 的 plan 默认值一致）。

    模型端点取自设置（环境变量 LLM_HOST/SEM_HOST/OCR_URL 优先，见 deps.get_settings）：
    容器内 127.0.0.1 指容器自身，必须用宿主网关地址（如 172.17.0.1）才能连到
    NAS 上的 ollama-cpu :11434 / ollama-gpu :11500 / ocr-service :8866。
    """
    me = get_settings().get("model_endpoints") or {}
    # 0.1.5：向量模型名改走设置页配置（common.embedding），不再写死 bge-m3
    from common.embedding import load_embedding_config
    sem_model = load_embedding_config().get("model") or "bge-m3:latest"
    # 0.1.7：API 后台线程已自建 job 行，cmd_plan 不得再建一行（_record_job=False），
    # 否则一行任务在任务页出现两次；job 的收尾仍由 _run_plan_job 负责。
    return argparse.Namespace(
        db=db_path, root=root, library=body.library or "", kind="document",
        out="", rules="", media_config="",
        ocr_url=me.get("ocr_url") or None, no_ocr=False, workers=8,
        max_chars=2000, ocr_timeout=90, min_score=4, content_first=False,
        cache="", unit_mode="auto", protect="",
        rename=bool(body.rename), rename_llm=False,
        llm=bool(body.use_llm), llm_host=me.get("llm_host") or "http://172.17.0.1:11434",
        llm_model="qwen3:4b", llm_min_conf=0.55, llm_max=2000,
        sem=bool(body.use_sem), sem_model=sem_model,
        sem_min_sim=0.42, sem_margin=0.02,
        _record_job=False,
    )


def _max_plan_id(conn) -> int:
    row = conn.execute("SELECT MAX(id) AS m FROM plans").fetchone()
    return row["m"] or 0


def _run_plan_job(job_id: int, db_path: str, ns: argparse.Namespace):
    conn = DB.connect(db_path)
    try:
        baseline = _max_plan_id(conn)
        try:
            CUR.cmd_plan(ns)
        except SystemExit as e:
            DB.finish_job(conn, job_id, "failed", {"error": str(e) or "plan 被拒绝"})
            return
        plan_id = _max_plan_id(conn)
        if plan_id <= baseline:
            DB.finish_job(conn, job_id, "failed", {"error": "plan 未生成（无新计划）"})
            return
        plan = DB.get_plan(conn, plan_id) or {}
        summary = plan.get("summary") or {}
        DB.finish_job(conn, job_id, "done", {
            "plan_id": plan_id,
            "move": summary.get("move", 0) + summary.get("rename", 0),
            "review": summary.get("review", 0),
        })
    except Exception as e:  # noqa: BLE001 - 后台失败记 job，不抛到请求线程
        try:
            DB.finish_job(conn, job_id, "failed", {"error": str(e)})
        except Exception:
            pass
    finally:
        conn.close()


def _common_root(paths: list[str]) -> str:
    paths = [p for p in paths if p]
    if not paths:
        return "."
    try:
        base = os.path.commonpath(paths)
    except ValueError:
        return "."
    return base if os.path.isdir(base) else os.path.dirname(base)


def _run_auto_apply(job_id: int, db_path: str, plan_id: int, threshold: float):
    """收件箱 auto_apply 子集执行：只执行高置信（>= 阈值）且非整目录的
    move/rename 项；整目录 unit 项与低置信项一律进复核（2026-11-24 计划
    #160 修复，判定见 common.routing.is_auto_apply_eligible）。"""
    conn = DB.connect(db_path)
    try:
        DB.set_plan_status(conn, plan_id, "applying")
        rows = DB.list_plan_items(conn, plan_id)
        items = [{**r, "src": r["src_path"], "dst": r["dst_path"],
                  "tags": json.loads(r["tags"]) if r.get("tags") else []}
                 for r in rows]
        todo, skipped = [], 0
        for it in items:
            if it.get("action") not in ("move", "rename"):
                continue
            if RT.is_auto_apply_eligible(it, threshold):
                todo.append(it)
            else:
                # P4：与 CLI _apply_items 口径对齐——被拦下的低置信项只置 skipped
                # 还不够，必须写 review_queue（kind=low_conf），否则人工在 Web
                # 复核队列里看不到、文件会静默留在原地无人处理。
                conn.execute("UPDATE plan_items SET status='skipped' WHERE id=?",
                             (it["id"],))
                # 0.1.9 去重：同一 src 已有复核行（待处理或已裁决）时不再新建；
                # 2026-11-27 提速改动 2：新建行带 size/mtime 指纹，已有行刷新指纹。
                try:
                    _mtime = os.path.getmtime(it.get("src") or "")
                except OSError:
                    _mtime = None
                if DB.has_review_for_src(conn, it.get("src")):
                    DB.refresh_review_fingerprint(conn, it.get("src"),
                                                  it.get("size"), _mtime)
                    skipped += 1
                    continue
                DB.add_review(conn, file_id=it.get("file_id"), plan_id=plan_id,
                              kind="low_conf",
                              payload={"src": it.get("src"),
                                       "category": it.get("category"),
                                       "confidence": it.get("confidence"),
                                       "reason": it.get("reason"),
                                       "evidence": it.get("evidence"),
                                       "suggested_dst": it.get("dst"),
                                       "size": it.get("size"), "mtime": _mtime,
                                       "tags": it.get("tags") or []})
                skipped += 1
        conn.commit()
        root = _common_root([i["src"] for i in todo]) or "."
        from curator_core import engine as CE
        operations, report = CE.apply_plan(todo, root, dry_run=False, uid=1000, gid=1000)
        done_ids = set()
        for op in operations:
            DB.add_operation(conn, plan_id, op["src_path"], op["dst_path"],
                             op.get("sha256"))
        for it in todo:
            for op in operations:
                if op["src_path"] == it["src"]:
                    done_ids.add(it["id"])
        for pid in done_ids:
            conn.execute("UPDATE plan_items SET status='done' WHERE id=?", (pid,))
        conn.commit()
        DB.set_plan_status(conn, plan_id, "done", report)
        DB.finish_job(conn, job_id, "done", {
            "plan_id": plan_id, "moved": report.get("moved", 0),
            "skipped": skipped, "failed": len(report.get("failed", [])),
            "auto_applied": True, "threshold": threshold,
        })
    except Exception as e:  # noqa: BLE001
        try:
            DB.finish_job(conn, job_id, "failed", {"error": str(e)})
        except Exception:
            pass
    finally:
        conn.close()


def _run_cmd_apply(job_id: int, db_path: str, plan_id: int):
    conn = DB.connect(db_path)
    try:
        DB.set_plan_status(conn, plan_id, "applying")
        ns = argparse.Namespace(db=db_path, plan_id=plan_id, plan="", root="",
                                map="", report="", uid=1000, gid=1000, dry_run=False,
                                _record_job=False)  # 0.1.7：本行已由 API 建 job，cmd_apply 不再建
        try:
            CUR.cmd_apply(ns)
        except SystemExit as e:
            DB.finish_job(conn, job_id, "failed", {"error": str(e) or "apply 被拒绝"})
            return
        DB.finish_job(conn, job_id, "done", {"plan_id": plan_id})
    except Exception as e:  # noqa: BLE001
        try:
            DB.finish_job(conn, job_id, "failed", {"error": str(e)})
        except Exception:
            pass
    finally:
        conn.close()


def _run_rollback(job_id: int, db_path: str, plan_id: int):
    conn = DB.connect(db_path)
    try:
        ns = argparse.Namespace(db=db_path, plan_id=plan_id, map="", dry_run=False,
                                _record_job=False)  # 0.1.7：本行已由 API 建 job，cmd_rollback 不再建
        try:
            CUR.cmd_rollback(ns)
        except SystemExit as e:
            DB.finish_job(conn, job_id, "failed", {"error": str(e) or "rollback 被拒绝"})
            return
        DB.finish_job(conn, job_id, "done", {"plan_id": plan_id})
    except Exception as e:  # noqa: BLE001
        try:
            DB.finish_job(conn, job_id, "failed", {"error": str(e)})
        except Exception:
            pass
    finally:
        conn.close()


def _job_dict(row) -> dict:
    d = dict(row)
    if d.get("summary"):
        try:
            d["summary"] = json.loads(d["summary"])
        except ValueError:
            pass
    return d


# ---------------------------------------------------------------- 进度（0.2.0）

def _plan_counts(conn, plan_id: int) -> dict:
    """plan_items 按状态计数；未知状态一律算 pending。"""
    counts = {"done": 0, "pending": 0, "skipped": 0, "failed": 0}
    for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM plan_items"
            " WHERE plan_id=? GROUP BY status", (plan_id,)):
        st = (r["status"] or "pending").lower()
        counts[st if st in counts else "pending"] += int(r["n"])
    return counts


def _apply_counts(conn, plan_id: int) -> dict:
    """apply 任务的工作集计数：只计非 review 项。

    review 项（action='review'）从来不是 apply 要移动的——
    把它们算进总数，执行进度永远到不了 100%（如 #107：31,493 个
    review 项会一直 pending）。move/rename 才是 apply 的工作集；
    工作集为空时回退到全部项（纯复核计划被 apply 的极端情况）。
    """
    counts = {"done": 0, "pending": 0, "skipped": 0, "failed": 0}
    for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM plan_items"
            " WHERE plan_id=? AND (action IS NULL OR action != 'review')"
            " GROUP BY status", (plan_id,)):
        st = (r["status"] or "pending").lower()
        counts[st if st in counts else "pending"] += int(r["n"])
    if sum(counts.values()) == 0:
        return _plan_counts(conn, plan_id)
    return counts


def _phase_for(job_kind, plan_status, job_status) -> str:
    """当前阶段（中文）：按 plan 状态优先，其次按任务类型。"""
    if job_status == "done":
        return "已完成"
    if job_status == "failed":
        return "失败"
    if plan_status == "applying":
        return "执行中"
    if plan_status == "done":
        return "已完成"
    return {"plan": "计划中", "apply": "执行中",
            "rollback": "回滚中"}.get(job_kind or "", "进行中")


def _job_progress(conn, job: dict) -> dict:
    """一个 DB 任务的进度摘要（数字全部来自 plan_items 真实计数）。

    - 有 plan：按 plan_items 状态计数；plan 扫描中途（plan_items 尚未
      写入）时 total 为 0，pct 按 0 计、phase 显示计划中——总数未知时
      不编造。
    - 无 plan 且已结束：按 job summary 的计数算 100%（done）或失败。
    - 无 plan 且进行中：总数未知（total/pct/ETA 均为 None，前端显示 —）。
    """
    summary = job.get("summary") or {}
    plan_id = summary.get("plan_id") if isinstance(summary, dict) else None
    started = job.get("started_at")
    finished = job.get("finished_at")
    status = job.get("status")
    if plan_id:
        plan = DB.get_plan(conn, plan_id)
        counts = _plan_counts(conn, plan_id)
        total = sum(counts.values())
        kind = job.get("kind")
        if kind in ("plan", "rollback"):
            # 计划/回滚任务：进度按任务本身算，不按 plan_items 状态。
            # plan 任务的工作是生成计划本身：计划已生成即 100%，
            # plan_items 的 pending 是“待执行”的下一阶段（apply 的事），
            # 不能算成本任务未完成；否则已完成的计划任务会显示 0%。
            # 回滚同理：回滚执行中的 plan_items 状态反映的是 apply 的旧结局，
            # 不是回滚进度。
            if status == "done":
                p = PROG.compute_progress(total, total, 0, 0, started, finished)
            elif status == "failed":
                p = PROG.compute_progress(total, 0, 0, 0, started, finished)
            else:  # running：计划尚未写入 / 回滚尚未结束，总数未知
                p = PROG.compute_progress(None, 0, 0, 0, started, None)
        else:
            # apply：按 plan_items 状态计执行进度（工作集为非 review 项）。
            # 但 CLI apply 从不写 plan_items 状态（#107/#142 的项至今全是
            # pending），已完成任务不能因此显示 0%：status 为 done 时，
            # 以 job summary（apply 自己的执行报告：moved/failed）为准
            # 兜底——done 取两边最大值，剩余未移动项是已在目标位置
            # （如 #142 的 6,408 个）或无需移动的，计 skipped；pending 归零。
            # vlm_apply 这类写了项状态的任务不受影响（max 取项状态值）。
            # running/failed 任务不用兜底：如实显示当前项状态。
            counts = _apply_counts(conn, plan_id)
            total = sum(counts.values())
            if status == "done":
                moved = int((summary.get("moved") or 0)) if isinstance(
                    summary, dict) else 0
                failed_n = int((summary.get("failed") or 0)) if isinstance(
                    summary, dict) else 0
                done_n = max(counts["done"], min(moved, total))
                failed_n = max(counts["failed"], min(failed_n, total - done_n))
                skipped_n = max(0, total - done_n - failed_n)
                p = PROG.compute_progress(total, done_n, skipped_n, failed_n,
                                          started, finished)
            else:
                p = PROG.compute_progress(total, counts["done"],
                                          counts["skipped"], counts["failed"],
                                          started, finished)
        p["phase"] = _phase_for(kind, (plan or {}).get("status"), status)
        p["plan_id"] = plan_id
        return p
    if status == "done":
        total = 0
        if isinstance(summary, dict):
            total = int(summary.get("move", 0) or 0) \
                + int(summary.get("review", 0) or 0) \
                + int(summary.get("moved", 0) or 0)
        p = PROG.compute_progress(total, total, 0, 0, started, finished)
        p["phase"] = "已完成"
        p["plan_id"] = None
        return p
    if status == "failed":
        p = PROG.compute_progress(0, 0, 0, 0, started, finished)
        p["phase"] = "失败"
        p["plan_id"] = None
        return p
    p = PROG.compute_progress(None, 0, 0, 0, started, None)
    p["phase"] = _phase_for(job.get("kind"), None, status)
    p["plan_id"] = None
    return p


def _script_tasks() -> list:
    """脚本任务（心跳 + 年份重分类兜底推导），按 updated_at 倒序。

    心跳文件优先；derived 的年份任务在同 task_id 已有心跳时不再重复列出
    （同一任务只显示一个来源，心跳为准）。
    """
    data_dir = os.path.dirname(get_db_path())
    out = PROG.read_heartbeats()
    seen = {t.get("task_id") for t in out}
    derived = PROG.derive_year_progress(data_dir)
    if derived is not None and derived.get("task_id") not in seen:
        out.append(derived)
    out.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return out


@router.get("/running")
def running_tasks(conn=Depends(get_db)):
    """进行中任务（总览页用）：DB 侧 running 的 job + 脚本任务。

    脚本任务只含 running/stale，以及 1 小时内刚结束的 done（让用户看到
    刚跑完的任务）；更早结束的不再列出。
    """
    import time as _time
    jobs = []
    for r in conn.execute(
            "SELECT * FROM jobs WHERE status='running' ORDER BY id DESC"):
        job = _job_dict(r)
        job["progress"] = _job_progress(conn, job)
        jobs.append(job)
    scripts = []
    now_ts = _time.time()
    for t in _script_tasks():
        if t.get("status") == "running" or t.get("stale"):
            scripts.append(t)
        elif t.get("status") == "done":
            upd = PROG.parse_ts(t.get("updated_at"))
            if upd is not None and (now_ts - upd.timestamp()) <= 3600:
                scripts.append(t)
    return {"jobs": jobs, "scripts": scripts}



@router.post("")
def create_job(body: CreateJobBody, conn=Depends(get_db)):
    root = _resolve_root(body)
    try:
        guard.assert_allowed(root, "整理根目录")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not os.path.isdir(root):
        raise HTTPException(status_code=400, detail=f"根目录不存在：{root}")
    db_path = get_db_path()
    job_id = DB.create_job(conn, "plan")
    ns = _plan_namespace(body, root, db_path)
    threading.Thread(target=_run_plan_job, args=(job_id, db_path, ns),
                     daemon=True).start()
    # dry_run 仅作兼容字段：plan 本身只做计划、从不移动文件。
    return {"job_id": job_id, "plan_id": None, "status": "running"}


@router.get("")
def list_jobs(conn=Depends(get_db)):
    out = []
    for r in conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 20"):
        job = _job_dict(r)
        job["progress"] = _job_progress(conn, job)
        out.append(job)
    return out


@router.get("/{job_id}")
def get_job(job_id: int, limit: int = 200, offset: int = 0, conn=Depends(get_db)):
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"任务 #{job_id} 不存在")
    job = _job_dict(row)
    job["progress"] = _job_progress(conn, job)
    out = {"job": job}
    summary = job.get("summary") or {}
    plan_id = summary.get("plan_id") if isinstance(summary, dict) else None
    if plan_id:
        plan = DB.get_plan(conn, plan_id)
        items = DB.list_plan_items(conn, plan_id)
        out["plan"] = plan
        # 契约键 items（前端与测试用）；plan_items 保留做兼容
        out["items"] = items[offset:offset + limit]
        out["plan_items"] = out["items"]
        out["plan_items_total"] = len(items)
    return out


@router.get("/{job_id}/progress")
def get_job_progress(job_id: int, conn=Depends(get_db)):
    """任务进度详情：计数 + 百分比 + 速率 + ETA + 问题清单。"""
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"任务 #{job_id} 不存在")
    job = _job_dict(row)
    progress = _job_progress(conn, job)
    failed_items = []
    plan_id = progress.get("plan_id")
    if plan_id:
        failed_items = [
            {"id": r["id"], "src_path": r["src_path"], "dst_path": r["dst_path"],
             "category": r["category"], "reason": r["reason"]}
            for r in conn.execute(
                "SELECT id, src_path, dst_path, category, reason FROM plan_items"
                " WHERE plan_id=? AND status='failed' ORDER BY id LIMIT 50",
                (plan_id,))]
    return {"job_id": job_id, "job": job, "progress": progress,
            "failed_items": failed_items,
            "failed_total": progress.get("failed", 0)}


@router.post("/{plan_id}/apply")
def apply_plan(plan_id: int, conn=Depends(get_db)):
    plan = DB.get_plan(conn, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail=f"计划 #{plan_id} 不存在")
    if plan.get("status") == "done":
        raise HTTPException(status_code=400, detail=f"计划 #{plan_id} 已执行过")
    db_path = get_db_path()
    job_id = DB.create_job(conn, "apply")
    settings = get_settings()
    if plan.get("library") == "inbox" and settings.get("auto_apply"):
        threshold = float((settings.get("thresholds") or {}).get("auto", 0.85))
        threading.Thread(target=_run_auto_apply,
                         args=(job_id, db_path, plan_id, threshold),
                         daemon=True).start()
        mode = "auto_apply_subset"
    else:
        threading.Thread(target=_run_cmd_apply, args=(job_id, db_path, plan_id),
                         daemon=True).start()
        mode = "full_apply"
    return {"job_id": job_id, "plan_id": plan_id, "status": "running", "mode": mode}


@router.post("/{plan_id}/rollback")
def rollback_plan(plan_id: int, conn=Depends(get_db)):
    plan = DB.get_plan(conn, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail=f"计划 #{plan_id} 不存在")
    db_path = get_db_path()
    job_id = DB.create_job(conn, "rollback")
    threading.Thread(target=_run_rollback, args=(job_id, db_path, plan_id),
                     daemon=True).start()
    return {"job_id": job_id, "plan_id": plan_id, "status": "running"}
