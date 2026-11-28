#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Review second pass (stage 1d): resolve readable-but-unattributed review records
with a text-model call over (filename, VLM/doc reason, source dir).
Blank/solid/damaged/unreadable/encrypted stay in review.
Inputs: /data/side_classify_full.jsonl + /data/side_classify_def.jsonl (review rows).
Outputs: /data/side_review_resolved.jsonl (moves), /data/side_review_remaining.jsonl.
Read-only: no moves, no DB writes.
"""
import sys, os, json, time, re
sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
from curator_core.llmkit import LLM

sys.path.insert(0, "/data")
import importlib.util
spec = importlib.util.spec_from_file_location("scf", "/data/side_classify_full.py")
scf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scf)

KEEP_PAT = re.compile(r"纯白|全白|全黑|纯色|纯黑|空白|纯灰|纯蓝|纯红|纯粉|纯橙|纯黄|纯绿|"
                      r"均匀灰色|近乎纯色|极度模糊|完全模糊|严重模糊|严重损坏|损坏|不可读|加密|"
                      r"API失败|处理异常|解码失败|无法抽帧|ffmpeg", re.S)

PROMPT = """你是文件归档专家。以下是一份已读出内容、但首轮无法确定所属方的文件。
只根据给出的线索（文件名、首轮看图/读文的描述、来源目录）判断它属于哪一方、进哪个子目录。
不要按文件类型分，只按内容所属方分。

归属规则（按顺序）：
1) 描述中出现辽宁地名/医院（沈阳、辽阳、锦州、营口、抚顺、辽源、北部战区、辽宁省、丹东、本溪、铁岭、朝阳、葫芦岛、鞍山、盘锦、大连、梅河口、通化）→ 4-辽宁器械。
2) 描述中出现上海/润心/华润医药/波科/BK/微术/旭化成/浙江事业部/上海业务组/温州仓/上海仓 → 2-上海润心。
3) 描述中出现北京/世龙经略/润邦/北京供应链 → 3-北京供应链。
4) 业务内容（医院、产品、会议、培训、报表、合同、资质、招投标、临床）但无地域/公司信号 → 2-上海润心（王震任总经理的公司，为默认业务归属方）。
5) 生活/家庭/旅游/风景/美食/随手拍等私人内容 → 1-王震。
6) 邮件：按主题/发件人定方；周报→10-周报与工作总结，人事录用/入职/离职→11-人事与行政，财务付款/对账/发票→07-财务与经营分析，销售数据→05-销售与业务数据，合同协议→03-合同与协议，会议→09-会议与汇报，库存物流→06-库存与物流，资质证照→01-公司资质与证照，产品注册→02-产品与注册，招投标→12-招投标，系统通知/营销广告/个人服务类（收件人为王震本人且无公司信号）→1-王震。
子目录：从该方子目录列表中选最贴近的；1-王震的照片/视频没有贴近子目录时填空；
2/3/4号的图片影像类填 13-图片与影像；1-王震的邮件填 12-项目资料。
各方子目录：1-王震[00-整理说明，09-会议与汇报，10-周报与工作总结，12-项目资料，14-工具与其他]；
2-上海润心/3-北京供应链/4-辽宁器械[01-公司资质与证照，02-产品与注册，03-合同与协议，04-供应商与采购，05-销售与业务数据，06-库存与物流，07-财务与经营分析，08-运营与项目，09-会议与汇报，10-周报与工作总结，11-人事与行政，12-招投标，13-图片与影像，14-工具与其他（4-辽宁器械另有 15-临床与学术资料；2-上海润心另有 12-项目资料）]。
如果线索实在不足以定方，party 填"无法判断"，subdir 为空。

只输出 JSON：{"party":"方名（原样输出，如 2-上海润心 或 无法判断）","subdir":"子目录名（原样输出）","confidence":0.85,"reason":"一句话理由，说明依据哪条线索"}
"""




THREE_WAY_PROMPT = """你是文件归档专家。以下是一份已读出内容、但首轮无法确定归属的图片文件（来自 Mac 下载或媒体库待确认）。
只根据给出的线索（文件名、首轮看图描述、来源目录）判断它属于工作、家庭还是媒体。
规则：
1) 应用图标/ App 图标 / 界面截图 / 无明确内容的图标类图片 → 媒体，party 填"媒体库"，subdir 填"照片/屏幕截图"。
2) 工作文档/合同/报表/产品/会议截图 → 工作，party 为 1-王震/2-上海润心/3-北京供应链/4-辽宁器械之一，subdir 为编号子目录（01-公司资质与证照、02-产品与注册、03-合同与协议、04-供应商与采购、05-销售与业务数据、06-库存与物流、07-财务与经营分析、08-运营与项目、09-会议与汇报、10-周报与工作总结、11-人事与行政、12-招投标、13-图片与影像、14-工具与其他；1-王震只有 00-整理说明、09-会议与汇报、10-周报与工作总结、12-项目资料、14-工具与其他）。
3) 家庭/生活照片 → 家庭，party 填"家庭文件"，subdir 为相对家庭文件/的路径（如 刘音/会议、王震/证件）。
4) 地名线索：辽宁地名/医院→4-辽宁器械；上海/润心/华润医药/波科→2-上海润心；北京/世龙经略→3-北京供应链。
5) 线索实在不足以定归属时 dest 填"无法判断"。
只输出 JSON：{"dest":"work 或 family 或 media 或 无法判断","party":"方名","subdir":"子目录","confidence":0.85,"reason":"一句话理由，说明依据哪条线索"}
"""

def load_reviews(path):
    rows = []
    if not os.path.exists(path):
        return rows
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("decision") == "review":
            rows.append(r)
    return rows


def main():
    llm = LLM()
    primary, fallback = scf.pick_providers(llm)
    print("primary: %s | model: %s" % (primary["name"], primary["model"]), flush=True)
    reviews = load_reviews("/data/side_classify_new5.jsonl")
    reviews += load_reviews("/data/side_classify_salvage.jsonl")
    print("review rows: %d" % len(reviews), flush=True)
    resolved, remaining = [], []
    for r in reviews:
        reason = r.get("review_reason") or ""
        status = r.get("status", "")
        method = r.get("method", "")
        if status in ("decode_failed", "unreadable", "api_error") or \
                method in ("encrypted", "unreadable", "dedup") or \
                KEEP_PAT.search(reason):
            r["resolve"] = "keep"
            remaining.append(r)
            continue
        blob = ("文件名：%s\n来源：%s\n首轮描述：%s" % (
            r.get("filename", ""), r.get("src", ""), reason[:600]))
        three_way = str(r.get("src_kind", "")).startswith(("K-", "J-"))
        prompt = THREE_WAY_PROMPT if three_way else PROMPT
        messages = [
            {"role": "system", "content": "只输出 JSON，不要解释。"},
            {"role": "user", "content": prompt + "\n" + blob},
        ]
        content, prov, err = scf.call_ark(llm, primary, fallback, messages)
        if err is not None:
            r["resolve"] = "keep"
            r["resolve_note"] = "second-pass API fail"
            remaining.append(r)
            print("APIERR %s" % r.get("filename", ""), flush=True)
            continue
        parsed = scf.parse_json(content)
        if three_way:
            out = None
            if parsed and parsed.get("dest") in ("work", "family", "media"):
                party = str(parsed.get("party", "")).strip()
                sub = str(parsed.get("subdir", "")).strip()
                ok = ((parsed["dest"] == "work" and party in ("1-王震", "2-上海润心", "3-北京供应链", "4-辽宁器械") and sub) or
                      (parsed["dest"] == "family" and party == "家庭文件" and sub) or
                      (parsed["dest"] == "media" and party == "媒体库" and sub))
                if ok:
                    out = {"party": party, "subdir": sub,
                           "reason": str(parsed.get("reason", ""))[:150]}
        else:
            out = scf.valid_out(parsed)
        if out is None or out.get("party") in (None, "", "无法判断"):
            r["resolve"] = "keep"
            r["resolve_note"] = "second-pass still undecided"
            remaining.append(r)
            print("KEEP %s" % r.get("filename", ""), flush=True)
            continue
        r2 = dict(r)
        r2.update({"party": out["party"], "subdir": out["subdir"],
                   "confidence": 0.85, "reason": out["reason"],
                   "method": "review-resolve-text",
                   "model": prov["model"], "provider": prov["name"],
                   "fallback": prov is fallback,
                   "decision": "move", "resolve": "resolved"})
        r2.pop("review_reason", None)
        resolved.append(r2)
        print("RESOLVED %s -> %s/%s" % (r.get("filename", "")[:50],
                                        out["party"], out["subdir"]), flush=True)
    with open("/data/side_review_resolved2.jsonl", "w", encoding="utf-8") as f:
        for r in resolved:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open("/data/side_review_remaining2.jsonl", "w", encoding="utf-8") as f:
        for r in remaining:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    print("SUMMARY " + json.dumps({
        "review_in": len(reviews), "resolved": len(resolved),
        "remaining": len(remaining),
        "resolved_by_party": dict(Counter(r["party"] for r in resolved)),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
