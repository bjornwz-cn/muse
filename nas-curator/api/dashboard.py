"""api.dashboard —— GET /api/dashboard 总览。"""
from __future__ import annotations

import os
from datetime import date

from fastapi import APIRouter, Depends

from .deps import get_db, get_settings
from .jobs import _job_dict, _job_progress  # 0.2.0：最近任务带进度摘要

from common.version import read_local_version

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _scandir_top(root: str) -> tuple[int, int]:
    """DB 为空时的轻量统计：只扫顶层（不递归），返回 (文件数, 字节数)。"""
    n_files = n_bytes = 0
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        n_files += 1
                        n_bytes += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return n_files, n_bytes


@router.get("")
def dashboard(conn=Depends(get_db)):
    settings = get_settings()
    total_files = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]

    libraries = []
    for lib in settings["libraries"]:
        root = lib.get("root", "")
        if total_files > 0:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS b FROM files"
                " WHERE path=? OR path LIKE ?",
                (root, root.rstrip("/") + "/%")).fetchone()
            n_files, n_bytes = row["n"], row["b"]
        else:
            n_files, n_bytes = _scandir_top(root)
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM review_queue WHERE status='pending'").fetchone()["n"]
        libraries.append({
            "id": lib.get("id"), "name": lib.get("name"), "root": root,
            "files": n_files, "bytes": n_bytes, "review_pending": pending,
        })

    review_pending = conn.execute(
        "SELECT COUNT(*) AS n FROM review_queue WHERE status='pending'").fetchone()["n"]
    today = date.today().isoformat()
    today_new = conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE substr(created_at,1,10)=?",
        (today,)).fetchone()["n"]
    jobs_recent = []
    for r in conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 5"):
        job = _job_dict(r)
        job["progress"] = _job_progress(conn, job)
        jobs_recent.append({
            "id": job["id"], "kind": job["kind"], "status": job["status"],
            "started_at": job["started_at"], "finished_at": job["finished_at"],
            "progress": job["progress"]})
    dup_row = conn.execute(
        "SELECT COUNT(*) AS n FROM review_queue"
        " WHERE kind='dup' AND status='pending'").fetchone()
    dup_bytes = conn.execute(
        "SELECT COALESCE(SUM(f.size),0) AS b FROM review_queue q"
        " LEFT JOIN files f ON f.id=q.file_id"
        " WHERE q.kind='dup' AND q.status='pending'").fetchone()["b"]
    return {
        "version": read_local_version()["version"],  # 0.1.4：总览页显示当前版本
        "libraries": libraries,
        "review_pending": review_pending,
        "today_new": today_new,
        "jobs_recent": jobs_recent,
        "dup_reclaimable": {"count": dup_row["n"], "bytes": dup_bytes or 0},
    }
