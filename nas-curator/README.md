# NAS Curator（文件管家）

飞牛 NAS 文件整理应用：把散落在各处的文档、照片、视频自动归类到工作 / 家庭 / 媒体三个库，低置信进人工复核，绝不误删。

由两个 Marvis 技能合并而来：`workfiles-curator` v3.1.1（文档整理）+ `media-library-curator`（媒体整理）。

## 功能

- **统一收件箱**：`/vol1/1000/收件箱`，30 分钟一轮监听，丢进去就自动分类
- **任意目录处理**：任选文件或目录出整理计划（dry-run），审过再执行
- **三库归位**：工作 / 家庭 / 媒体；高置信（≥0.85）自动执行，0.60–0.85 进复核附建议，<0.60 进复核无建议
- **Web 界面**：总览 / 复核 / 任务 / 浏览 / 规则 / 设置，端口 8765
- **去重**：SHA256 精确去重 + dHash 近重复（汉明距离 ≤5）
- **回滚**：每次 apply 都写 operations，可整批回滚；同名文件加 `__n`，不覆盖、不删原件
- **分类链**：实体画像 → 预筛规则 → bge-m3 语义向量 → LLM 兜底（火山 Ark → 按量付费 → 本地 Ollama）→ 人工复核

## 目录结构

```
curator.py            # 统一 CLI：inventory / plan / apply / rollback / review / inbox / process / analyze / explain
api/                  # FastAPI 后端（15 个端点）
web/                  # Jinja2 六页前端
common/               # DB 层（14 表）、哈希、回滚、标签、守卫
curator_core/         # 文档分类引擎（规则 v8、实体画像、预筛、语义、LLM）
media_core/           # 媒体分类引擎（SigLIP + ChineseCLIP，多帧采样）
config/libraries.json # 三库定义与阈值
scripts/              # 迁移、dry-run、汇总脚本
tests/                # 回归测试（29 passed / 3 skipped）
docs/                 # 架构设计与 P1–P4 报告
fpk/                  # 飞牛 .fpk 打包工程
Dockerfile            # 多架构 amd64+arm64，Python 3.12 slim
docker-compose.yml
```

## 安装（飞牛）

```bash
# 在 NAS 上构建镜像（隧道带宽低，勿从 VM 传镜像）
fnpack build   # 或按 docs/P3-REPORT-2026-11-19.md §3
# 生成 dist/nas-curator-<version>.fpk 后在飞牛应用中心手动安装
```

Web 入口：`http://<NAS-IP>:8765/`

## 配置

- 阈值与开关在 Web 设置页改（`thresholds` / `auto_apply` / `schedule`），库路径禁改
- 火山 Ark Key 只写 `config/.env.local`（600 权限），**绝不进仓库、镜像与 .fpk**；不填则走本地 Ollama
- 语义模型 bge-m3 在 `ollama-gpu:11500` 上（`SEM_HOST=11500`）

## 安全铁律

- 只出计划、不自动执行修库：`/vol1` 全量修库须先出 dry-run 计划、人工确认后再执行
- 不删原件、不覆盖、移动后 chown 1000:1000
- 排除：`下载/Wangzhen-Mac/muse/`、`下载/AI模型/`、压缩包、大文件 / 图书 / 音乐按 guard 规则不动

## 状态（2026-11-19）

- 版本：**0.1.1** 在 NAS 上运行（healthy，六页 200）
- P1 引擎合并、P2 Web 界面、P3 打包、P4 完善均已完成；详见 `docs/P1-REPORT-2026-11-19.md` ~ `docs/P4-REPORT-2026-11-19.md`
- dry-run 计划：文档批 2,030 项、Mac 批 857 项已出；媒体批（约 16 万文件，约 61 小时）在跑
- 干跑计划全文：见仓库外另存的 `NAS-Curator-全量修库dry-run计划-2026-11-19.md`
