import sys, os, json, time, base64, io
sys.path.insert(0, "/app")
os.environ.setdefault("CURATOR_CONFIG_DIR", "/data/config")
try:
    import pillow_heif; pillow_heif.register_heif_opener()
except Exception:
    pass
from PIL import Image
from curator_core.llmkit import LLM

sample = json.load(open("/tmp/vlm_sample.json", encoding="utf-8"))
item = sample[0]
llm = LLM()
prov = [p for p in llm.providers if p["name"] == "model-3"][0]
print("provider: %s | model: %s | billing: %s" % (prov["name"], prov["model"], prov.get("billing")))
img = Image.open(item["src"]).convert("RGB")
if max(img.size) > 1024:
    img.thumbnail((1024, 1024), Image.LANCZOS)
buf = io.BytesIO(); img.save(buf, "JPEG", quality=80)
b64 = base64.b64encode(buf.getvalue()).decode()
messages = [
    {"role": "system", "content": "只输出 JSON，不要解释。"},
    {"role": "user", "content": [
        {"type": "text", "text": "你是照片归档专家。看这张照片，判断它属于下面哪个类目（只能选其一，原样输出类目名），并给出置信度（0-1）与一句话理由。\n可选类目：人物合影、自拍、儿童成长、旅行风景、城市与建筑、美食与咖啡、日常生活、宠物与动物、工作与活动、屏幕截图、文档与票据、梗图与表情\n\n文件名：%s\n\n只输出 JSON：{\"category\":\"类目名\",\"confidence\":0.9,\"reason\":\"一句话理由\"}" % os.path.basename(item["src"])},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
    ]},
]
t0 = time.time()
content, err = llm._call_ark(prov, messages, True, 60, 300)
print("latency=%.1fs err=%s" % (time.time() - t0, err))
print("content:", (content or "")[:400])
