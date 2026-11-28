#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""side-classify 移动执行（阶段 2）：按全量分类结论移动文件、写库、写清单。

输入：/data/side_classify_full.jsonl（阶段 1 结论；ffmpeg 兜底结果须先并入）。
前提：宿主机上已用 cp 备份 DB（--backup 传入备份路径，如
  /data/nas-curator.db.bak-20261124-XXXXXX-pre-side-classify）。
  本脚本不备份、不重启容器、不改 auto_apply、不删任何文件。
流程：
  - 建 plan（library='inbox'，status applying→done），summary 记 kind/manifest/backup
  - move 候选：复核 src 仍在且大小未变 → 算 dst → dst 已存在时同大小则全量 sha 比对
    （相同→转复核 dup，不删不移；不同→版本号改名）→ os.rename → 验 dst 在、src 不在
    → operations + plan_items(done) + review 行 approved（decided_by
    'side-classify' / 'side-classify-rename'，仅改 status='pending' 的行）
  - review 文件：确保 review 行存在（缺失则 INSERT，kind 按原因映射），保持 pending；
    plan_items(action='review', status='skipped')
  - 输出 manifest /data/side_classify_manifest.tsv 与 review 清单
    /data/side_classify_review.tsv；最后 integrity_check + 抽查。
目标映射：
  - 1-王震：邮件→12-项目资料/06-邮件归档/01-主归档-按年月/<YYYY-MM>/；
    照片/视频→1-王震根目录（该方无图片子目录，按任务书“没结构就放根目录”）；
    其他→12-项目资料。
  - 2/3/4：模型 subdir（须磁盘真实存在，否则 fallback）；图片进 13-图片与影像时
    下钻到 01-现场与验收照片，视频下钻到 03-视频与录音；邮件 fallback 按文件名
    关键词映射，无匹配→14-工具与其他。
同名处理：版本号 {原名}_{YYYYMMDD-HHMMSS}_{大小}_{序号}，时间优先 EXIF、否则 mtime。
"""
import sys, os, json, time, re, argparse, sqlite3, hashlib
from datetime import datetime
from collections import Counter, defaultdict

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

try:
    from PIL import Image
except Exception:
    Image = None

try:
    from common.progress import write_heartbeat as _write_hb
except Exception:
    _write_hb = None

DB = "/data/nas-curator.db"
WORK = "/vol1/1000/工作文件"
PARTY_DIR = {"1-王震": "1-王震", "2-上海润心": "2-上海润心",
             "3-北京供应链": "3-北京供应链", "4-辽宁器械": "4-辽宁器械"}

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".jfif"}
VID_EXTS = {".mov", ".mp4"}

MAIL_KW = [("周报", "10-周报与工作总结"), ("月报", "10-周报与工作总结"),
           ("录用", "11-人事与行政"), ("入职", "11-人事与行政"),
           ("离职", "11-人事与行政"), ("面试", "11-人事与行政"),
           ("人事", "11-人事与行政"), ("财务", "07-财务与经营分析"),
           ("付款", "07-财务与经营分析"), ("发票", "07-财务与经营分析"),
           ("对账", "07-财务与经营分析"), ("销售", "05-销售与业务数据"),
           ("合同", "03-合同与协议"), ("协议", "03-合同与协议"),
           ("会议", "09-会议与汇报"), ("库存", "06-库存与物流"),
           ("物流", "06-库存与物流"), ("资质", "01-公司资质与证照"),
           ("证照", "01-公司资质与证照"), ("注册", "02-产品与注册"),
           ("招投标", "12-招投标"), ("招标", "12-招投标")]


def _hb(task_id, progress_dir, **kw):
    if _write_hb is None:
        return
    try:
        _write_hb(task_id, progress_dir=progress_dir, **kw)
    except Exception as e:
        print("heartbeat failed: %s" % e, flush=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def creation_dt(path):
    if Image is not None:
        try:
            with Image.open(path) as im:
                exif = im._getexif()
            if exif:
                for tag in (36867, 36868, 306):
                    v = exif.get(tag)
                    if v:
                        try:
                            return datetime.strptime(str(v).strip(),
                                                     "%Y:%m:%d %H:%M:%S")
                        except ValueError:
                            continue
        except Exception:
            pass
    return datetime.fromtimestamp(os.stat(path).st_mtime)


def versioned_name(dst_dir, src, claimed):
    stem, ext = os.path.splitext(os.path.basename(src))
    dt = creation_dt(src).strftime("%Y%m%d-%H%M%S")
    size = os.stat(src).st_size
    seq = 1
    while True:
        name = "%s_%s_%d_%d%s" % (stem, dt, size, seq, ext)
        cand = os.path.join(dst_dir, name)
        if cand not in claimed and not os.path.exists(cand):
            claimed.add(cand)
            return name, cand
        seq += 1


def mail_fallback_subdir(filename):
    for kw, sub in MAIL_KW:
        if kw in filename:
            return sub
    return "14-工具与其他"


def compute_dst(rec, claimed):
    """返回 (dst_path, dst_dir, subdir_used)。"""
    party = rec["party"]
    base = os.path.join(WORK, PARTY_DIR[party])
    kind = rec["src_kind"]
    ext = rec["ext"]
    name = rec["filename"]
    if party == "1-王震":
        if kind == "C-邮件归档":
            month = rec.get("mail_month") or "日期未知"
            d = os.path.join(base, "12-项目资料", "06-邮件归档",
                             "01-主归档-按年月", month)
            return os.path.join(d, name), d, "12-项目资料/06-邮件归档/01-主归档-按年月/" + month
        d = base
        return os.path.join(d, name), d, "(根目录)"
    sub = rec.get("subdir") or ""
    if not sub or not os.path.isdir(os.path.join(base, sub)):
        if kind == "C-邮件归档":
            sub = mail_fallback_subdir(name)
        elif ext in VID_EXTS or ext in IMG_EXTS:
            sub = "13-图片与影像"
        else:
            sub = "14-工具与其他"
    d = os.path.join(base, sub)
    sub_used = sub
    if sub == "13-图片与影像":
        if ext in VID_EXTS:
            deep = os.path.join(d, "03-视频与录音")
            if os.path.isdir(deep):
                d, sub_used = deep, sub + "/03-视频与录音"
        elif ext in IMG_EXTS:
            deep = os.path.join(d, "01-现场与验收照片")
            if os.path.isdir(deep):
                d, sub_used = deep, sub + "/01-现场与验收照片"
    return os.path.join(d, name), d, sub_used


REVIEW_KIND = {"重复": "dup", "损坏": "ambiguous", "无法判断": "ambiguous",
               "低置信": "low_conf", "API失败": "low_conf", "不可读": "ambiguous",
               "处理异常": "low_conf", "目标已存在重复": "dup"}


def review_kind_for(reason):
    for k, v in REVIEW_KIND.items():
        if reason.startswith(k):
            return v
    return "ambiguous"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="/data/side_classify_full.jsonl")
    ap.add_argument("--backup", required=True, help="宿主机 cp 的 DB 备份路径（/data/...）")
    ap.add_argument("--manifest", default="/data/side_classify_manifest.tsv")
    ap.add_argument("--review-tsv", default="/data/side_classify_review.tsv")
    args = ap.parse_args()

    if not os.path.exists(args.backup):
        print("backup not found: %s" % args.backup)
        return 2
    print("backup: %s (%d bytes)" % (args.backup, os.path.getsize(args.backup)),
          flush=True)

    recs = []
    with open(args.results, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    print("results: %d" % len(recs), flush=True)
    move_recs = [r for r in recs if r.get("decision") == "move"]
    review_recs = [r for r in recs if r.get("decision") != "move"]
    print("move candidates: %d, review: %d" % (len(move_recs), len(review_recs)),
          flush=True)

    db = sqlite3.connect(DB)
    db.execute("PRAGMA busy_timeout=30000")
    cur = db.execute(
        "INSERT INTO plans (library, summary) VALUES ('inbox', ?)",
        (json.dumps({"kind": "side-classify", "manifest": args.manifest,
                     "backup": args.backup}, ensure_ascii=False),))
    plan_id = cur.lastrowid
    db.execute("UPDATE plans SET status='applying' WHERE id=?", (plan_id,))
    db.commit()
    print("plan #%d created" % plan_id, flush=True)

    progress_dir = "/data/progress"
    total = len(recs)
    _hb("side-classify-apply", progress_dir, label="待整理三处移动执行",
        kind="side-classify-apply", phase="apply", status="running",
        total=total, done=0)
    t0 = time.time()
    counts = Counter()
    claimed = set()
    manifest_rows = []
    review_rows = []
    n = 0

    def find_rq(src):
        row = db.execute(
            "SELECT id, status FROM review_queue "
            "WHERE json_extract(payload,'$.src')=?", (src,)).fetchone()
        return row

    def ensure_rq(rec, kind, status, decided_by=None):
        src = rec["src"]
        row = find_rq(src)
        payload = {"src": src, "category": rec.get("party"),
                   "confidence": rec.get("confidence"),
                   "reason": rec.get("reason", ""),
                   "suggested_dst": rec.get("_dst") or "",
                   "side_party": rec.get("party"),
                   "side_subdir": rec.get("_subdir_used") or "",
                   "side_method": rec.get("method", ""),
                   "side_kind": rec.get("src_kind", ""),
                   "tags": ["side-classify"]}
        if row:
            rq_id, st = row
            if status == "approved" and st == "pending":
                db.execute(
                    "UPDATE review_queue SET status='approved', decided_by=?, "
                    "decided_at=strftime('%Y-%m-%dT%H:%M:%S','now'), payload=? "
                    "WHERE id=?", (decided_by, json.dumps(payload, ensure_ascii=False),
                                   rq_id))
                return rq_id, "updated"
            if status == "pending" and st == "pending":
                db.execute("UPDATE review_queue SET payload=? WHERE id=?",
                           (json.dumps(payload, ensure_ascii=False), rq_id))
                return rq_id, "kept-pending"
            return rq_id, "already-" + st
        cur2 = db.execute(
            "INSERT INTO review_queue (kind, payload, status, decided_by, "
            "decided_at) VALUES (?,?,?,?,?)",
            (kind, json.dumps(payload, ensure_ascii=False), status,
             decided_by,
             time.strftime("%Y-%m-%dT%H:%M:%S") if status == "approved" else None))
        return cur2.lastrowid, "inserted"

    # ---- move ----
    for rec in sorted(move_recs, key=lambda r: r["src"]):
        n += 1
        src = rec["src"]
        try:
            st = os.stat(src)
        except FileNotFoundError:
            counts["skip-src-gone"] += 1
            manifest_rows.append((rec, "", "skip-src-gone"))
            review_rows.append((rec, "源文件已不在盘"))
            db.execute(
                "INSERT INTO plan_items (plan_id, src_path, dst_path, category, "
                "confidence, reason, evidence, action, kind, size, ext, "
                "review_kind, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, src, None, rec.get("party"), rec.get("confidence"),
                 "side-classify:src-gone", rec.get("method", ""), "review",
                 "side-classify", rec.get("size"), rec.get("ext"), "ambiguous",
                 "skipped"))
            ensure_rq(rec, "ambiguous", "pending")
            continue
        if rec.get("size") and int(rec["size"]) != st.st_size:
            counts["skip-size-changed"] += 1
            manifest_rows.append((rec, "", "skip-size-changed"))
            review_rows.append((rec, "大小变化未移动"))
            db.execute(
                "INSERT INTO plan_items (plan_id, src_path, dst_path, category, "
                "confidence, reason, evidence, action, kind, size, ext, "
                "review_kind, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, src, None, rec.get("party"), rec.get("confidence"),
                 "side-classify:size-changed", rec.get("method", ""), "review",
                 "side-classify", rec.get("size"), rec.get("ext"), "ambiguous",
                 "skipped"))
            ensure_rq(rec, "ambiguous", "pending")
            continue
        dst, dst_dir, sub_used = compute_dst(rec, claimed)
        rec["_dst"] = dst
        rec["_subdir_used"] = sub_used
        os.makedirs(dst_dir, exist_ok=True)
        action = "move"
        decided = "side-classify"
        detail = "move"
        if os.path.exists(dst) or dst in claimed:
            if os.path.exists(dst) and os.path.getsize(dst) == st.st_size:
                try:
                    same = sha256_file(dst) == sha256_file(src)
                except Exception:
                    same = False
                if same:
                    counts["dup-at-dst"] += 1
                    manifest_rows.append((rec, dst, "dup-at-dst"))
                    review_rows.append((rec, "目标已存在重复：与目标文件内容完全相同"))
                    db.execute(
                        "INSERT INTO plan_items (plan_id, src_path, dst_path, "
                        "category, confidence, reason, evidence, action, kind, "
                        "size, ext, review_kind, status) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (plan_id, src, dst, rec.get("party"),
                         rec.get("confidence"), "side-classify:dup-at-dst",
                         rec.get("method", ""), "review", "side-classify",
                         rec.get("size"), rec.get("ext"), "dup", "skipped"))
                    ensure_rq(rec, "dup", "pending")
                    continue
            new_name, dst = versioned_name(dst_dir, src, claimed)
            action = "rename"
            decided = "side-classify-rename"
            detail = "rename-to:" + new_name
            rec["_dst"] = dst
        try:
            os.rename(src, dst)
        except Exception as e:
            counts["failed-move"] += 1
            manifest_rows.append((rec, dst, "failed-move:%s" % e))
            review_rows.append((rec, "移动失败：%s" % e))
            db.execute(
                "INSERT INTO plan_items (plan_id, src_path, dst_path, category, "
                "confidence, reason, evidence, action, kind, size, ext, "
                "review_kind, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, src, dst, rec.get("party"), rec.get("confidence"),
                 "side-classify:failed-move", rec.get("method", ""), "review",
                 "side-classify", rec.get("size"), rec.get("ext"), "ambiguous",
                 "failed"))
            ensure_rq(rec, "ambiguous", "pending")
            db.commit()
            continue
        if not os.path.exists(dst) or os.path.exists(src):
            counts["failed-verify"] += 1
            manifest_rows.append((rec, dst, "failed-verify"))
            review_rows.append((rec, "移动后校验失败"))
            db.execute(
                "INSERT INTO plan_items (plan_id, src_path, dst_path, category, "
                "confidence, reason, evidence, action, kind, size, ext, "
                "review_kind, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, src, dst, rec.get("party"), rec.get("confidence"),
                 "side-classify:failed-verify", rec.get("method", ""), "review",
                 "side-classify", rec.get("size"), rec.get("ext"), "ambiguous",
                 "failed"))
            ensure_rq(rec, "ambiguous", "pending")
            db.commit()
            continue
        claimed.add(dst)
        db.execute(
            "INSERT INTO plan_items (plan_id, src_path, dst_path, category, "
            "confidence, reason, evidence, action, kind, size, ext, "
            "review_kind, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, src, dst, rec.get("party"), rec.get("confidence"),
             "side-classify:" + detail, rec.get("method", ""), action,
             "side-classify", rec.get("size"), rec.get("ext"),
             "low_conf", "done"))
        db.execute(
            "INSERT INTO operations (plan_id, src_path, dst_path) VALUES (?,?,?)",
            (plan_id, src, dst))
        _rq, note = ensure_rq(rec, "low_conf", "approved", decided)
        counts["file-" + action] += 1
        counts["rq-" + note] += 1
        counts["review-rows-approved"] += 1
        manifest_rows.append((rec, dst, action))
        if n % 200 == 0:
            db.commit()
            print("%d/%d applied (%.1fs) counts=%s"
                  % (n, total, time.time() - t0, dict(counts)), flush=True)
            _hb("side-classify-apply", progress_dir, label="待整理三处移动执行",
                kind="side-classify-apply", phase="apply", status="running",
                total=total, done=n)
    db.commit()

    # ---- review ----
    for rec in sorted(review_recs, key=lambda r: r["src"]):
        n += 1
        reason = rec.get("review_reason") or "复核"
        kind = review_kind_for(reason)
        if rec.get("status") == "salvage":
            reason = "待ffmpeg兜底抽帧后复判（cv2 零帧）"
            kind = "ambiguous"
        db.execute(
            "INSERT INTO plan_items (plan_id, src_path, dst_path, category, "
            "confidence, reason, evidence, action, kind, size, ext, "
            "review_kind, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, rec["src"], None, rec.get("party"),
             rec.get("confidence"), "side-classify:review:" + reason,
             rec.get("method", ""), "review", "side-classify",
             rec.get("size"), rec.get("ext"), kind, "skipped"))
        ensure_rq(rec, kind, "pending")
        counts["review-" + kind] += 1
        manifest_rows.append((rec, "", "review:" + reason))
        review_rows.append((rec, reason))
        if n % 500 == 0:
            db.commit()
    db.commit()

    with open(args.manifest, "w", encoding="utf-8") as f:
        f.write("src\tdst\tparty\tsubdir\tconfidence\tmethod\tdecision\tinfo\n")
        for rec, dst, info in manifest_rows:
            f.write("%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" % (
                rec["src"], dst, rec.get("party", ""), rec.get("subdir", ""),
                rec.get("confidence", ""), rec.get("method", ""),
                rec.get("decision", ""), info))
    with open(args.review_tsv, "w", encoding="utf-8") as f:
        f.write("src\tparty\tconfidence\treason\n")
        for rec, reason in review_rows:
            f.write("%s\t%s\t%s\t%s\n" % (
                rec["src"], rec.get("party", ""), rec.get("confidence", ""),
                reason.replace("\t", " ").replace("\n", " ")))

    db.execute("UPDATE plans SET status='done' WHERE id=?", (plan_id,))
    db.commit()
    integ = db.execute("PRAGMA integrity_check").fetchone()[0]
    items = db.execute(
        "SELECT status, COUNT(*) FROM plan_items WHERE plan_id=? GROUP BY status",
        (plan_id,)).fetchall()
    ops = db.execute("SELECT COUNT(*) FROM operations WHERE plan_id=?",
                     (plan_id,)).fetchone()[0]
    approved = db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE decided_by LIKE 'side-classify%'"
        " AND status='approved'").fetchone()[0]
    print("plan #%d done. counts=%s" % (plan_id, dict(counts)), flush=True)
    print("plan_items=%s operations=%d approved_rows_total=%d integrity=%s"
          % (items, ops, approved, integ), flush=True)
    print("manifest=%s review=%s" % (args.manifest, args.review_tsv), flush=True)
    _hb("side-classify-apply", progress_dir, label="待整理三处移动执行",
        kind="side-classify-apply", phase="apply", status="done",
        total=total, done=n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
