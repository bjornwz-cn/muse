#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""media_core.curate —— 媒体分类引擎（模块化版）。

来源：只读源 ``~/workspace/analysis/media-library-curator/scripts/curate.py``
（884 行，v1.0.0）逐函数迁移；判定逻辑与阈值一律未改，只做模块化拆分：

* 去掉 ``main()`` 与 ``if __name__ == '__main__'`` 直接执行部分——入口在
  :mod:`media_core.engine`（plan → apply 由统一 CLI 负责；本包不提供
  apply_plan，移动 mechanics 走文档引擎的 apply_plan）。
* ``do_move`` 保留为底层移动原语（apply 层可复用），plan 流程绝不调用它。
* ``write_report`` 改为纯函数：只返回 Markdown 文本，不落盘；
  报告文本的构建逻辑在 :func:`build_report`，``write_report`` 为同名保留。
* ``review_plan`` 改为纯规划：只返回 ``(items, summary)`` 数据，不写 TSV、
  不移动；``apply=True`` 会直接抛错（移动已移交统一 apply 流程）。
* 新增 :func:`video_frames`（多帧采样）与 :func:`decode_item_frames`
  （视频返回全部帧，供众数投票用）；:func:`decode_item` 签名与返回值不变，
  视频取首帧做 blur/白底等特征（与原单帧行为一致）。

import 顺序坑（cv2 必须先于 torch，见 :func:`ensure_cv2_first`）：
NAS 的 marvis runtime python 直接 ``import torch`` 会报
``libopenblasp-*.so 缺失``，因为 torch 依赖的 OpenBLAS 只在 opencv 的
``.libs`` 里；原技能能跑是因为 decode 流程先 import 了 cv2（其 .libs 被
加载进进程），之后 torch 才能 import。本模块把这个隐式依赖显式化：
任何可能触发 torch 加载的路径（即 :class:`Classifier.__init__`）都先调
:func:`ensure_cv2_first`；模块 import 本身绝不 import torch/cv2，
只在需要时延迟加载。
"""
from __future__ import annotations

import csv
import datetime
import glob
import hashlib
import io
import json
import os
import shutil
import subprocess
import time

try:
    from ..common.image_ext import DEFAULT_IMAGE_EXTS, get_image_exts
except Exception:  # noqa: BLE001 - 平铺导入时退化为绝对导入
    from common.image_ext import DEFAULT_IMAGE_EXTS, get_image_exts

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.environ.get('MEDIA_CURATOR_CONFIG') or os.path.join(SKILL_DIR, 'config.json')

# 模型全部走本地缓存，必须在任何 HF/timm 导入前关闭联网，否则会卡在重试
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['TIMM_OFFLINE'] = '1'

# HEIC/HEIF 解码：Pillow 原生不支持，需 pillow_heif 注册 opener
# 缺失时 HEIC 会全部落到 _Review/待确认（decode_failed）
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

# 0.1.5：图片扩展名改由配置（config/libraries.json image_extensions）统一管理，
# 文档引擎与媒体引擎共用同一份；缺省为两处旧硬编码列表的并集（16 个）。
# IMG_EXT / MEDIA_EXTS 保留为缺省快照（兼容旧导入）；判定逻辑一律调函数。
IMG_EXT = DEFAULT_IMAGE_EXTS
VID_EXT = {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.wmv', '.flv', '.3gp', '.mpg',
           '.mpeg', '.mts', '.m2ts', '.webm', '.insv', '.vob', '.rmvb'}

# 解不开的视频黑名单（2026-11-27 提速改动 3）：RealVideo 容器 .rm/.rmvb/.ram。
# 容器内 ffmpeg 的 rv40 解码器会在 "B-frame without reference data" 上报错
# 刷屏、cv2 抽帧空转烧 CPU，且本项目无 RealVideo 解码需求——一律不尝试
# 解码，直接记无法验证、留复核。注意：VID_EXT 里目前只有 .rmvb 是媒体
# 扩展名（.rm/.ram 不进媒体收集，见 is_media）；这里三者都列是防御性的，
# 万一扩展名集合日后扩进来，解码路径仍直接跳过。
UNDECODABLE_VIDEO_EXTS = frozenset({".rm", ".rmvb", ".ram"})
# 照片 + 视频扩展名全集：与 is_media() 的判定完全一致；统一 CLI 用它收集媒体路径。
MEDIA_EXTS = frozenset(IMG_EXT | VID_EXT)


def media_exts():
    """照片 + 视频扩展名全集（随图片配置动态变化）。"""
    return frozenset(get_image_exts() | VID_EXT)
PROXY_EXT = {'.lrv'}                      # 视频代理文件，不纳入
SKIP_NAMES = {'.DS_Store', 'Thumbs.db', 'desktop.ini', '.directory', '@eaDir', '#recycle'}


def ensure_cv2_first():
    """在任何可能 import torch 的代码之前，先 import cv2。

    原因（NAS marvis runtime 实测）：torch 依赖的 OpenBLAS（libopenblasp）
    只在 opencv 的 .libs 目录里；直接 ``import torch`` 会报
    ``libopenblasp-*.so: cannot open shared object file``。先 ``import cv2``
    会把 opencv 的 .libs 加载进进程，之后 torch 才能正常 import。
    原 curate.py 是靠 decode 流程先 import cv2 偶然满足了这个顺序；
    这里显式化：:class:`Classifier` 实例化前必须先调本函数。

    补充（2026-11-19 NAS 实测）：只 ``import cv2`` 有时仍失败——cv2 自身
    也要 libopenblasp，而 .libs 不在默认 so 搜索路径里（须
    ``LD_LIBRARY_PATH=<site-packages>/opencv_python_headless.libs`` 才行）。
    本函数在 ImportError 时改用 ctypes 按显式路径预加载 libopenblasp
    （os.environ 的 LD_LIBRARY_PATH 对已启动进程无效，改了也没用），
    再重试 ``import cv2``；仍失败才抛 RuntimeError（附原始错误与环境变量提示）。
    返回 True 表示 cv2 已就绪。
    """
    try:
        import cv2  # noqa: F401
        return True
    except ImportError as first_err:
        try:
            import ctypes
            import glob
            import site
            import sysconfig
            cands = []
            for base in {*(site.getsitepackages() or []),
                         sysconfig.get_path("purelib")}:
                for pat in (os.path.join(base, "opencv_python_headless.libs",
                                        "libopenblasp*.so"),
                            os.path.join(base, "opencv_python.libs",
                                        "libopenblasp*.so"),
                            os.path.join(base, "opencv_contrib_python.libs",
                                        "libopenblasp*.so")):
                    cands.extend(sorted(glob.glob(pat)))
            for lib in cands:
                try:
                    ctypes.CDLL(lib)
                except OSError:
                    continue
                break
            import cv2  # noqa: F401
            return True
        except ImportError as e:
            raise RuntimeError(
                '加载视觉模型前需要先 import cv2：本机/NAS 的 torch 依赖 OpenBLAS，'
                '其 libopenblasp-*.so 只在 opencv 的 .libs 里，直接 import torch 会报 '
                'libopenblasp 缺失。先 import cv2（加载其 .libs 进进程）再 import torch。'
                '若 cv2 本身也报 libopenblasp 缺失，请以 '
                'LD_LIBRARY_PATH=<site-packages>/opencv_python_headless.libs '
                '启动进程，或确认 .libs 目录存在。'
                '原始错误：%s（首次：%s）' % (e, first_err)) from e


def _resolve_hf_snapshot(cache_dir, repo_id):
    """解析 HF 缓存中某仓库的本地快照目录（refs/main 优先，否则取最新快照）。

    用于缓存缺个别文件（如 config.json）且离线无法下载时，从本地快照
    目录直接加载分词器/模型，不走 repo id。返回目录路径或 None。
    """
    repo_dir = os.path.join(cache_dir, "models--" + repo_id.replace("/", "--"))
    snaps_dir = os.path.join(repo_dir, "snapshots")
    if not os.path.isdir(snaps_dir):
        return None
    ref = os.path.join(repo_dir, "refs", "main")
    if os.path.isfile(ref):
        try:
            h = open(ref, encoding="utf-8").read().strip()
            p = os.path.join(snaps_dir, h)
            if os.path.isdir(p):
                return p
        except OSError:
            pass
    cands = sorted((d for d in glob.glob(os.path.join(snaps_dir, "*"))
                    if os.path.isdir(d)),
                   key=lambda d: os.path.getmtime(d), reverse=True)
    return cands[0] if cands else None


# ---------------- 基础 ----------------

def load_cfg():
    with open(CFG_PATH, encoding='utf-8') as f:
        return json.load(f)


def now_ts():
    return datetime.datetime.now().strftime('%Y%m%d_%H%M%S')


def sudo_run(args, timeout=900):
    """以 sudo -n 执行命令，返回 CompletedProcess。"""
    return subprocess.run(['sudo', '-n'] + args, capture_output=True, text=True,
                          timeout=timeout, errors='replace')


def is_media(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in PROXY_EXT:
        return None
    if ext in get_image_exts():
        return '照片'
    if ext in VID_EXT:
        return '视频'
    return None


def scan_inbox(cfg):
    """递归扫描投递目录，返回 (候选列表, 跳过统计)。"""
    inbox = cfg['inbox']
    rows, skipped = [], {}
    for root, dirs, files in os.walk(inbox):
        dirs[:] = [d for d in dirs
                   if not d.startswith('.') and d not in SKIP_NAMES
                   and d != 'node_modules']
        for fn in files:
            if fn.startswith('.') or fn in SKIP_NAMES or fn.startswith('._'):
                skipped['隐藏/系统文件'] = skipped.get('隐藏/系统文件', 0) + 1
                continue
            if fn.lower().endswith('.d.mts'):
                # P2(P1.1)：.mts 与 AVCHD 视频扩展名碰撞，TypeScript 声明文件不是媒体
                skipped['TypeScript声明(.d.mts)'] = skipped.get('TypeScript声明(.d.mts)', 0) + 1
                continue
            p = os.path.join(root, fn)
            kind = is_media(fn)
            if not kind:
                key = '视频代理(.lrv)' if os.path.splitext(fn)[1].lower() in PROXY_EXT else '非媒体文件'
                skipped[key] = skipped.get(key, 0) + 1
                continue
            try:
                st = os.stat(p)
            except OSError:
                skipped['无法访问'] = skipped.get('无法访问', 0) + 1
                continue
            rows.append({'path': p, 'name': fn, 'kind': kind,
                         'size': st.st_size, 'mtime': st.st_mtime,
                         'ext': os.path.splitext(fn)[1].lower()})
    rows.sort(key=lambda r: r['path'])
    return rows, skipped


def stable_filter(rows, cfg, state):
    """跳过仍在写入/刚放入的文件，保留稳定可处理的。"""
    settle = float(cfg.get('settle_minutes', 3)) * 60
    now = time.time()
    ready, waiting = [], []
    seen = state.get('seen', {})
    for r in rows:
        age = now - r['mtime']
        prev = seen.get(r['path'])
        if age < settle:
            waiting.append({**r, 'reason': '刚写入，等待稳定'})
        elif prev is not None and prev != r['size']:
            waiting.append({**r, 'reason': '大小仍在变化'})
        else:
            ready.append(r)
        seen[r['path']] = r['size']
    # 清理已消失文件记录
    alive = {r['path'] for r in rows}
    state['seen'] = {k: v for k, v in seen.items() if k in alive}
    return ready, waiting


def sha256_of(path, timeout=1200):
    try:
        p = sudo_run(['sha256sum', '--', path], timeout=timeout)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.split()[0]
    except Exception:
        pass
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return 'ERR'


def _can_open(path):
    """os.access 在 fnOS 上会被内核授权层误导，实际以能否 open 为准。"""
    try:
        with open(path, 'rb') as f:
            f.read(1)
        return True
    except Exception:
        return False


def ensure_readable(path):
    """文件对当前用户不可读时，用 sudo 复制到本地临时目录再读。"""
    if _can_open(path):
        return path, None
    tmpdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.tmpread')
    os.makedirs(tmpdir, exist_ok=True)
    dst = os.path.join(tmpdir, os.path.basename(path))
    p = sudo_run(['cp', '-f', '--', path, dst])
    if p.returncode != 0:
        return None, 'copy_failed'
    return dst, dst


def dhash_int(img):
    """64 位 dHash：9x8 灰度相邻比较。"""
    import numpy as np
    g = img.convert('L').resize((9, 8))
    a = np.asarray(g, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def hamming(a, b):
    return bin(a ^ b).count('1')


# ---------------- 索引 ----------------

class Index:
    def __init__(self, cfg):
        self.dir = os.path.join(cfg['work_dir'], 'index')
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, 'hash_index.tsv')
        self.by_sha = {}
        self.dhash_list = []
        self.count = 0
        if os.path.exists(self.path):
            with open(self.path, encoding='utf-8', errors='replace') as f:
                for line in f:
                    p = line.rstrip('\n').split('\t')
                    if len(p) < 3:
                        continue
                    sha, _size, path = p[0], p[1], p[2]
                    dh = p[3] if len(p) > 3 else ''
                    self.by_sha[sha] = path
                    self.count += 1
                    if dh.isdigit():
                        self.dhash_list.append((int(dh), path))

    def dup_of(self, sha):
        return self.by_sha.get(sha)

    def near_dup_of(self, dh, maxd):
        best = None
        for v, p in self.dhash_list:
            d = hamming(dh, v)
            if d <= maxd and (best is None or d < best[0]):
                best = (d, p)
        return best

    def add(self, sha, size, path, dh):
        self.by_sha[sha] = path
        if dh is not None:
            self.dhash_list.append((dh, path))
        self.count += 1
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write('%s\t%d\t%s\t%s\t%s\n' % (sha, size, path,
                                              dh if dh is not None else '',
                                              int(time.time())))


def rebuild_index(cfg, idx):
    """全量重建哈希索引（媒体库现有文件）。"""
    lib = cfg['library_root']
    out = os.path.join(idx.dir, 'hash_index.tsv')
    tmp = out + '.building'
    n = 0
    with open(tmp, 'w', encoding='utf-8') as fo:
        for root, dirs, files in os.walk(lib):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in SKIP_NAMES
                       and d not in ('_收件箱',)]
            for fn in files:
                if fn.startswith('.'):
                    continue
                if not is_media(fn):
                    continue
                p = os.path.join(root, fn)
                sha = sha256_of(p)
                if sha.startswith('ERR'):
                    continue
                dh = ''
                kind = is_media(fn)
                if kind == '照片':
                    src, tmpc = ensure_readable(p)
                    if src:
                        try:
                            from PIL import Image
                            dh = str(dhash_int(Image.open(src).convert('RGB')))
                        except Exception:
                            dh = ''
                        if tmpc:
                            os.unlink(tmpc)
                fo.write('%s\t%d\t%s\t%s\t%d\n' % (sha, os.path.getsize(p), p, dh, int(time.time())))
                n += 1
                if n % 2000 == 0:
                    print('indexed %d' % n, flush=True)
    os.replace(tmp, out)
    return n


# ---------------- 视觉分类 ----------------

PHOTO_PROMPTS_EN = {
    '人物合影': ['a group photo of several people posing together',
             'family members standing together for a group picture',
             'wedding guests or colleagues posing in a group',
             'people posing in front of a landmark or scenery'],
    '自拍': ['a selfie of one person with face close to the camera',
           'person holding a phone taking a selfie',
           'a portrait selfie of one person smiling at the camera',
           'mirror selfie of one person'],
    '儿童成长': ['a photo of a baby or toddler', 'young children playing',
             'a kid playing on a playground', 'a child in a kindergarten or at a birthday party'],
    '旅行风景': ['a scenic travel landscape with mountains lake or sea',
             'tourists sightseeing at a famous spot',
             'beach ocean and blue sky travel photo',
             'snow mountain desert or forest landscape'],
    '城市与建筑': ['city street with tall buildings', 'exterior of a building or architecture',
              'night city skyline with lights', 'a bridge or landmark building'],
    '美食与咖啡': ['food dishes on a table in a restaurant', 'a cake or dessert',
              'a cup of coffee on a table', 'a bouquet of flowers'],
    '日常生活': ['everyday life scene indoors at home', 'person cooking or doing housework',
             'living room or bedroom interior', 'a room or home object close-up'],
    '宠物与动物': ['a pet dog or cat', 'animals in a zoo or park',
              'a bird on the ground or branch', 'a wild animal'],
    '工作与活动': ['a business meeting in a conference room',
              'a corporate event with stage and audience',
              'a factory workshop or construction site', 'people working in an office'],
    '屏幕截图': ['a screenshot of a phone app interface with text and buttons',
             'a screenshot of a computer screen with windows',
             'an online shopping product page screenshot', 'a chat conversation screenshot'],
    '文档与票据': ['a scanned paper document', 'a receipt invoice or bill',
              'handwritten notes on paper', 'a printed form or contract with text'],
    '梗图与表情': ['an internet meme image with cartoon character',
              'an emoji sticker or smiley face', 'a funny cartoon drawing',
              'a clipart illustration'],
}
PHOTO_PROMPTS_ZH = {
    '人物合影': ['多个人一起合影', '一家人站在一起拍合照', '一群人集体合影留念', '在景点前的多人合照'],
    '自拍': ['一个人的自拍照', '手机前置摄像头自拍', '一个人对着镜头微笑的近距离自拍', '单人肖像近距离自拍'],
    '儿童成长': ['婴儿或幼儿的照片', '小孩子在玩耍', '儿童在游乐场或幼儿园', '小孩过生日'],
    '旅行风景': ['旅行风景照片，有山或湖或海', '游客在景点观光', '海滩蓝天风景照', '雪山草原森林风景'],
    '城市与建筑': ['城市街道和高楼大厦', '建筑物外观和建筑摄影', '夜晚城市灯光夜景', '桥或地标建筑'],
    '美食与咖啡': ['餐厅里的一桌菜', '蛋糕甜点', '桌上的一杯咖啡', '一束鲜花'],
    '日常生活': ['家里的日常生活场景', '有人在家做饭做家务', '客厅或卧室室内环境', '家里的物品特写'],
    '宠物与动物': ['宠物狗或宠物猫', '动物园或公园里的动物', '一只鸟', '野生动物'],
    '工作与活动': ['会议室里的商务会议', '有舞台和观众的公司活动', '工厂车间或施工现场', '办公室工作场景'],
    '屏幕截图': ['手机App界面截图', '电脑屏幕截图', '电商商品详情页截图', '聊天记录截图'],
    '文档与票据': ['扫描的纸质文件', '发票收据账单', '纸上的手写笔记', '带文字和印章的打印表格合同'],
    '梗图与表情': ['网络梗图', '表情包贴纸', '搞笑卡通画', '卡通插画'],
}
VIDEO_PROMPTS_EN = {
    '儿童成长': ['a video of a baby or young child', 'kids playing', 'a child walking outdoors'],
    '旅行风景': ['a travel video of scenery landscape', 'tourists sightseeing a famous place',
             'beach or mountain view'],
    '日常生活': ['an everyday life vlog scene indoors', 'people eating or chatting at home',
             'a casual home video'],
    '聚会与夜生活': ['a party with people drinking and dancing', 'a wedding banquet celebration',
              'a night street with crowds and lights', 'a concert with stage lights'],
    '运动与活动': ['people playing sports or exercising', 'a sports game on a field',
              'running skating or skiing'],
    '屏幕录制': ['a screen recording of a phone or computer', 'a video game gameplay recording',
             'a video call or face filter animation', 'a presentation screen recording'],
}
VIDEO_PROMPTS_ZH = {
    '儿童成长': ['小孩玩耍的视频', '婴儿或幼儿的视频', '儿童在户外活动'],
    '旅行风景': ['旅行风景视频', '游客在景点观光', '海滩或山景视频'],
    '日常生活': ['家庭日常生活视频', '有人在家吃饭聊天', '随手拍的家庭视频'],
    '聚会与夜生活': ['聚会上有人喝酒跳舞', '婚宴聚餐庆祝', '夜晚街道人群和灯光', '演出现场舞台灯光'],
    '运动与活动': ['人们运动健身', '体育比赛现场', '跑步滑冰滑雪'],
    '屏幕录制': ['手机或电脑的屏幕录制', '游戏画面录制', '视频通话或动态特效', '演示文稿录屏'],
}

# 照片 12 类 / 视频 6 类（与原提示词表 key 完全一致）。
PHOTO_CATEGORIES = tuple(PHOTO_PROMPTS_EN.keys())
VIDEO_CATEGORIES = tuple(VIDEO_PROMPTS_EN.keys())


def _load_cv2():
    """延迟加载 cv2（模块 import 时绝不加载 cv2/torch）；失败返回 None。

    0.1.8：视频抽帧改走 cv2.VideoCapture，不再调 ffmpeg 子进程。
    cv2 缺失时视频抽帧返回空（调用方按 decode_failed 处理，与旧版
    ffmpeg 缺失时的行为一致）；照片的 _image_features 同样要求 cv2，
    缺失时亦走 decode_failed（见测试 test_decode_item_returns_numbers）。
    """
    try:
        import cv2
        return cv2
    except ImportError:
        pass
    try:
        ensure_cv2_first()
        import cv2
        return cv2
    except Exception:
        return None


def _cap_frame(cv2, cap, ss):
    """从已打开的 VideoCapture 取 ss 秒处一帧（PIL RGB 图）；抽不到返回 None。

    cv2 读到的是 BGR 帧，转 RGB 后再包 PIL，与旧 ffmpeg 管道的 mjpeg 解码
    结果一致（下游 _image_features / classifier.scores 均按 RGB 处理）。
    seek 到关键帧后向前解码；超片长的时间点读不到，直接跳过（不回退到
    0 秒，避免首帧被重复计票）。
    """
    from PIL import Image
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, float(ss) * 1000.0)
    except Exception:
        return None
    try:
        ok, frame = cap.read()
    except Exception:
        return None
    if not ok or frame is None or frame.size == 0:
        return None
    try:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    except Exception:
        return None
    return Image.fromarray(rgb)


def video_frame(path, ss='2'):
    """原单帧采样：取 ss 秒处一帧（PIL RGB 图）；失败时回退到 ss='0'，仍失败返回 None。

    0.1.8：改走 cv2.VideoCapture（不再调 ffmpeg 子进程）；回退语义不变。
    """
    img = _video_frame_at(path, ss)
    if img is not None:
        return img
    if str(ss) != '0':
        return video_frame(path, '0')
    return None


def _video_frame_at(path, ss):
    """单次抽帧（无回退）：取 ss 秒处一帧；抽不到/超片长返回 None。

    0.1.8：改走 cv2.VideoCapture（不再调 ffmpeg 子进程）。
    供 video_frames() 使用——多帧采样里某时间点抽不到只跳过该点，
    不应像 video_frame() 那样回退到 0 秒（否则首帧会被重复计票）。
    cv2 缺失或打不开文件时返回 None。
    """
    cv2 = _load_cv2()
    if cv2 is None:
        return None
    try:
        cap = cv2.VideoCapture(path)
    except Exception:
        return None
    if not cap.isOpened():
        return None
    try:
        return _cap_frame(cv2, cap, ss)
    finally:
        cap.release()


def video_frames(path, max_frames=6, step=10):
    """视频多帧采样（v2，0.1.8）：首帧 + 每 step 秒一帧，上限 max_frames 帧。

    时间点为 0, step, 2*step, …（默认 0/10/20/30/40/50 秒，最多 6 帧）；
    超过片长的时间点抽不到，直接跳过——短视频自然只返回 1–2 帧。
    返回 PIL RGB 图列表（可能为空列表：全部抽帧失败时由调用方按
    decode_failed 处理）。每帧走同一套图片分类，众数投票在 engine 层做。
    0.1.8 改动：同一 VideoCapture 只开一次、逐点 seek（不再每点拉起一次
    ffmpeg 子进程）；cv2 缺失或打不开文件时返回 []。
    2026-11-27 提速改动 3：RealVideo 黑名单扩展名直接返回 []，不拉起
    VideoCapture（rv40 解码报错刷屏、抽帧空转烧 CPU）。
    """
    if os.path.splitext(path)[1].lower() in UNDECODABLE_VIDEO_EXTS:
        return []
    cv2 = _load_cv2()
    if cv2 is None:
        return []
    try:
        cap = cv2.VideoCapture(path)
    except Exception:
        return []
    if not cap.isOpened():
        return []
    frames = []
    try:
        for i in range(max(0, int(max_frames))):
            img = _cap_frame(cv2, cap, i * int(step))
            if img is not None:
                frames.append(img)
    finally:
        cap.release()
    return frames


def _decode_livp_frames(path):
    """Live Photo（.livp = zip，内含 .jpg 静帧 + .mov 视频）解包取帧。

    优先用其中的 .mov 走 cv2 抽帧（与视频同一采样）；.mov 不可用则用
    .jpg 静帧当一帧（.heic 静帧需 pillow_heif，已在模块 import 时注册，
    无则打不开、跳过）。解包进临时目录，用后删除。全部不可用返回 []，
    由调用方按 decode_failed 处理。
    0.1.8 新增：旧版把 .livp 当照片直接 PIL 打开，zip 包必然报
    UnidentifiedImageError，全数落到待确认（计划 #107 计 420 条）。
    """
    import tempfile
    import zipfile
    from PIL import Image
    try:
        zf = zipfile.ZipFile(path)
    except Exception:
        return []
    tmpd = tempfile.mkdtemp(prefix='livp-')
    try:
        try:
            names = zf.namelist()
        except Exception:
            return []
        movs = [n for n in names if n.lower().endswith(('.mov', '.mp4'))]
        jpgs = [n for n in names
                if n.lower().endswith(('.jpg', '.jpeg', '.png', '.heic', '.heif'))]
        for name in movs:
            try:
                target = os.path.join(tmpd, os.path.basename(name))
                with zf.open(zf.getinfo(name), 'r') as src, \
                        open(target, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                frames = video_frames(target)
                if frames:
                    return frames
            except Exception:
                continue
        for name in jpgs:
            try:
                target = os.path.join(tmpd, os.path.basename(name))
                with zf.open(zf.getinfo(name), 'r') as src, \
                        open(target, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                im = Image.open(target)
                return [im.convert('RGB')]
            except Exception:
                continue
        return []
    finally:
        try:
            zf.close()
        except Exception:
            pass
        shutil.rmtree(tmpd, ignore_errors=True)


def _image_features(img):
    """单张图的数值特征：(blur, 白底占比, 色彩数, 长宽比)。

    与原 decode_item 内联计算完全一致（320 宽缩放 → Laplacian 方差为 blur；
    RGB 各通道 >235 的像素占比为白底；//32 量化后去重色数；原图长宽比）。
    需要 numpy + cv2；缺失时抛异常，由调用方按解码失败处理。
    """
    import numpy as np
    import cv2
    w, h = img.size
    small = img.resize((320, max(1, int(320 * h / w))))
    arr = np.asarray(small)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    white = float((arr.min(2) > 235).mean())
    colors = int(len(np.unique((arr // 32).reshape(-1, 3), axis=0)))
    ar = float(w) / float(h)
    return blur, white, colors, ar


def decode_item_frames(row):
    """解码 + 基础图像特征；返回 (帧列表, blur, white, 色彩数, 长宽比, 备注)。

    照片：帧列表为 [图]；视频：帧列表为 video_frames() 的多帧采样。
    blur/white/colors/ar 一律取首帧（照片即原图，视频即首帧），与原
    decode_item 的单帧特征口径一致。失败时帧列表为空，备注为异常类名
    （如 'noframe' / 'ValueError' / 'ModuleNotFoundError'）。
    """
    from PIL import Image
    frames, note, blur, white, colors, ar = [], '', 0.0, 0.0, 999, 1.0
    tmpc = None
    # 2026-11-27 提速改动 3：RealVideo 黑名单扩展名不尝试解码，直接记无法
    # 验证（note 带扩展名，调用方按 decode_failed 进复核）；后续轮次靠
    # 复核行去重（改动 2）不再重试，不在此处缓存。
    if (row.get('ext') or '').lower() in UNDECODABLE_VIDEO_EXTS:
        return [], 0.0, 0.0, 999, 1.0, "unsupported_codec:%s" % row.get('ext')
    try:
        src, tmpc = ensure_readable(row['path'])
        if not src:
            raise ValueError('unreadable')
        if row['ext'] == '.livp':
            # Live Photo：zip 包，PIL 打不开；解包后取 .mov 抽帧或 .jpg 静帧。
            # 0.1.8 新增（旧版全数报 UnidentifiedImageError，计划 #107 计 420 条）。
            frames = _decode_livp_frames(src)
            if not frames:
                raise ValueError('noframe')
        elif row['kind'] == '照片':
            im = Image.open(src)
            if row['ext'] in ('.jpg', '.jpeg', '.jfif'):
                try:
                    im.draft('RGB', (448, 448))
                except Exception:
                    pass
            frames = [im.convert('RGB')]
        else:
            frames = video_frames(row['path'])
            if not frames:
                raise ValueError('noframe')
        w, h = frames[0].size
        if w < 8 or h < 8:
            raise ValueError('tiny')
        blur, white, colors, ar = _image_features(frames[0])
    except Exception as e:
        note = type(e).__name__
        frames = []
    finally:
        if tmpc and os.path.exists(tmpc):
            try:
                os.unlink(tmpc)
            except OSError:
                pass
    return frames, blur, white, colors, ar, note


def decode_item(row):
    """解码 + 基础图像特征；返回 (PIL图, blur, 白底占比, 色彩数, 长宽比, 备注)。

    与原 curate.py 同签名同语义：视频取首帧（多帧采样见 decode_item_frames）。
    解码失败时图为 None、备注为异常类名，blur/white 等保持数值默认值。
    """
    frames, blur, white, colors, ar, note = decode_item_frames(row)
    img = frames[0] if frames else None
    return img, blur, white, colors, ar, note


class Classifier:
    """SigLIP(英文/场景) + ChineseCLIP(中文/界面文档) 双模型集成。

    实例化时才加载模型（模块 import 不加载）。实例化前先调
    ensure_cv2_first()：cv2 的 .libs 进进程后 torch 才能 import
    （OpenBLAS/libopenblasp 只在 opencv .libs 里，见模块 docstring）。
    """

    def __init__(self, cfg):
        ensure_cv2_first()
        import torch
        import open_clip
        from transformers import ChineseCLIPModel, ChineseCLIPProcessor
        self.torch = torch
        torch.set_num_threads(max(4, min(16, os.cpu_count() or 8)))
        md = cfg['model_dir']
        self.sm, _, self.spp = open_clip.create_model_and_transforms(
            'ViT-B-16-SigLIP', pretrained='webli', cache_dir=md, device='cpu')
        self.sm.eval()
        try:
            self.stok = open_clip.get_tokenizer('ViT-B-16-SigLIP')
        except OSError:
            # NAS 实测（2026-11-19）：缓存缺 config.json 且 huggingface.co 不可达，
            # AutoTokenizer 按 repo id 加载失败；改从本地快照目录直接建同款
            # HFTokenizer（同一 tokenizer.json，行为一致）。原版无此兜底，
            # 同环境下原版同样失败（已用原版 Classifier 实测确认）。
            snap = _resolve_hf_snapshot(md, 'timm/ViT-B-16-SigLIP')
            if snap is None:
                raise
            from open_clip.tokenizer import HFTokenizer
            # SigLIP 文本编码器上下文长度为 64（非 CLIP 的 77）；原版
            # get_tokenizer 按模型配置取 64，直接用默认 77 会在推理时报
            # positional_embedding 尺寸错（77 vs 64）。
            ctx = int(getattr(self.sm, 'context_length', 64) or 64)
            self.stok = HFTokenizer(snap, context_length=ctx)
        self.sscale = float(self.sm.logit_scale.detach().exp())
        self.cm = ChineseCLIPModel.from_pretrained('OFA-Sys/chinese-clip-vit-base-patch16',
                                                   cache_dir=md)
        self.cm.eval()
        self.cpp = ChineseCLIPProcessor.from_pretrained(
            'OFA-Sys/chinese-clip-vit-base-patch16', cache_dir=md)
        self.cache = {}
        for kind, pen, pzh in (('照片', PHOTO_PROMPTS_EN, PHOTO_PROMPTS_ZH),
                               ('视频', VIDEO_PROMPTS_EN, VIDEO_PROMPTS_ZH)):
            self.cache[kind] = (self._sig_feats(pen), self._zh_feats(pzh))

    def _sig_feats(self, prompts):
        texts, owner = [], []
        for cat, ps in prompts.items():
            for p in ps:
                texts.append(p)
                owner.append(cat)
        with self.torch.no_grad():
            tf = self.sm.encode_text(self.stok(texts))
            tf /= tf.norm(dim=-1, keepdim=True)
        return tf, owner

    def _zh_feats(self, prompts):
        texts, owner = [], []
        for cat, ps in prompts.items():
            for p in ps:
                texts.append(p)
                owner.append(cat)
        return texts, owner

    def scores(self, pil, kind):
        torch = self.torch
        (stf, sop), (ztx, zop) = self.cache[kind]
        sx = self.spp(pil).unsqueeze(0)
        with torch.no_grad():
            sf = self.sm.encode_image(sx)
            sf /= sf.norm(dim=-1, keepdim=True)
            sp = ((sf @ stf.T) * self.sscale).softmax(-1)[0]
            zin = self.cpp(text=ztx, images=[pil], return_tensors='pt', padding=True)
            zp = self.cm(**zin).logits_per_image.softmax(-1)[0]
        cats = sorted(set(sop))
        scs, scz = {}, {}
        for c in cats:
            idxs = [k for k, o in enumerate(sop) if o == c]
            scs[c] = float(max(sp[k] for k in idxs))
            scz[c] = float(max(zp[k] for k in idxs))
        ts = sorted(scs.items(), key=lambda kv: -kv[1])
        return {'sig': scs, 'zh': scz, 'sig_top': ts[0], 'sig_2': ts[1]}


# 0.2.6 P1：截图类扩展名（UI 图标/截图天然低纹理，不做模糊判定）与
# 小图门控阈值（长边 <100px 的图不做模糊判定）。
SCREENSHOT_EXTS = {".png", ".gif", ".bmp", ".webp"}
BLUR_MIN_EDGE = 100


def is_blur_check_applicable(ext, long_edge=None):
    """0.2.6 P1：该文件是否要做模糊判定。

    截图类扩展名（png/gif/bmp/webp）跳过——UI 图标/截图天然低纹理，
    Laplacian 方差趋近 0，与“拍糊”无法区分（抽检 30 张 blur<5 全是
    UI 素材，0 张真糊）。小图（长边 <100px）也跳过——16×16 图标放大到
    320px 再算 Laplacian，方差注定趋近 0。long_edge 为 None 时不做小图门控。
    """
    if (ext or "").lower() in SCREENSHOT_EXTS:
        return False
    if long_edge is not None and long_edge < BLUR_MIN_EDGE:
        return False
    return True


def decide(row, cls_scores, blur, white, colors, ar, note, cfg, long_edge=None):
    """判定（原逻辑逐字保留）：返回 (cat, cat2, s1, s2, why）。

    why 取值：'accepted' / 'decode_failed' / 'blurry' / 'low_confidence'。
    顺序：解码失败 → 待确认；照片 blur < blur_min → 模糊；
    margin < margin_min 或 top1 < score_min → 待确认；否则接受。
    白底规则优先：白底 ≥0.45 且截图/文档分高时判截图/文档（见三条分支）。
    0.2.6 P1：模糊判定跳过截图类扩展名与小图（见 is_blur_check_applicable）；
    照片阈值 blur_min 由 50 下调到 20（media_core/config.json 与
    _DEFAULT_THRESHOLDS 同步）。long_edge 为原图长边（像素），None 时不做小图门控。
    """
    th = cfg['thresholds']
    cat, cat2, s1, s2 = '待确认', '待确认', 0.0, 0.0
    if cls_scores:
        zs, ss = cls_scores['zh'], cls_scores['sig']
        z_sc, z_doc = zs.get('屏幕截图', 0), zs.get('文档与票据', 0)
        s_sc, s_doc = ss.get('屏幕截图', 0), ss.get('文档与票据', 0)
        if white >= 0.45 and max(z_sc, s_sc) >= 0.30:
            cat, s1, s2, cat2 = '屏幕截图', max(z_sc, s_sc), max(z_doc, s_doc), '文档与票据'
        elif white >= 0.45 and max(z_doc, s_doc) >= 0.25:
            cat, s1, s2, cat2 = '文档与票据', max(z_doc, s_doc), max(z_sc, s_sc), '屏幕截图'
        elif white >= 0.60 and colors <= 80 and ar < 0.85 and max(z_doc, s_doc) >= 0.15:
            cat, s1, s2, cat2 = '文档与票据', max(max(z_doc, s_doc), 0.60), 0.0, '屏幕截图'
        else:
            cat, s1 = cls_scores['sig_top']
            cat2, s2 = cls_scores['sig_2'][0], cls_scores['sig_2'][1]
    margin = s1 - s2
    if note:
        return '待确认', '待确认', s1, s2, 'decode_failed'
    if row['kind'] == '照片' and is_blur_check_applicable(row.get('ext'), long_edge) \
            and blur < th['blur_min']:
        return '模糊', cat, s1, s2, 'blurry'
    if margin < th['margin_min'] or s1 < th['score_min']:
        return '待确认', cat, s1, s2, 'low_confidence'
    return cat, cat2, s1, s2, 'accepted'


# ---------------- 移动与报告 ----------------

def target_dir(cfg, kind, category):
    if category in cfg['review_dirs']:
        return cfg['review_dirs'][category]
    table = cfg['photo_dirs'] if kind == '照片' else cfg['video_dirs']
    return table.get(category) or cfg['review_dirs']['待确认']


def unique_dst(dst):
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(dst)
    i = 1
    while True:
        cand = '%s__%d%s' % (base, i, ext)
        if not os.path.exists(cand):
            return cand
        i += 1


def do_move(cfg, src, dst_rel):
    """底层移动原语（保留）：在媒体库内移动文件；返回最终绝对路径或 None。

    dst_rel 为目标目录（相对媒体库根）。重名自动加 __n（unique_dst）。
    注意：plan 流程绝不调用本函数；移动由统一 apply 流程（CLI 走文档引擎
    的 apply_plan）执行，本函数只作为 apply 层可复用的 mechanics。
    """
    d = os.path.join(cfg['library_root'], dst_rel)
    dst = os.path.join(d, os.path.basename(src))
    if cfg.get('use_sudo'):
        sudo_run(['mkdir', '-p', '--', d])
        dst = unique_dst(dst)
        p = sudo_run(['mv', '-f', '--', src, dst])
        if p.returncode != 0:
            return None
        sudo_run(['chown', '-R', '%d:%d' % (cfg['owner_uid'], cfg['owner_gid']), '--', d])
    else:
        os.makedirs(d, exist_ok=True)
        dst = unique_dst(dst)
        os.replace(src, dst) if os.path.dirname(src) == d else subprocess.run(
            ['mv', '-f', '--', src, dst], check=True)
    return dst


def build_report(cfg, result, plan, skipped, waiting):
    """构建治理报告 Markdown 文本（纯函数，不落盘；原 write_report 的组装逻辑）。"""
    counts = result['counts']
    lines = ['# 媒体库收件箱治理报告', '',
             '- 执行时间：%s' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             '- 投递目录：`%s`' % cfg['inbox'],
             '- 模式：%s' % ('实际归档' if result['applied'] else '只读预演'),
             '- 处理文件：%d 个，共 %.1f MB' % (result['processed'], result['bytes'] / 1e6), '',
             '## 去向统计', '', '| 去向 | 数量 |', '| --- | --- |']
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append('| %s | %d |' % (k, v))
    if waiting:
        lines += ['', '## 等待稳定（本轮跳过）', '', '| 文件 | 原因 |', '| --- | --- |']
        for w in waiting[:50]:
            lines.append('| %s | %s |' % (w['name'], w['reason']))
    if skipped:
        lines += ['', '## 未纳入（保留在原处）', '', '| 类型 | 数量 |', '| --- | --- |']
        for k, v in skipped.items():
            lines.append('| %s | %d |' % (k, v))
    if plan:
        lines += ['', '## 明细（最多 300 条）', '',
                  '| 文件 | 类型 | 判定 | 去向 | 置信 | 说明 |', '| --- | --- | --- | --- | --- | --- |']
        for p in plan[:300]:
            lines.append('| %s | %s | %s | %s | %.2f | %s |' % (
                p['name'], p['kind'], p['decision'], p['target'], p['score'], p.get('note', '')))
    lines += ['', '---', '',
              '规则：重复 / 模糊 / 低置信一律进入 `_Review`，不删除任何原始文件。']
    return '\n'.join(lines) + '\n'


def write_report(cfg, result, plan, skipped, waiting):
    """保留原名：返回报告 Markdown 文本，不落盘。

    v1 改动：原实现会写 work_dir/reports/ 与媒体库 .curator/reports/ 两份
    文件；统一架构下报告落盘由调用方（CLI/编排器）负责，本函数只产文本。
    需要写盘时调用方用 build_report() 的返回值自行写入。
    """
    return build_report(cfg, result, plan, skipped, waiting)


# ---------------- 复核区回炉 ----------------

REVIEW_SCOPE = ('待确认', '模糊')


def _is_placeholder(path):
    """iCloud 未下载占位符：文件头为 __CLOUDS，无图像数据。"""
    magic = b'__CLOUDS'
    try:
        with open(path, 'rb') as f:
            return f.read(9) == magic
    except Exception:
        pass
    p = sudo_run(['head', '-c', '9', '--', path], timeout=60)
    return p.stdout.strip().startswith('__CLOUDS')


def scan_review(cfg, scope):
    """扫描 _Review 指定子目录，返回媒体行列表（只读，不移动）。"""
    rows = []
    for bucket in scope:
        rel = cfg['review_dirs'].get(bucket)
        if not rel:
            continue
        base = os.path.join(cfg['library_root'], rel)
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for fn in files:
                if fn.startswith('.') or fn.startswith('._'):
                    continue
                kind = is_media(fn)
                if not kind:
                    continue
                p = os.path.join(root, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    st = sudo_run(['stat', '-c', '%s', '--', p])
                    try:
                        size = int(st.stdout.strip())
                    except Exception:
                        size = 0
                    rows.append({'path': p, 'name': fn, 'kind': kind, 'size': size,
                                 'mtime': 0, 'ext': os.path.splitext(fn)[1].lower(),
                                 'bucket': bucket})
                    continue
                rows.append({'path': p, 'name': fn, 'kind': kind, 'size': st.st_size,
                             'mtime': st.st_mtime, 'ext': os.path.splitext(fn)[1].lower(),
                             'bucket': bucket})
    rows.sort(key=lambda r: r['path'])
    return rows


def load_reuse(path):
    """载入既有重判结果（heic_reclass_merged.tsv 等），避免重复推理。"""
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding='utf-8') as f:
        rd = csv.reader(f, delimiter='\t')
        next(rd, None)
        for r in rd:
            if len(r) == 13:
                out[r[0]] = r
    return out


def review_plan(cfg, scope, reuse_path='', limit=0, tag='', apply=False):
    """生成 _Review 回炉方案（纯规划）：返回 (items, summary)，不写盘、不移动。

    items 每条为 dict：path/name/size/kind/bucket/category/score/reason/
    action（'move' 或 'keep'）/target/dup_of。summary 为统计 dict。
    与原实现相比：去掉了 TSV 方案写盘与 --apply 执行分支；apply=True 会
    直接抛 ValueError——移动统一走 CLI 的 apply 流程（文档引擎 apply_plan）。
    判定逻辑与原实现一致：占位符 keep；复用重判结果（reuse 第 13 列为 1 才用）；
    其余解码+分类+decide，明确的移出复核区，待确认/模糊/占位符原地保留，
    移出前再与库内哈希核验（命中复核区自身不算重复）。
    """
    if apply:
        raise ValueError('review_plan 不再执行移动：apply 请走统一 CLI apply 流程')
    idx = Index(cfg)
    reuse = load_reuse(reuse_path)
    rows = scan_review(cfg, scope)
    if limit:
        rows = rows[:limit]
    th = cfg['thresholds']
    cls = None
    items = []
    counts = {}
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        it = {'path': r['path'], 'name': r['name'], 'size': r['size'], 'kind': r['kind'],
              'bucket': r['bucket'], 'category': '', 'score': 0.0, 'reason': '',
              'action': 'keep', 'target': '', 'dup_of': ''}
        if _is_placeholder(r['path']):
            it.update(category='占位符', reason='icloud_placeholder', action='keep')
            items.append(it); counts['占位符'] = counts.get('占位符', 0) + 1
            continue
        rr = reuse.get(r['path'])
        if rr and rr[12] == '1':
            it['category'], it['score'], it['reason'] = rr[3], float(rr[5] or 0), rr[7]
        else:
            try:
                img, blur, white, colors, ar, note = decode_item(r)
            except Exception as e:
                img, blur, white, colors, ar, note = None, 0, 0, 0, 0, type(e).__name__
            if img is None:
                it.update(category='待确认', reason='decode_failed:%s' % note, action='keep')
            else:
                if cls is None:
                    cls = Classifier(cfg)
                sc = cls.scores(img, r['kind'])
                # 0.2.6 P1：把原图长边传给 decide 做小图门控。
                cat, cat2, s1, s2, why = decide(
                    r, sc, blur, white, colors, ar, note, cfg,
                    long_edge=max(img.size) if img is not None else None)
                it.update(category=cat, score=round(s1, 3),
                          reason=why if why != 'accepted' else 'accepted(%s)' % cat2)
        if it['category'] in ('待确认', '模糊', '占位符'):
            items.append(it); counts[it['category']] = counts.get(it['category'], 0) + 1
            continue
        # 去重核验：与库内哈希/近似比对
        sha = sha256_of(r['path'])
        if not sha.startswith('ERR'):
            dup = idx.dup_of(sha)
            if dup and dup.startswith(cfg['review_root']):
                dup = None  # 命中复核区自身，不算库内重复
            if dup:
                it.update(action='keep', dup_of=dup, reason='库内已有完全相同文件')
                counts['库内完全重复'] = counts.get('库内完全重复', 0) + 1
                items.append(it); continue
        it['target'] = target_dir(cfg, r['kind'], it['category'])
        it['action'] = 'move'
        counts[it['category']] = counts.get(it['category'], 0) + 1
        items.append(it)
        if i % 50 == 0:
            print('PROG %d/%d %.0fs' % (i, len(rows), time.time() - t0), flush=True)

    summary = {'scope': list(scope), 'scanned': len(rows), 'counts': counts,
               'tag': tag, 'applied': False,
               'moved': 0, 'failed': 0, 'elapsed_s': round(time.time() - t0)}
    return items, summary
