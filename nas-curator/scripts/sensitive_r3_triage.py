#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第三轮 sensitive 分流判断（sensitive-r3，2026-11-28）：只判断，不删除。

对 81 条 sensitive 行重跑 _sensitive_hit（文件名命中词 vs 正文命中词
分开算），只输出触发词与位置，不输出任何文件内容、文件名、原文片段
以外的敏感细节。
- 正文命中 密码/身份证/护照/银行卡 → B：真含证件/财务/隐私信息，不动。
- 文件名命中 身份证/护照/银行卡 → C：文件本身很可能是证件/卡片，不动。
- 仅文件名命中 账号/登录/密码/password/token 等通用词且正文无命中
  → A：疑似误判候选，列出供文档 LLM 分类复核（另行处理）。
- 两处都无命中 → D：无法复现触发原因，不动，列清单请母本定。

保存 /data/sensitive_r3_triage.json：每行只有 rq_id、bucket、触发词、
触发位置、抽取器、文本长度——无文件名、无内容。
stdout 打印的上下文窗口已做数字掩码（连续 4 位以上数字→****），
且不写入任何文件。
"""
import sys, os, json, re, sqlite3
from collections import Counter

sys.path.insert(0, "/app")
sys.path.insert(0, "/data")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

import remaining_docs_classify as RDC
from curator_core.engine import _sensitive_hit

DB = "/data/nas-curator.db"
OUT = "/data/sensitive_r3_triage.json"
BUCKET_A = "/data/sensitive_r3_bucketA.tsv"

NAME_CRED = ("身份证", "护照", "银行卡")
NAME_GENERIC = ("密码", "password", "账号", "登录", "token")
BODY_WORDS = ("密码", "身份证", "护照", "银行卡")


def mask_digits(t):
    return re.sub(r"\d{4,}", "****", t or "")


def window(body, word, n=30):
    i = (body or "").find(word)
    if i < 0:
        return ""
    return mask_digits(body[max(0, i - n):i + len(word) + n].replace("\n", " "))


def main():
    conn = sqlite3.connect(DB, timeout=30)
    rows = conn.execute(
        "SELECT id, json_extract(payload,'$.src') AS src FROM review_queue "
        "WHERE status='pending' AND kind='sensitive' ORDER BY id").fetchall()
    print("sensitive pending: %d" % len(rows), flush=True)
    buckets = Counter()
    records = []
    bucket_a = []
    for rid, src in rows:
        name = os.path.basename(src or "")
        try:
            text, extractor, enc = RDC.extract(src, os.path.splitext(src or "")[1].lower())
        except Exception as e:  # noqa: BLE001
            text, extractor, enc = "", "extract-failed:%s" % type(e).__name__, False
        if enc:
            text, extractor = "", "encrypted:" + extractor
        name_hits = _sensitive_hit(name, "")
        body_hits = _sensitive_hit("", text or "")
        if any(w in name_hits for w in NAME_CRED):
            bucket = "C-name-credential"
        elif body_hits:
            bucket = "B-body-sensitive"
        elif name_hits:
            bucket = "A-name-generic"
        else:
            bucket = "D-no-hit"
        buckets[bucket] += 1
        rec = {"rq_id": rid, "bucket": bucket,
               "name_hits": name_hits, "body_hits": body_hits,
               "extractor": extractor, "text_len": len(text or "")}
        records.append(rec)
        if bucket == "A-name-generic":
            bucket_a.append((rid, src))
        # 只打印触发词与掩码窗口（不打印文件名、不打印原文、不存盘）。
        wins = {w: window(text, w) for w in body_hits}
        print("rq=%d bucket=%s name_hits=%s body_hits=%s extractor=%s text_len=%d wins=%s"
              % (rid, bucket, name_hits, body_hits, extractor,
                 len(text or ""), json.dumps(wins, ensure_ascii=False)),
              flush=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"buckets": dict(buckets), "rows": records}, f,
                  ensure_ascii=False, indent=2)
    with open(BUCKET_A, "w", encoding="utf-8") as f:
        f.write("rq_id\tsrc\n")
        for rid, src in bucket_a:
            f.write("%d\t%s\n" % (rid, src))
    print("TRIAGE DONE buckets=%s" % dict(buckets), flush=True)
    print("saved %s and %s (bucket A: %d)" % (OUT, BUCKET_A, len(bucket_a)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
