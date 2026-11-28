#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第三轮 OCR 分类（ocr-r3，2026-11-28）：扫描件补文字再分类。

范围：pending + ambiguous + reason scanned-no-text（429 条，全部 .pdf），
以及 confidence=0.7 的带建议行（60 条：48 条 scan-name + 12 条 llm:0.70）。
只用 agent-plan（免费额度）做文档 LLM 分类，不用阿里（本端无 Key）、
不用 model-3 经济版。

流程（两阶段，与 remaining-docs 流程一致）：
  --run：逐行抽文本（pdftotext 前 3 页；无文本的 PDF 调 ocr-service
    RapidOCR 前 3 页）；有文本则 agent-plan 判所属方+子目录；结果写
    /data/ocr_r3_results.jsonl（断点续跑），并把 ocr 证据（字符数、
    脱敏摘录）写进复核行 payload（只加键，不改状态）。
  --register：读 JSONL，party 已定且 confidence>=0.75 的批准移动
    （decided_by=system:clear-r3-ocr，batch_id=ocr-r3-20261128）；
    其余只更新 suggested_dst/证据，留队列。
  --dry-run：只报数，不写 DB、不调模型（先看工作量）。

安全：OCR 文本只用于判类；写进 payload 的摘录先脱敏（连续 4 位以上
数字掩码），不存全文；stdout 不打印任何 OCR 原文。
"""
import sys, os, json, re, time, argparse, sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

sys.path.insert(0, "/app")
sys.path.insert(0, "/data")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

import remaining_docs_classify as RDC
from curator_core.llmkit import LLM

DB = "/data/nas-curator.db"
RESULTS = "/data/ocr_r3_results.jsonl"
SUMMARY = "/data/ocr_r3_summary.json"
CONFIG_PATH = "/data/config/libraries.json"
BATCH_ID = "ocr-r3-20261128"
AUTO_CONF = 0.75  # LLM 文档自动执行阈值（0.2.5 口径）


def mask_digits(t):
    return re.sub(r"\d{4,}", "****", t or "")


def load_roots():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    roots = {}
    for lib in cfg.get("libraries", []):
        roots[lib["id"]] = lib["root"]
    return roots


def load_worklist(conn, limit=None):
    rows = conn.execute(
        "SELECT id, json_extract(payload,'$.src') AS src, "
        "json_extract(payload,'$.reason') AS reason, "
        "json_extract(payload,'$.suggested_dst') AS dst, payload "
        "FROM review_queue WHERE status='pending' AND kind='ambiguous' "
        "AND (json_extract(payload,'$.reason') LIKE 'scanned-no-text%' "
        "OR CAST(json_extract(payload,'$.confidence') AS REAL)=0.7) "
        "ORDER BY id").fetchall()
    if limit:
        rows = rows[:limit]
    return rows


def classify_ark(text, filename):
    """只用 agent-plan。返回 (out, err)。"""
    messages = RDC.build_messages(text, filename)
    out, err = RDC.call_ark(messages)
    return out, err


def process_one(item):
    rid, src, reason, old_dst, payload = item
    d = json.loads(payload) if payload else {}
    rec = {"rq_id": rid, "src": src, "reason_old": reason,
           "suggested_dst_old": old_dst or "", "extractor": "",
           "ocr_used": False, "ocr_chars": 0, "ocr_err": "",
           "party": "", "subdir": "", "confidence": 0.0,
           "doc_reason": "", "doc_model": "", "why": ""}
    ext = os.path.splitext(src or "")[1].lower()
    if not src or not os.path.exists(src):
        rec["why"] = "src-gone"
        return rec
    try:
        text, extractor, enc = RDC.extract(src, ext)
    except Exception as e:  # noqa: BLE001
        rec.update({"why": "extract-failed",
                    "doc_reason": "extract-failed:%s" % type(e).__name__})
        return rec
    if enc:
        rec.update({"why": "encrypted", "extractor": extractor})
        return rec
    rec["extractor"] = extractor
    ocr_used, ocr_chars, ocr_err = False, 0, ""
    if not text.strip():
        if ext == ".pdf":
            otext, oerr = RDC.ocr_pdf(src)
            if oerr:
                ocr_err = oerr
            elif len(otext.strip()) >= 20:
                text = otext
                ocr_used, ocr_chars = True, len(otext.strip())
            else:
                ocr_chars = len(otext.strip())
        else:
            stext = RDC.strings_text(src)
            if stext:
                text = stext
                rec["extractor"] = "strings-fallback"
    rec.update({"ocr_used": ocr_used, "ocr_chars": ocr_chars,
                "ocr_err": ocr_err})
    if not text.strip():
        rec["why"] = "doc-empty"
        return rec
    out, err = classify_ark(text, os.path.basename(src))
    if err is not None:
        rec.update({"why": "api_error", "doc_reason": "api_error:%s" % err[:150]})
        return rec
    rec.update({"party": out["party"], "subdir": out["subdir"],
                "confidence": round(float(out["confidence"] or 0), 3),
                "doc_reason": str(out["reason"])[:300],
                "doc_model": out.get("model", ""),
                "why": "doc-llm-ocr" if ocr_used else "doc-llm"})
    rec["excerpt_masked"] = mask_digits(text.strip()[:150])
    return rec


def update_payload_ocr(conn, rec):
    """把 ocr 证据写进 payload（只加键，不改状态/置信度/建议目标）。"""
    row = conn.execute("SELECT payload FROM review_queue WHERE id=?",
                       (rec["rq_id"],)).fetchone()
    if not row:
        return
    d = json.loads(row[0]) if row[0] else {}
    d["ocr_r3_chars"] = rec["ocr_chars"]
    d["ocr_r3_used"] = rec["ocr_used"]
    d["ocr_r3_err"] = rec["ocr_err"]
    d["ocr_r3_excerpt"] = rec.get("excerpt_masked", "")
    d["ocr_r3_party"] = rec["party"]
    d["ocr_r3_subdir"] = rec["subdir"]
    d["ocr_r3_confidence"] = rec["confidence"]
    d["ocr_r3_model"] = rec["doc_model"]
    d["ocr_r3_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute("UPDATE review_queue SET payload=? WHERE id=?",
                 (json.dumps(d, ensure_ascii=False), rec["rq_id"]))
    conn.commit()


def run_phase(limit, workers, dry_run):
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    rows = load_worklist(conn, limit)
    print("worklist: %d (scanned-no-text + conf=0.7)" % len(rows), flush=True)
    by_reason = Counter(r[2] for r in rows)
    print("by reason: %s" % dict(by_reason), flush=True)
    if dry_run:
        print("DRY-RUN: no DB writes, no model calls", flush=True)
        return {"worklist": len(rows), "dry_run": True}
    done = {}
    if os.path.exists(RESULTS):
        with open(RESULTS, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    done[r["rq_id"]] = r
                except Exception:
                    continue
    todo = [r for r in rows if r[0] not in done]
    print("already in results: %d; to process: %d" % (len(done), len(todo)),
          flush=True)
    if not todo:
        return {"worklist": len(rows), "already": len(done), "processed": 0}
    t0 = time.time()
    counts = Counter()
    with open(RESULTS, "a", encoding="utf-8") as fh:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(process_one, it): it for it in todo}
            n = 0
            for fut in as_completed(futs):
                try:
                    rec = fut.result()
                except Exception as e:  # noqa: BLE001
                    it = futs[fut]
                    rec = {"rq_id": it[0], "src": it[1], "why": "worker-exception",
                           "doc_reason": type(e).__name__, "ocr_chars": 0,
                           "ocr_used": False, "ocr_err": "", "party": "",
                           "subdir": "", "confidence": 0.0, "doc_model": ""}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                update_payload_ocr(conn, rec)
                counts[rec["why"]] += 1
                if rec["why"] in ("doc-llm", "doc-llm-ocr"):
                    counts["determined" if rec["party"] != "无法判断"
                           else "undetermined"] += 1
                    if rec["party"] != "无法判断" and rec["confidence"] >= AUTO_CONF:
                        counts["ge075"] += 1
                n += 1
                if n % 25 == 0:
                    print("PROG %d/%d %.0fs %s" % (n, len(todo), time.time() - t0,
                                                    dict(counts)), flush=True)
    print("RUN DONE n=%d elapsed=%.0fs counts=%s"
          % (len(todo), time.time() - t0, dict(counts)), flush=True)
    return {"worklist": len(rows), "processed": len(todo),
            "counts": dict(counts), "elapsed_s": round(time.time() - t0, 1)}


def register_phase(limit, dry_run):
    from api.review import DecideBody, decide_one
    from common import db as DBM
    roots = load_roots()
    work_root = roots.get("work", "")
    conn = DBM.connect(DB)
    results = []
    if os.path.exists(RESULTS):
        with open(RESULTS, encoding="utf-8") as fh:
            for line in fh:
                try:
                    results.append(json.loads(line))
                except Exception:
                    continue
    print("results in file: %d" % len(results), flush=True)
    counts = Counter()
    agree = Counter()
    for rec in results:
        if limit and counts["considered"] >= limit:
            break
        counts["considered"] += 1
        rid = rec["rq_id"]
        row = conn.execute(
            "SELECT payload FROM review_queue WHERE id=? AND status='pending'",
            (rid,)).fetchone()
        if not row:
            counts["skip-not-pending"] += 1
            continue
        d = json.loads(row[0]) if row[0] else {}
        src = d.get("src", "")
        party, subdir, conf = rec.get("party", ""), rec.get("subdir", ""), \
            float(rec.get("confidence") or 0)
        old_dst = d.get("suggested_dst", "") or ""
        if party and party != "无法判断" and subdir:
            new_dst = os.path.join(work_root, party, subdir,
                                   os.path.basename(src))
            old_sub = os.path.basename(os.path.dirname(old_dst)) \
                if old_dst else ""
            agree["agree-subdir" if old_sub == subdir else
                  "disagree-subdir" if old_sub else "no-old-suggestion"] += 1
        else:
            new_dst = ""
        if party and party != "无法判断" and subdir and conf >= AUTO_CONF:
            counts["auto-eligible"] += 1
            if dry_run:
                continue
            body = DecideBody(decision="approved", suggested_dst=new_dst)
            res = decide_one(conn, rid, "approved", body,
                             decided_by="system:clear-r3-ocr",
                             batch_id=BATCH_ID)
            if res.get("moved"):
                counts["moved"] += 1
            else:
                counts["approve-failed"] += 1
                print("  #%d approve failed: %s" % (rid, res.get("note", "")),
                      flush=True)
        else:
            counts["left-pending"] += 1
            if dry_run:
                continue
            # 只更新建议目标与证据，不改状态。
            if new_dst:
                d["suggested_dst"] = new_dst
                d["confidence"] = max(float(d.get("confidence") or 0), conf)
                d["reason"] = "ocr-r3:%s" % (rec.get("doc_reason", "")[:120])
            conn.execute("UPDATE review_queue SET payload=? WHERE id=?",
                         (json.dumps(d, ensure_ascii=False), rid))
            conn.commit()
    print("REGISTER DONE counts=%s agree=%s (dry_run=%s)"
          % (dict(counts), dict(agree), dry_run), flush=True)
    return {"counts": dict(counts), "agree": dict(agree),
            "dry_run": dry_run}


def main():
    ap = argparse.ArgumentParser(description="第三轮 OCR 分类")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    llm = LLM()
    RDC.ARK_PRIMARY = RDC.pick_providers(llm)
    print("ark primary: %s | model: %s | billing: %s"
          % (RDC.ARK_PRIMARY["name"], RDC.ARK_PRIMARY["model"],
             RDC.ARK_PRIMARY.get("billing")), flush=True)
    print("aliyun: NOT USED (no key on this side); model-3: NOT USED",
          flush=True)
    out = {}
    if args.run:
        out["run"] = run_phase(args.limit, args.workers, args.dry_run)
    if args.register:
        out["register"] = register_phase(args.limit, args.dry_run)
    if not args.run and not args.register:
        ap.print_help()
        return 2
    with open(SUMMARY, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved " + SUMMARY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
