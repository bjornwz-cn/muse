#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.progress —— 任务进度：心跳契约 + 进度计算（0.2.0）。

背景：任务页以前只有任务历史（状态/起止时间），看不到每个任务进行到
哪一步、还剩多久。0.2.0 补上两类进度来源：

1. DB 任务（Web API 发起的 plan/apply/rollback，jobs 表有行）：
   按 plan_items 的状态实时计数（done/pending/skipped/failed），算出
   完成百分比、处理速率（项/分钟）、预估剩余时间 ETA。
2. 脚本任务（CLI 脚本直接跑的，如 vlm_reclassify.py、year_siglip.py，
   只建 plans、不建 jobs 行）：心跳文件契约——脚本在
   <DB 同目录>/progress/<任务标识>.json 写进度，API 聚合后统一显示。

心跳文件格式（JSON，全部字段可选，缺失按 None/0 处理）：
  {
    "task_id": "year-reclassify", "label": "年份文件夹重分类",
    "kind": "year-reclassify", "phase": "siglip",
    "status": "running",            # running / done / failed
    "total": 14315, "done": 8600, "skipped": 0, "failed": 3,
    "errors": ["rq=1234: api_error ..."],   # 最近错误，最多保留 10 条
    "started_at": "2026-11-23T14:17:24",    # 与 DB 一致的本地时间字符串
    "updated_at": "2026-11-23T15:21:09",
    "extra": {"unclassified": 120}          # 任务自定的补充数字
  }
心跳文件过期（updated_at 超过 STALE_AFTER_S 秒且 status 仍为 running）
即标 stale（已失联/可能中断），绝不假装还在跑。

另有 derive_year_progress()：为正在跑的旧版 year_siglip.py 兜底——
它不写心跳时，从 /data/year_siglip.log 的 PROG 行与
year_siglip_results.jsonl 的行数推导进度（source 标 "derived"，
与心跳来源区分开）。

本模块只依赖标准库；时间一律用与 DB 相同的本地时间字符串
（strftime('%Y-%m-%dT%H:%M:%S', 'now') 口径，即 datetime.now()）。
ETA 算不出来（无速率、剩余为 0、总数未知）时一律返回 None，
前端显示 "—"，绝不编造数字。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

# 心跳过期阈值（秒）：running 状态的心跳超过 10 分钟未更新即视为失联。
STALE_AFTER_S = 600

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DB = os.path.join(_PROJECT_ROOT, "data", "nas-curator.db")

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PROG_LINE_RE = re.compile(r"PROG\s+(\d+)\s*/\s*(\d+)\s+(\d+)s")
_TODO_RE = re.compile(r"to classify \(SigLIP\):\s*(\d+)")


# ---------------------------------------------------------------- 时间

def now_iso() -> str:
    """当前本地时间字符串（与 DB 的 strftime('now') 同一口径）。"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def parse_ts(s):
    """解析本地时间字符串；非法/缺失返回 None。"""
    if not s or not isinstance(s, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _seconds_between(a: datetime | None, b: datetime | None):
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


# ---------------------------------------------------------------- 进度计算（纯函数，可单元测试）

def compute_progress(total, done, skipped=0, failed=0,
                     started_at=None, finished_at=None, now=None):
    """由计数算进度摘要。total 为 None 表示总数未知。

    规则：
    - pending = total - done - skipped - failed（总数已知时；下限 0）。
    - pct：总数已知时 done/total；总数 0 且已结束时 100，否则 None。
    - 速率：已耗时 >= 60 秒且 done > 0 时才算（项/分钟）；否则 None。
    - ETA：剩余 pending / 速率；无速率或 pending 为 0 时 None。
    """
    total = None if total is None else int(total)
    done = int(done or 0)
    skipped = int(skipped or 0)
    failed = int(failed or 0)
    start = parse_ts(started_at)
    finish = parse_ts(finished_at)
    now_dt = parse_ts(now) if isinstance(now, str) else (now or datetime.now())
    end = finish or now_dt
    elapsed_s = _seconds_between(start, end)
    if total is None:
        pending = None
        pct = None
    else:
        pending = max(0, total - done - skipped - failed)
        pct = round(100.0 * done / total, 1) if total > 0 else (
            100.0 if finish is not None else 0.0)
    rate_per_min = None
    if elapsed_s is not None and elapsed_s >= 60 and done > 0:
        rate_per_min = round(done / (elapsed_s / 60.0), 1)
    eta_seconds = None
    if rate_per_min and pending:
        eta_seconds = int(round(pending / (rate_per_min / 60.0)))
    return {
        "total": total, "done": done, "pending": pending,
        "skipped": skipped, "failed": failed,
        "pct": pct, "elapsed_s": int(elapsed_s) if elapsed_s is not None else None,
        "rate_per_min": rate_per_min, "eta_seconds": eta_seconds,
        "finished": finish is not None,
    }


# ---------------------------------------------------------------- 心跳文件

def default_progress_dir() -> str:
    """心跳目录：环境变量 CURATOR_PROGRESS_DIR，缺省为 DB 同目录的 progress/。

    容器内 DB 为 /data/nas-curator.db → /data/progress；本地开发为
    项目 data/progress。
    """
    env = os.environ.get("CURATOR_PROGRESS_DIR")
    if env:
        return env
    db_path = os.environ.get("CURATOR_DB") or _DEFAULT_DB
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "progress")


def heartbeat_path(task_id: str, progress_dir: str | None = None) -> str:
    if not _TASK_ID_RE.match(task_id or ""):
        raise ValueError(f"非法任务标识：{task_id!r}（只允许字母/数字/_/-）")
    return os.path.join(progress_dir or default_progress_dir(),
                        task_id + ".json")


def write_heartbeat(task_id, *, label=None, kind=None, phase=None,
                    status="running", total=None, done=0, skipped=0,
                    failed=0, errors=None, started_at=None, extra=None,
                    progress_dir=None, now=None):
    """写一条心跳（原子写：临时文件 + os.replace）。返回写入的记录 dict。

    started_at 缺省时：同一任务已有的心跳文件里的 started_at 会被保留
    （同一轮运行不重置计时）；全新任务则用 now。
    """
    if status not in ("running", "done", "failed"):
        raise ValueError(f"非法心跳状态：{status!r}")
    path = heartbeat_path(task_id, progress_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now_s = now_iso() if now is None else now
    prev = None
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        prev = None
    if started_at is None and isinstance(prev, dict):
        started_at = prev.get("started_at")
    if started_at is None:
        started_at = now_s
    prog = compute_progress(total, done, skipped, failed,
                            started_at, None, now_s)
    rec = {
        "task_id": task_id,
        "label": label or (prev or {}).get("label") or task_id,
        "kind": kind or (prev or {}).get("kind") or task_id,
        "phase": phase if phase is not None else (prev or {}).get("phase"),
        "status": status,
        "source": "heartbeat",
        "started_at": started_at,
        "updated_at": now_s,
        "errors": list(errors or [])[-10:],
        "extra": extra or {},
    }
    rec.update(prog)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return rec


def read_heartbeats(progress_dir=None, now=None):
    """读全部心跳文件，补 stale 标记。返回按 updated_at 倒序的记录列表。

    stale：status 为 running 且 updated_at 超过 STALE_AFTER_S 秒未更新。
    损坏的 JSON 文件跳过（不让一个坏文件拖垮整个列表）。
    """
    d = progress_dir or default_progress_dir()
    now_dt = parse_ts(now) if isinstance(now, str) else (now or datetime.now())
    out = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        if not isinstance(rec, dict) or "task_id" not in rec:
            continue
        rec = dict(rec)
        rec.setdefault("source", "heartbeat")
        rec.setdefault("status", "running")
        upd = parse_ts(rec.get("updated_at"))
        stale = False
        if rec["status"] == "running" and upd is not None:
            stale = (now_dt - upd).total_seconds() > STALE_AFTER_S
        rec["stale"] = stale
        out.append(rec)
    out.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return out


# ---------------------------------------------------------------- 兜底推导：年份重分类（旧版脚本不写心跳）

def derive_year_progress(data_dir=None, now=None):
    """从 year_siglip 的日志与结果文件推导进度（source='derived'）。

    依据（全部是脚本自己产出的真实产物）：
    - /data/year_siglip_results.jsonl 行数 = 已分类数（逐行刷盘）。
    - /data/year_siglip.log 最后一行 PROG d/t secs = 脚本自报的
      已做/总数/已耗时（每 200 项打印一次）；总数取不到时用
      "to classify (SigLIP): N" 那行。
    - 汇总文件 year_reclassify_summary.json 存在即视为本阶段跑完。
    产物都不存在时返回 None（本任务尚未开始）。
    """
    d = data_dir
    if d is None:
        db_path = os.environ.get("CURATOR_DB") or _DEFAULT_DB
        d = os.path.dirname(os.path.abspath(db_path))
    now_dt = parse_ts(now) if isinstance(now, str) else (now or datetime.now())
    summary_path = os.path.join(d, "year_reclassify_summary.json")
    results_path = os.path.join(d, "year_siglip_results.jsonl")
    log_path = os.path.join(d, "year_siglip.log")
    if os.path.exists(summary_path):
        try:
            with open(summary_path, encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            s = {}
        total = (s.get("disk_media_photo") or 0) + (s.get("disk_media_video") or 0)
        done = s.get("review_rows_on_disk") or 0
        rec = {
            "task_id": "year-reclassify", "label": "年份文件夹重分类",
            "kind": "year-reclassify", "phase": "summary", "status": "done",
            "source": "derived",
            "started_at": None,
            "updated_at": datetime.fromtimestamp(
                os.path.getmtime(summary_path)).strftime("%Y-%m-%dT%H:%M:%S"),
            "errors": [], "extra": {"unclassified": s.get("unclassified") or 0},
            "stale": False,
        }
        rec.update(compute_progress(total or None, done, 0, 0, None, rec["updated_at"]))
        return rec
    if not os.path.exists(results_path) and not os.path.exists(log_path):
        return None
    done = 0
    if os.path.exists(results_path):
        with open(results_path, "rb") as f:
            done = sum(1 for _ in f)
    total = None
    prog_done = prog_elapsed = None
    if os.path.exists(log_path):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                log_text = f.read()
        except Exception:
            log_text = ""
        for m in _PROG_LINE_RE.finditer(log_text):
            prog_done, total, prog_elapsed = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if total is None:
            m = _TODO_RE.search(log_text)
            if m:
                total = int(m.group(1))
    updated = None
    if os.path.exists(results_path):
        updated = datetime.fromtimestamp(
            os.path.getmtime(results_path)).strftime("%Y-%m-%dT%H:%M:%S")
    elif os.path.exists(log_path):
        updated = datetime.fromtimestamp(
            os.path.getmtime(log_path)).strftime("%Y-%m-%dT%H:%M:%S")
    rec = {
        "task_id": "year-reclassify", "label": "年份文件夹重分类",
        "kind": "year-reclassify", "phase": "siglip", "status": "running",
        "source": "derived",
        "started_at": None, "updated_at": updated,
        "errors": [], "extra": {},
        "stale": False,
    }
    # 速率与 ETA：用脚本自报的 PROG 已耗时（比 started_at 更准，
    # 因 derived 来源没有可信的开始时间）。
    prog = compute_progress(total, done, 0, 0, None, None)
    if prog_elapsed and prog_done:
        rate = round(prog_done / (prog_elapsed / 60.0), 1) if prog_elapsed >= 60 else None
        prog["rate_per_min"] = rate
        prog["elapsed_s"] = prog_elapsed
        if rate and prog.get("pending"):
            prog["eta_seconds"] = int(round(prog["pending"] / (rate / 60.0)))
    if updated:
        upd = parse_ts(updated)
        if upd is not None:
            rec["stale"] = (now_dt - upd).total_seconds() > STALE_AFTER_S
    rec.update(prog)
    return rec
