#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NEF 单张补跑（容器内）：rq=124411。
PIL 能解出其 160x120 内嵌预览图，故按两阶段口径补跑：
model-3 过筛 -> vlm_economy_*；若 >=0.72 则 agent-plan 确认 -> vlm_*。
只写这一行，不动其他行。Key：无（只用方舟通道）。
"""
import sys, os, json, time, base64, sqlite3

sys.path.insert(0, "/app")
sys.path.insert(0, "/data")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")

import vlm_reclassify_batch4 as V
import vlm_screen_midconf as S
import vlm_confirm_midconf as C

DB = "/data/nas-curator.db"
RQ = 124411

def main():
    conn = sqlite3.connect(DB, timeout=30)
    row = conn.execute("SELECT payload FROM review_queue WHERE id=? AND status='pending'", (RQ,)).fetchone()
    d = json.loads(row[0])
    src = d.get("src", "")
    print("src: %s" % src)
    raw, _o, _r = V.prep_image(src)
    print("decoded: %s -> %s" % (_o, _r))
    b64 = base64.b64encode(raw).decode()
    messages = [
        {"role": "system", "content": "只输出 JSON，不要解释。"},
        {"role": "user", "content": [
            {"type": "text", "text": V.build_prompt(os.path.basename(src))},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ]},
    ]
    llm = V.LLM()
    ark = [p for p in llm.providers if p.get("kind") == "ark"]
    econ = next((p for p in ark if p.get("name") == "model-3"), None)
    plan = next((p for p in ark if p.get("name") == "agent-plan"), None)
    # stage 1: model-3 screen
    cat, conf, reason, err = V.try_provider(llm, econ, messages)
    if err is not None:
        print("SCREEN api_error: %s" % err[:200])
        return 1
    print("SCREEN model-3 -> %s(%.2f)" % (cat, conf))
    d["vlm_economy_category"] = cat
    d["vlm_economy_confidence"] = conf
    d["vlm_economy_reason"] = reason
    d["vlm_economy_model"] = econ["model"]
    d["vlm_economy_source"] = "vlm-midconf-screen"
    conn.execute("UPDATE review_queue SET payload=?, decided_by='vlm-midconf-screen' WHERE id=?",
                 (json.dumps(d, ensure_ascii=False), RQ))
    conn.commit()
    if conf < 0.72:
        print("SCREEN <0.72: no agent-plan confirm; final falls back to SigLIP")
        return 0
    cat2, conf2, reason2, err2 = V.try_provider(llm, plan, messages)
    if err2 is not None:
        print("CONFIRM api_error: %s" % err2[:200])
        return 1
    print("CONFIRM agent-plan -> %s(%.2f)" % (cat2, conf2))
    d["vlm_category"] = cat2
    d["vlm_confidence"] = conf2
    d["vlm_reason"] = reason2
    d["vlm_model"] = plan["model"]
    d["vlm_source"] = "vlm-midconf"
    conn.execute("UPDATE review_queue SET payload=?, decided_by='vlm-midconf' WHERE id=?",
                 (json.dumps(d, ensure_ascii=False), RQ))
    conn.commit()
    print("NEF-ONEOFF DONE rq=124411 screen=%.2f confirm=%s" % (conf, conf2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
