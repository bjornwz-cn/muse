# NAS Curator 自学习机制设计（SELF-LEARNING-DESIGN）

版本：0.2.2（草案）｜日期：2026-11-26｜缘起：王震 2026-11-25 晚令“文件分类规则要不断优化，每批的教训都要回写进规则”；2026-11-26 令“把这条做成系统能力，不再靠人工回写；自学习要用大模型，优先用 NAS 本地模型”。

## 1. 目标

把“人工回写教训”变成闭环：每次分类决策都留痕 → 用户纠正是最强的学习信号 → 从纠正里提炼规则 → 学到的规则参与下一次分类 → 学坏的规则自动降级。硬规则永远优先，学到的规则不能覆盖硬规则。

非目标：不重写分类管线；不改自动执行阈值（0.85）与路径先验守卫；不碰线上容器与生产数据库（本版只在开发侧实现与验证，部署等王震点头）。

## 2. 现状：管线如何分类（只读梳理）

### 2.1 文档路径（curator_core/engine.py::plan_files）

- 关键词打分：`score_rule`（文件名命中权重 3/字符、 正 文 2/字 符）→ `best_category`（name_first=True 时文件名命中 ≥8 分直接定类，不再让正文改写；否则文件名+正文打分；分数 < min_score 或过弱进 low_cat）。
- 置信度：`_map_confidence`（score ≥12→0.9、≥8→0.85、≥min_score→0.7；review 行 ≥0.6 压到 0.45）。
- 补充判定：prescreen（向量近邻）→ sem/LLM（llmkit，见 §5）→ 敏感隔离、重复、空壳、云同步加密。
- 守卫：`common.routing.apply_path_prior_guard`（低置信不得跨顶层）；自动执行资格唯一入口 `common.routing.is_auto_apply_eligible`（action move/rename 且 confidence ≥0.85 且非 unit）。

### 2.2 媒体路径（media_core/engine.py::plan_media_files + curate.py::decide）

- SHA256 精确去重 → dHash 近重复 → 解码（失败进 review）→ 模糊检测 → SigLIP + ChineseCLIP 双模型打分 → `decide()`（白底规则优先；margin < 0.12 或 top1 < 0.35 进待确认；否则接受，confidence = top1）。
- 类目→文件夹映射在 `media_core/config.json`（photo_dirs/video_dirs）。

### 2.3 纠正现在记在哪里

- Web 复核：`api/review.py::decide_one`——用户点批准/驳回；改顶层（body.category）或改目的地（body.suggested_dst）时重算 dst 并移动。纠正信号当时只体现在最终 dst 里，**没有结构化记录**（预测值 vs 最终值没有对比表）。
- CLI：`curator.py::cmd_review` 只改状态；`_apply_items` / `cmd_apply` 执行移动、写 operations。同样没有“预测 vs 实际”的留痕。
- 现有 `learn_rules`（engine.py）是离线批量反推（从已整理好的目录学关键词），不是在线纠正学习——本设计补的是后者。

## 3. 自学习机制

### 3.1 反馈采集：decision_log

每次分类决策留一行（新表 `decision_log`，见 §4）：预测类目/置信度/理由、最终实际去向、结局（auto_applied / review_approved / review_rejected / user_corrected / manual_move）、是否被纠正、哪些学到的规则起了作用（rule_ids_applied）、是否已被学习器消费（processed）。

记录点（只加不改，全部 try/except 包住，学习失败绝不影响主流程）：

| 记录点 | 信号 |
|---|---|
| `api/review.py::decide_one`（用户在 Web 点裁定后） | 预测（payload.category/confidence）vs 最终（实际 dst/category）；body 改了类目/目的地 → user_corrected，否则 review_approved/rejected |
| `curator.py::_apply_items` 与 `cmd_apply`（移动执行后） | 预测（plan-item）vs 实际（operations.dst），结局 auto_applied |
| `curator.py::cmd_review` decide | 只有状态变化时记 review_approved/rejected（无 dst 变化） |

### 3.2 学习内容（learn_from_decisions）

消费未处理的 decision_log 行，分三类信号：

- **纠正（predicted ≠ final）**：最强信号。从本次纠正提炼候选规则：
  - 文件名模式：文件名分词后，去掉日期/数字/通用词，命中最终类目的显著 token（如“纳威”“税务申报”）→ filename_pattern 规则；
  - 内容关键词：有 file_text 时，从正文抽 top 关键词（复用 textkit.keywords），与最终类目相关者 → content_keyword 规则；
  - 来源路径：同一来源父目录多次纠正到同一目标（≥2 次）→ source_path 规则。
  - 候选规则初始 confidence 0.55、confirmations=1；同（kind, pattern, target）已存在则 confirmations+1、confidence+0.05（上限 0.95）。
- **确认（predicted = final 且有规则起作用）**：起作用的规则 confirmations+1、confidence+0.05（上限 0.95）。
- **反纠正（规则起了作用但最终仍被改）**：该规则 corrections+1、confidence−0.15（下限 0.10）；confidence < 0.30 时自动禁用（status=disabled）并记 learn_events（degraded）。

### 3.3 规则存储（learned_rules，与硬规则分开）

- 硬规则：`curator_core/rules.*.json` + `rules` 表（版本化），永远优先，学到的规则不能覆盖。
- 学到的规则：新表 `learned_rules`（kind, pattern, target, confidence, confirmations, corrections, status active/disabled, source, created_at, updated_at）。每条带置信度、来源（哪次纠正学来的，如 review#1234；种子规则记 seed:2026-11-25-batch）、版本（updated_at）。
- 目标合法性：学习时只接受目标在已知类目集合内的规则（文档：rules JSON 的 categories 路径；媒体：config.json 的 photo_dirs/video_dirs 键）；未知目标直接丢弃并记事件。
- 格式/策略类规则（kind=format_heuristic / policy）：不直接参与打分，作为前置检查或审计记录（如 xls 文件头判断、AppleDouble 有效、cv2 验视频、文件名不可信）。见 §6。

### 3.4 分类器集成（只加集成点，不改判定逻辑）

- **文档**：`plan_files(..., learn_conn=None)`；传入 DB 连接时建 LearnContext，在两次 `best_category` 调用后追加一次 learned-boost（`learn.boost_doc`）：命中规则（confidence ≥0.7 且 confirmations ≥2）给对应类目加分（+confidence×10，上限 +10，每文件最多 1 条规则起作用），理由追加 `learn:rule#id(pattern)+n`，起作用的规则 id 写入 plan-item 的 evidence（供确认计数）。`best_category` 本体不动；whitelist 与路径先验守卫仍在之后生效。
- **媒体**：`plan_media_files(..., learn_conn=None)`；有 LearnContext 时，对照片/视频在 `decide` 之前给匹配类目的 SigLIP/ChineseCLIP 分数加 +0.15（仅 confidence ≥0.8 且 confirmations ≥2 的 filename_pattern 规则），并记入 evidence。`decide()` 本体不动。
- **置信度**：学到的规则只通过 score 分数影响 `_map_confidence`，不直接写 confidence；自动执行门控（0.85）与路径先验守卫原样生效。

### 3.5 回滚与审计

- 每条规则可单独禁用/删除（CLI `selflearn disable|enable --id`）；禁用立即生效（下次分类不再加载）。
- 规则变更全记 `learn_events`（created/confirmed/corrected/disabled/degraded/seeded）。
- decision_log 全量保留，可重放学习；学坏的规则靠 §3.2 的自动降级处理，降级阈值（0.30）与步长（−0.15）为常量，可调。
- 种子规则与人工硬规则一样，禁用需显式操作，不会被自动降级（source 以 seed: 开头的规则不参与自动降级，只记事件）。

## 4. 存储（schema.sql 新增，三张表，均 IF NOT EXISTS）

```sql
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_path TEXT NOT NULL, ext TEXT, kind TEXT,
    predicted_category TEXT, predicted_confidence REAL, predicted_reason TEXT,
    final_category TEXT, final_dst TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('auto_applied','review_approved','review_rejected','user_corrected','manual_move')),
    corrected INTEGER NOT NULL DEFAULT 0,
    rule_ids_applied TEXT,          -- JSON 数组
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    processed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decision_processed ON decision_log (processed);

CREATE TABLE IF NOT EXISTS learned_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('filename_pattern','content_keyword','source_path','format_heuristic','policy','media_filename')),
    pattern TEXT NOT NULL, target TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.55,
    confirmations INTEGER NOT NULL DEFAULT 0,
    corrections INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('active','disabled')) DEFAULT 'active',
    source TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE (kind, pattern, target)
);
CREATE INDEX IF NOT EXISTS idx_learned_status ON learned_rules (status);

CREATE TABLE IF NOT EXISTS learn_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER, event TEXT NOT NULL,
    detail TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
```

`common/learn.py` 内 `ensure_tables()` 也建同样的表（IF NOT EXISTS），不依赖 schema.sql 重跑。

## 5. 模型选型（自学习 LLM 任务）

自学习的 LLM 任务是**文本推理**（单条纠正→提炼规则；多条纠正→总结模式；候选规则→生成描述），不是看图分类。2026-11-24“本地模型看图 52%”的结论针对的是 VLM 图像分类任务，不适用于文本任务——本次对文本任务单独实测。

### 5.1 NAS 实测可用模型清单（2026-11-26 实测）

| 容器 / 端口 | 模型 | 用途 | 备注 |
|---|---|---|---|
| ollama-cpu 0.40.5 / :11434 | qwen3:4b（2.5G） | 单条纠正提规则（快路径） | 现有 llmkit 默认模型；经 /api/chat 实测 4.5s、JSON 合格 |
| ollama-cpu 0.40.5 / :11434 | qwen3:8b（5.2G） | 多条纠正总结模式（主力） | 实测单条 30.4s、总结 39.4s、JSON 合格，模式区分最清晰 |
| ollama-cpu 0.40.5 / :11434 | qwen3-30b-a3b-instruct-2507:q4km（18G） | 复杂/疑难复核（深度路径） | 实测单条 86.4s，规则模式最丰富（含多变体）；CPU 推理慢，仅疑难用 |
| ollama-gpu 0.3.14 / :11500 | qwen2.5:3b（1.9G） | 单条纠正提规则（快路径，GPU） | GTX 750 可跑，实测 7.5s、JSON 合格、模式精确 |
| ollama-cpu / ollama-gpu | qwen3-vl:2b/8b | 不用于自学习文本任务 | 视觉模型；文本任务不需要 |
| ollama-gpu | codellama:7b-nas | 备用（代码类模式） | 未实测文本提规则；仅当规则涉及代码文件名模式时可试 |
| 两容器 | bge-m3 / bge-reranker-v2-m3 | 不可用作 LLM | 向量/排序模型，不是聊天模型 |

版本锁说明：ollama-gpu 是 0.3.14（为兼容 2014 年 GTX 750 锁定），大模型（如 qwen3-30b-a3b）在 GPU 容器加载失败；ollama-cpu 是 0.40.5（无此锁），30b 模型在 CPU 上实测可加载运行（86.4s/次）。“某模型加载不了”以容器实测为准，不要跨容器假设。

### 5.2 实测结果（2026-11-26，NAS 上实测，任务见下）

任务 A（单条纠正→提规则，JSON 输出）：“2021-09-01_陕西纳威_税务申报.zip，预测 06-其他，实际 3-北京供应链/08-运营与项目/02-投资与尽调”：

| 模型 | 耗时 | 结果 |
|---|---|---|
| qwen2.5:3b（GPU） | 7.5s | ✅ pattern“陕西纳威”，target 正确 |
| qwen3:4b（CPU，/api/chat） | 4.5s | ✅ pattern“陕西纳威”，target 正确 |
| qwen3:8b（CPU） | 30.4s | ✅ pattern“投并购尽调”，target 正确 |
| qwen3-30b-a3b（CPU） | 86.4s | ✅ pattern 多变体“投并购尽调\|投资尽调\|并购尽调\|尽调资料”，target 正确 |

任务 B（4 条纠正→总结误判模式，JSON 数组）：

| 模型 | 耗时 | 结果 |
|---|---|---|
| qwen2.5:3b（GPU） | 6.4s | ⚠️ JSON 合法但模式笼统、fix 为空——小模型不适合开放式总结 |
| qwen3:8b（CPU） | 39.4s | ✅ 区分出 3 类模式（类型误判 / 内容与类目不符 / 特殊格式未识别），fix 方向正确但偏笼统 |

注意：qwen3:4b 经 `/api/generate` 调用时曾无视“只输出 JSON”指令、长篇思考后截断；经 `/api/chat`（llmkit 实际走的路径）则 4.5s 正常输出 JSON。自学习一律走 llmkit 的 `/api/chat`，不要直接调 generate。

教训：开放式“总结模式”即使 8b 给出的 fix 也偏笼统。因此模式总结先由确定性聚合做（按 (predicted, final, reason) 分组计数），LLM 只负责把已算好的聚类写成一句话描述、或校验候选规则是否合理——不让 LLM 从原始记录里自由发现模式。

### 5.3 分工与 fallback

| 自学习子任务 | 首选（本地） | 备选（本地） | 云端 fallback 触发条件 |
|---|---|---|---|
| 单条纠正→提炼规则（JSON） | qwen3:4b（CPU，默认） | qwen2.5:3b（GPU） | 本地连续 2 次调用失败，或输出 2 次校验不通过（JSON 非法 / pattern 为空 / target 不在已知类目） |
| 多条纠正→模式描述（先聚合、后描述） | qwen3:8b（CPU） | qwen3-30b-a3b（CPU，疑难） | 同上；或 8b 输出的模式与聚合结果不一致时用 30b 复核一次 |
| 候选规则描述生成 | qwen3:4b | qwen3:8b | 同上 |
| 看图分类 | 不用 LLM（沿用 SigLIP/VLM 现有链路） | — | —（不在自学习 LLM 范围内） |

Fallback 走现有 `curator_core.llmkit.LLM` 的 provider 链（本地 Ollama → 方舟免费额度 → 方舟按量），链内顺序与冷却逻辑已实现（`order_providers` / `_mark`），自学习不另写 fallback。触发后记 learn_events（llm_fallback，含原因）。

### 5.4 可配置入口与升级路径

- 自学习调模型只走 `curator_core.llmkit.LLM.ask_json`（JSON 模式）/ `chat`，**模型名不写死**：`common/learn.py::get_selflearn_llm()` 从环境变量 `CURATOR_SELFLEARN_MODEL` 读模型名（缺省 `qwen3:4b`），host 沿用 llmkit 默认（NAS 11434）。换模型只改配置/环境变量，不改代码。
- 若后续换 GPU / 升级 Ollama：在模型列表配置里把深度路径模型换成更大的本地模型（如 qwen3:32b），或把快路径换成新 GPU 上的模型；`learn.py` 的模型选择只读配置，不感知硬件。
- LLM 只是“提议者”：它提的规则仍要过 §3.2 的确认门槛（confidence ≥0.7 且 confirmations ≥2 才起作用），且目标必须在已知类目内；LLM 提错了，下一次纠正会把它降级（§3.2）。

## 6. 首批种子规则（2026-11-25 批的教训，source=seed:2026-11-25-batch）

| kind | pattern | target | confidence | 说明 |
|---|---|---|---|---|
| filename_pattern | 纳威 | 08-运营与项目 | 0.80 | 54 个纳威文件全进北京供应链投并购尽调；目标只 assert 类目，所属方（北京供应链）是业务知识、不由关键词断言 |
| content_keyword | 税务申报 | 08-运营与项目 | 0.75 | 纳威税务申报包的内容信号 |
| media_filename | 截图 | 屏幕截图 | 0.70 | 启发式种子，需线上确认才升 confidence |
| format_heuristic | xls-header | （动作：先读文件头） | 0.90 | .xls 先看文件头：OLE（D0 CF 11 E0）走 xls2csv；XML（<?xml / <）走 SpreadsheetML strip-tags。本次已接进 textkit.extract_text |
| format_heuristic | appledouble-valid | （动作：视为有效） | 0.90 | `._*` 是 macOS AppleDouble 元数据文件，本身有效、不是损坏 |
| format_heuristic | cv2-only-video | （动作：只用 cv2） | 0.90 | 容器内无 ffprobe，验视频只用 cv2（其内部即 ffmpeg 库） |
| policy | filename-untrusted | （策略） | 0.85 | 文件名与内容可能不符，分类以内容为准；学到的 filename 规则需 ≥2 次确认才起作用 |
| policy | photo-by-content | （策略） | 0.90 | 照片/视频按内容归媒体库、不按所属方判断 |
| policy | doc-by-content | （策略） | 0.90 | 文档读取内容再判断归属 |

种子规则中 format_heuristic / policy 不参与打分，只作前置检查与审计；其中 xls-header 已在本次实现里接进 `textkit.extract_text`（见 §7）。

## 7. 实现清单（本版）

- 新增 `common/learn.py`：建表、记决策、学规则、查规则、提分、格式嗅探（sniff_xls / is_appledouble / verify_video_cv2）、种子、禁用/启用、LLM 提议（`propose_rule_llm`，经 llmkit、可注入 fake LLM 测试）。
- `common/schema.sql`：加 decision_log / learned_rules / learn_events 三张表。
- `curator_core/engine.py::plan_files`：加 `learn_conn=None` 可选参数；传入时在两次 best_category 调用后追加 learned-boost（`best_category` 本体不动）。
- `curator_core/textkit.py::extract_text`：OLE 分支里 .xls/.doc/.ppt 先嗅探文件头，XML 内容走 strip-tags（source=spreadsheetml），再走原工具。
- `media_core/engine.py::plan_media_files`：加 `learn_conn=None`；有 LearnContext 时在 decide 前给匹配类目加分并记 evidence（`decide()` 本体不动）。
- `api/review.py::decide_one`、`curator.py::cmd_review`：裁定后记 decision_log（try/except）。
- `curator.py::_apply_items` 与 `cmd_apply`：执行后记 decision_log（try/except）。
- `curator.py`：新增 `selflearn` 子命令（learn / rules / disable / enable / seed），与现有 `learn`（批量反推）区分开。
- `tests/test_learn.py`：纠正学会规则、规则下次生效、禁用后失效、格式嗅探、自动降级、媒体提分、决策留痕（全离线，tmp DB/文件，不碰 NAS、不调模型）。
- `docs/SELF-LEARNING-DESIGN.md`：本文档。

不做的（后续）：Web API 的学习接线（api/jobs 的 plan 路径）、定时自动学习任务（cron）、规则效果仪表盘。

## 8. 验证标准

- 一条纠正 → learn_from_decisions 产生一条规则；下一次同类文件分类时该规则起作用（reason 含 learn:rule#id）；禁用后不再起作用。
- .xls SpreadsheetML 与 OLE 两种文件头都能被 sniff 正确；textkit 对 SpreadsheetML .xls 能抽出文本（source=spreadsheetml）。
- 种子规则中至少两条在分类时生效（纳威 filename 规则提分类；xls-header 改变抽取结果）。
- 全部新增测试通过；现有回归测试不受影响（改动点都有默认关闭的开关：learn_conn=None 时行为与原来一致）。

## 9. 部署

- 本版只改开发侧代码并提交到 git（本地提交，不 push），不碰线上容器与生产数据库。
- 上线需王震确认：① 自学习默认开启还是先跑一段只记录、不生效的观察期；② LLM 提议规则是否需要人工先审一遍（建议：初期人工审，稳定后自动）；③ 定时学习任务的频率（建议每天一次，随晨报带一行摘要）。
