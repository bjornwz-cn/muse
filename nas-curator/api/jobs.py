"""api.jobs —— 整理任务：发起 plan / 查任务 / apply / rollback。

- POST /api/jobs：guard 校验后建 job，后台线程调 curator.cmd_plan，立即返回
  {job_id, plan_id: null, status: 'running'}，前端轮询 GET /api/jobs/{id}。
  cmd_plan 不返回 plan_id，后台线程以「执行前最大 plan id」为基线，
  完成后取新出现的最大 plan id 写入 job summary。
- apply：默认走 curator.cmd_apply（plan_id 路径，执行全部 move/rename 项）。
  例外——统一收件箱模式（plan.library == 'inbox'）且设置 auto_apply 开启时，
  只执行 action=move/rename 且 (confidence >= auto_threshold 或 auto_exempt)
  的项：不满足的 plan_item 先置 skipped，再直接调 CE.apply_plan(过滤后项)
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
from common import guard
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
    return argparse.Namespace(
        db=db_path, root=root, library=body.library or "", kind="document",
        out="", rules="", media_config="",
        ocr_url=me.get("ocr_url") or "http://127.0.0.1:8866", no_ocr=False, workers=8,
        max_chars=2000, ocr_timeout=90, min_score=4, content_first=False,
        cache="", unit_mode="auto", protect="",
        rename=bool(body.rename), rename_llm=False,
        llm=bool(body.use_llm), llm_host=me.get("llm_host") or "http://172.17.0.1:11434",
        llm_model="qwen3:4b", llm_min_conf=0.55, llm_max=2000,
        sem=bool(body.use_sem), sem_model="bge-m3:latest",
        sem_min_sim=0.42, sem_margin=0.02,
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
    """收件箱 auto_apply 子集执行：只执行高置信 move/rename 项。"""
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
            conf = it.get("confidence")
            conf = float(conf) if conf is not None else 0.0
            if conf >= threshold or it.get("auto_exempt"):
                todo.append(it)
            else:
                # P4：与 CLI _apply_items 口径对齐——被拦下的低置信项只置 skipped
                # 还不够，必须写 review_queue（kind=low_conf），否则人工在 Web
                # 复核队列里看不到、文件会静默留在原地无人处理。
                conn.execute("UPDATE plan_items SET status='skipped' WHERE id=?",
                             (it["id"],))
                DB.add_review(conn, file_id=it.get("file_id"), plan_id=plan_id,
                              kind="low_conf",
                              payload={"src": it.get("src"),
                                       "category": it.get("category"),
                                       "confidence": it.get("confidence"),
                                       "reason": it.get("reason"),
                                       "evidence": it.get("evidence"),
                                       "suggested_dst": it.get("dst"),
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
                                map="", report="", uid=1000, gid=1000, dry_run=False)
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
        ns = argparse.Namespace(db=db_path, plan_id=plan_id, map="", dry_run=False)
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
    return [_job_dict(r) for r in conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT 20")]


@router.get("/{job_id}")
def get_job(job_id: int, limit: int = 200, offset: int = 0, conn=Depends(get_db)):
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"任务 #{job_id} 不存在")
    job = _job_dict(row)
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
