#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""side-classify 试点 v3（仅邮件 40 封）：验证 v2 回归是否由人员/组织知识块修复。
复用 pilot 的 C 分层取样（同一 stratified 逻辑，同一 40 封），只改邮件 prompt，
加入人员→所属方映射。只读：不移动、不写库。输出 /data/side_classify_pilot3_mail.jsonl。
"""
import sys, os, json, time, re, argparse, email
from email import policy
from email.parser import BytesParser
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
from curator_core.llmkit import LLM

SRC_C = "/vol1/1000/收件箱/收件箱-邮件归档"
PARTIES = ["1-王震", "2-上海润心", "3-北京供应链", "4-辽宁器械", "无法判断"]
SUBDIRS = {
    "1-王震": ["00-整理说明", "09-会议与汇报", "10-周报与工作总结",
               "12-项目资料", "14-工具与其他"],
    "2-上海润心": ["01-公司资质与证照", "02-产品与注册", "03-合同与协议",
                  "04-供应商与采购", "05-销售与业务数据", "06-库存与物流",
                  "07-财务与经营分析", "08-运营与项目", "09-会议与汇报",
                  "10-周报与工作总结", "11-人事与行政", "12-招投标",
                  "12-项目资料", "13-图片与影像", "14-工具与其他"],
    "3-北京供应链": ["01-公司资质与证照", "02-产品与注册", "03-合同与协议",
                   "04-供应商与采购", "05-销售与业务数据", "06-库存与物流",
                   "07-财务与经营分析", "08-运营与项目", "09-会议与汇报",
                   "10-周报与工作总结", "11-人事与行政", "12-招投标",
                   "13-图片与影像", "14-工具与其他"],
    "4-辽宁器械": ["01-公司资质与证照", "02-产品与注册", "03-合同与协议",
                  "04-供应商与采购", "05-销售与业务数据", "06-库存与物流",
                  "07-财务与经营分析", "08-运营与项目", "09-会议与汇报",
                  "10-周报与工作总结", "11-人事与行政", "12-招投标",
                  "13-图片与影像", "14-工具与其他", "15-临床与学术资料"],
}

KNOWLEDGE = (
    "人员与组织线索（发件人/收件人/抄送人属于某一方已知人员、或内容涉及该方已知"
    "事业部/区域，即为明确指向该方；不要只认公司全名）：\n"
    "- 2-上海润心：王震（总经理，分管营销中心）；金祎、吴洁（供应链总监）、战轲、"
    "王雅琴（人事经理）、于航（浙江事业部负责人）、佟晓锐、郑丹凤（综合管理部）、"
    "张欢（财务部）、黄明辉（财务）、李阳希（上海业务组）、高熙渊（上海业务组）、"
    "王佳乐（上海业务组）、胥艳丽（物流部）、刘芳芳；事业部/区域：浙江事业部、"
    "上海业务组、温州仓、上海仓；业务：波科（BK）、微术、旭化成。\n"
    "- 4-辽宁器械：白天山（辽宁事业部负责人）、王子辰（吉林事业部负责人，辽吉区域）；"
    "辽宁业务组、辽宁分、辽吉区域；沈阳/抚顺/辽源/锦州/营口等辽宁区域医院；"
    "辽宁电力中心医院SPD、区域检验中心；公司名含“辽宁器械”“华润润心（辽宁）”"
    "“华润辽宁器械”（即使有“上海分公司”字样也属辽宁器械）。\n"
    "- 3-北京供应链：北京区域、北京供应链公司、供应链服务方案。\n"
    "- 1-王震：王震个人、党建、述职、个人证件、家庭。\n"
    "品牌名（爱德华、波科等）本身不决定归属，看协议主体/事业部/区域。\n"
    "多方内容以主体/发件人所属方为准；确实多方均等、无法定主体时才判无法判断。\n"
)

PARTY_DESC = (
    "1-王震：王震个人的文件——个人述职/工作总结/工作计划、党建材料、个人证件与人事档案、"
    "个人培训学习、个人周报、家庭/私人照片视频、非公司业务的个人邮件。\n"
    "2-上海润心：上海华润润心医疗器械有限公司（上海公司）的业务文件。\n"
    "3-北京供应链：北京供应链公司的业务文件。\n"
    "4-辽宁器械：华润润心（辽宁）医疗器械有限公司（含其上海分公司）的业务文件。\n"
    "无法判断：正文信息不足以确定属于哪一方，或内容与四方都无关。"
)


def parse_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def pick_providers(llm):
    ark = [p for p in llm.providers if p.get("kind") == "ark"]
    primary = next((p for p in ark if p.get("name") == "agent-plan"), None)
    if primary is None:
        primary = next((p for p in ark if p.get("billing") == "free"), None)
    fallback = next((p for p in ark if p.get("name") == "model-3"), None)
    if fallback is None:
        fallback = next((p for p in ark
                         if p.get("billing") == "payg" and p is not primary), None)
    if primary is None:
        raise RuntimeError("no ark provider available")
    return primary, fallback


def parse_eml(path):
    with open(path, "rb") as fh:
        m = BytesParser(policy=policy.default).parse(fh)
    atts = []
    body = ""
    if m.is_multipart():
        for p in m.walk():
            if p.get_filename():
                atts.append(p.get_filename())
            elif p.get_content_type() == "text/plain" and not body:
                try:
                    body = p.get_content() or ""
                except Exception:
                    body = ""
    else:
        if m.get_content_type() == "text/plain":
            try:
                body = m.get_content() or ""
            except Exception:
                body = ""
    body = re.sub(r"\s+", " ", body or "").strip()[:1500]
    return ("发件人：%s\n收件人：%s\n抄送：%s\n日期：%s\n主题：%s\n附件：%s\n正文：%s"
            % (m.get("From"), m.get("To"), m.get("Cc"), m.get("Date"),
               m.get("Subject"), "；".join(atts) if atts else "无", body))


def text_classify(llm, primary, fallback, text_blob):
    prompt = (
        "你是文件归档专家。根据下面这封邮件的发件人、收件人、主题与正文，判断它属于下面哪一方、"
        "进该方哪个子目录。只按内容所属方判断。\n"
        "判断规则（按顺序）：\n"
        "1) 正文/主题/发件人/收件人/抄送人明确指向某一方业务→定方（见下方人员与组织线索）。\n"
        "2) 系统通知/营销广告/个人服务类邮件（云服务、差旅酒店、银行营销、SaaS通知、"
        "会议系统通知、验证码等），收件人为王震本人且无公司业务归属信号→1-王震。\n"
        "3) 业务内容（招聘、人事、合同、销售、会议、系统）但看不出属于哪一方→无法判断。\n"
        f"{KNOWLEDGE}"
        f"可选方：\n{PARTY_DESC}\n"
        "子目录：必填，从该方子目录列表中选最贴近的（周报类→10-周报与工作总结；"
        "人事录用/入职/离职→11-人事与行政；财务付款/对账/发票→07-财务与经营分析；"
        "销售数据→05-销售与业务数据；合同协议→03-合同与协议；会议→09-会议与汇报；"
        "库存物流→06-库存与物流；资质证照→01-公司资质与证照；产品注册→02-产品与注册；"
        "招投标→12-招投标；1-王震的邮件→12-项目资料；实在没有贴近的→14-工具与其他）。\n"
        f"邮件：\n{text_blob}\n"
        '只输出 JSON：{"party":"方名（原样输出，如 2-上海润心 或 无法判断）",'
        '"subdir":"子目录名（原样输出，如 10-周报与工作总结；无法判断时为空）",'
        '"confidence":0.9,"reason":"一句话理由，说明关键信息（发件人/主题/正文）"}'
    )
    messages = [
        {"role": "system", "content": "只输出 JSON，不要解释。"},
        {"role": "user", "content": prompt},
    ]
    for prov in (primary, fallback):
        if prov is None:
            continue
        content, err = llm._call_ark(prov, messages, True, 60, 300)
        if err or not content:
            continue
        out = parse_json(content)
        if not isinstance(out, dict) or "party" not in out:
            continue
        party = str(out.get("party", "")).strip()
        if party not in PARTIES:
            continue
        try:
            conf = round(max(0.0, min(1.0, float(out.get("confidence", 0)))), 2)
        except Exception:
            conf = 0.0
        subdir = str(out.get("subdir", "")).strip()
        if party != "无法判断" and subdir not in SUBDIRS.get(party, []):
            subdir = ""
        return {"party": party, "subdir": subdir, "confidence": conf,
                "reason": str(out.get("reason", ""))[:150],
                "model": prov["model"], "provider": prov["name"],
                "fallback": prov is fallback}
    return None


def stratified(paths, n):
    if len(paths) <= n:
        return list(paths)
    step = len(paths) / n
    return [paths[int(i * step)] for i in range(n)]


def main():
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    llm = LLM()
    primary, fallback = pick_providers(llm)
    print("primary: %s | model: %s" % (primary["name"], primary["model"]), flush=True)
    c_all = []
    for root, _dirs, files in os.walk(SRC_C):
        for f in files:
            if f.lower().endswith(".eml"):
                c_all.append(os.path.join(root, f))
    c_all.sort()
    picks = stratified(c_all, 40)
    print("mail picks: %d / %d" % (len(picks), len(c_all)), flush=True)
    tls = threading.local()

    def thread_llm():
        o = getattr(tls, "llm", None)
        if o is None:
            o = LLM()
            tls.llm = o
        return o

    def one(path):
        t0 = time.time()
        try:
            blob = parse_eml(path)
        except Exception as e:
            return {"src": path, "filename": os.path.basename(path),
                    "status": "decode_failed", "error": str(e)[:150]}
        out = text_classify(thread_llm(), primary, fallback, blob)
        if out is None:
            return {"src": path, "filename": os.path.basename(path),
                    "status": "api_error", "error": "all providers failed"}
        out.update({"src": path, "filename": os.path.basename(path),
                    "status": "ok", "latency_s": round(time.time() - t0, 2)})
        return out

    results = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(one, p): p for p in picks}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            print("%s %s %s" % (r.get("status"), r.get("party", r.get("error", "")),
                                r.get("filename", ""))[:180], flush=True)
    with open("/data/side_classify_pilot3_mail.jsonl", "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    print("SUMMARY " + json.dumps({
        "n": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "by_party": dict(Counter(r.get("party") for r in results if r["status"] == "ok")),
        "ge85": sum(1 for r in results if r.get("confidence", 0) >= 0.85),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
