import json, sqlite3, collections, random, os, re

IMG_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".jfif"}
conn = sqlite3.connect("/data/nas-curator.db")
rows = conn.execute("SELECT id, plan_id, payload FROM review_queue WHERE status='pending' AND kind='low_conf'").fetchall()

def parse_ev(ev):
    ev = (ev or "").strip()
    m = re.match(r"^(.*?)\s+([\d.]+)\s*/\s*(.*?)\s+([\d.]+)\s*$", ev)
    if m:
        try: return (m.group(1), float(m.group(2)), m.group(3), float(m.group(4)))
        except ValueError: pass
    m2 = re.match(r"^(.*?)\s+([\d.]+)\s*$", ev)
    if m2:
        try: return (m2.group(1), float(m2.group(2)), "", None)
        except ValueError: pass
    return ("", None, "", None)

items = []
for r in rows:
    p = json.loads(r[2])
    src = p.get("src") or ""
    ext = os.path.splitext(src)[1].lower()
    if ext not in IMG_EXTS:
        continue
    top1, s1, top2, s2 = parse_ev(p.get("evidence"))
    if not top1 or s1 is None:
        continue
    if not os.path.exists(src):
        continue
    items.append({"rq_id": r[0], "plan_id": r[1], "src": src, "ext": ext,
                  "cat": p.get("category") or "", "conf": p.get("confidence"),
                  "reason": p.get("reason") or "", "sig_top": top1, "sig_score": s1,
                  "sig_2": top2, "sig_2score": s2})
print("eligible image+siglip+exists:", len(items))

random.seed(20261123)
by_cat = collections.defaultdict(lambda: collections.defaultdict(list))
for i in items:
    by_cat[i["sig_top"]][i["ext"]].append(i)
cats = sorted(by_cat, key=lambda c: -sum(len(v) for v in by_cat[c].values()))
print("cats:", [(c, sum(len(v) for v in by_cat[c].values())) for c in cats])
quota = {c: 5 if idx < 2 else 4 for idx, c in enumerate(cats)}
sample = []
for c in cats:
    q = quota[c]
    exts = sorted(by_cat[c], key=lambda e: -len(by_cat[c][e]))
    # 类内按扩展轮询取，保证 jpg/heic/webp/png 混合
    pools = {e: list(by_cat[c][e]) for e in exts}
    for e in pools:
        random.shuffle(pools[e])
    taken = 0
    ei = 0
    while taken < q:
        progressed = False
        for k in range(len(exts)):
            e = exts[(ei + k) % len(exts)]
            if pools[e] and taken < q:
                sample.append(pools[e].pop())
                taken += 1
                progressed = True
        ei += 1
        if not progressed:
            break
print("SAMPLE n=%d" % len(sample))
print("sample by cat:", collections.Counter(i["sig_top"] for i in sample).most_common())
print("sample by ext:", collections.Counter(i["ext"] for i in sample).most_common())
with open("/tmp/vlm_sample.json", "w", encoding="utf-8") as f:
    json.dump(sample, f, ensure_ascii=False, indent=1)
for i in sample:
    print("SAMPLE_ROW", i["rq_id"], i["ext"], repr(i["sig_top"]), i["sig_score"], repr(i["sig_2"]), i["sig_2score"], i["reason"][:50], i["src"])
