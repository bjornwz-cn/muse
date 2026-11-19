# P2 后端开发笔记（api/ FastAPI 服务）

2026-11-19 · 后端开发员自测通过（32/32），未部署、未打包。

## 交付文件（只写了 api/，未碰其他目录）

| 文件 | 内容 |
| --- | --- |
| `api/__init__.py` | 包初始化：把项目根目录插进 sys.path（curator.py / common / curator_core 按根目录 import） |
| `api/main.py` | FastAPI app；启动时 `DB.init_db()`；挂 6 个路由模块；`/static` 仅当目录存在才挂载；`/` 有 `web/templates/index.html` 则走 Jinja2 模板，否则返回内置极简总览页 |
| `api/deps.py` | `get_db()`（每请求一连接，`check_same_thread=False`，用完关闭）、`get_settings()`（libraries.json + 媒体配置兜底 + 模型端点默认值）、凭据掩码工具 |
| `api/dashboard.py` | GET /api/dashboard |
| `api/review.py` | GET /api/review、POST /api/review/{id}/decide、POST /api/review/bulk |
| `api/jobs.py` | POST /api/jobs、GET /api/jobs、GET /api/jobs/{id}、POST /api/jobs/{plan_id}/apply、POST /api/jobs/{plan_id}/rollback |
| `api/browse.py` | GET /api/browse、GET /api/browse/file |
| `api/rules.py` | GET /api/rules、PUT /api/rules/{name} |
| `api/settings.py` | GET /api/settings、PUT /api/settings、POST /api/settings/key |

## 路由清单

| 方法与路径 | 行为 |
| --- | --- |
| GET `/` | 总览页 HTML（模板在则 Jinja2，否则内置页） |
| GET `/api/dashboard` | 各库文件数/体积、review_pending、today_new、最近 5 任务、dup 可回收（count/bytes） |
| GET `/api/review?status=&kind=` | `DB.list_review`；status=all 时全量查 |
| POST `/api/review/{id}/decide` | 见下「复核裁定」 |
| POST `/api/review/bulk` | 逐条裁定，返回逐条结果 |
| POST `/api/jobs` | guard 校验 → 建 job → 后台线程 `curator.cmd_plan`，立即返回 `{job_id, plan_id: null, status: 'running'}` |
| GET `/api/jobs` | 最近 20 条 |
| GET `/api/jobs/{job_id}` | job + plan 明细（`DB.get_plan` + `list_plan_items`，limit/offset 默认 200/0） |
| POST `/api/jobs/{plan_id}/apply` | 后台执行；收件箱 auto_apply 模式走子集路径（见下），其余走 `cmd_apply` |
| POST `/api/jobs/{plan_id}/rollback` | 后台调 `curator.cmd_rollback` |
| GET `/api/browse?dir=&tag=&q=&limit=` | 优先读 DB files 表；DB 为空时 scandir 实时列目录（只读）；未给 dir 且 DB 为空时列各库根 |
| GET `/api/browse/file?path=` | 文件元数据 + 最近 plan_item 的 category/reason/evidence + tags + operations 历史 |
| GET `/api/rules` | work / family / media 三份规则文件内容 + thresholds |
| PUT `/api/rules/{name}` | 仅 work/family/media；先 `DB.save_rules`（版本化），再回写对应 JSON（写前备份 `.bak`） |
| GET `/api/settings` | libraries / inbox / thresholds / model_endpoints / auto_apply / schedule / keys（掩码） |
| PUT `/api/settings` | 只更新 thresholds / auto_apply / schedule 回写 libraries.json；库路径不许经 API 改，返回里注明 |
| POST `/api/settings/key` | key 写 `config/.env.local`（600）+ `os.environ`；返回掩码 |

## 关键决策

1. **DB 路径**：`CURATOR_DB` 环境变量，否则 `data/nas-curator.db`（与 CLI 默认一致）。`get_db()` 不用 `DB.connect()`（它未传 `check_same_thread=False`），而是自建连接；后台线程用 `DB.connect()`（同线程使用，无跨线程问题）。
2. **plan 任务取 plan_id**：`cmd_plan` 不返回 plan_id，后台线程以「执行前最大 plan id」为基线，完成后取新出现的最大 plan id 写入 job summary `{plan_id, move, review}`；`cmd_plan` 抛 `SystemExit`（如未知库）也记为 job failed，不抛到请求线程。
3. **cmd_plan 入参补齐**：后台构造 `argparse.Namespace`，字段与 `curator.build_parser` 的 plan 默认值一致（rules/media_config 为空走默认规则文件，llm_host/sem/ocr 默认端点，workers=8 等）。`dry_run` 字段仅作兼容——plan 本身只做计划、从不移动文件。
4. **默认规则层**：`use_llm/use_sem` 默认 false，不主动连接任何模型端点；模型端点只读配置（llm_host 默认 `http://172.17.0.1:11434`，sem_host 默认 `http://172.17.0.1:11500` 或 `SEM_HOST`，ocr_url 默认 `http://127.0.0.1:8866`）。
5. **复核裁定移动**：approved 时取复核行 payload 的 `suggested_dst`（请求体可传 `suggested_dst` 覆盖、`new_name` 改目标文件名）；源文件存在才调 `CE.apply_plan` 单条移动（root 取源文件所在目录，uid/gid 1000），成功写 operations 并把对应 plan_item 置 done；源文件不存在只改状态并在返回里注明。rejected 只改状态。tags 仅当文件已在 files 表登记时写 file_tags，写失败不阻塞裁定。
6. **apply 的 auto_apply 子集路径**：仅当 `plan.library == 'inbox'` 且设置 `auto_apply` 开启时启用。`cmd_apply` 按 action 取项、不看 status，因此不走它：先把不满足 `confidence >= auto_threshold（默认 0.85）或 auto_exempt` 的 move/rename 项置 `skipped`，再直接调 `CE.apply_plan(过滤后项)` + 写 operations + 置 plan done；原 plan 备注 `auto_applied: true`（job summary）。其余情况走 `cmd_apply(plan_id)` 全量执行（用户已确认计划）。`auto_exempt` 目前 plan_items 表无此列，取不到即按 False 处理，留待后续字段扩展。
7. **dashboard 文件统计**：files 表非空时按 path 前缀统计各库；为空时 scandir 只扫顶层（不递归）。today_new 按 `substr(created_at,1,10)` 匹配当日。dup bytes 按 review_queue file_id 关联 files.size 求和（未登记文件计 0）。
8. **凭据**：`POST /api/settings/key` 的 provider→环境变量映射：`ark_plan→ARK_PLAN_KEY`、`ark_payg→ARK_PAYG_KEY`，未知 provider 按 `<PROVIDER>_KEY`。GET 永远只返回掩码（前 2 + `****` + 后 2，≤4 位全掩码）；`.env.local` 写后 chmod 600。
9. **模板兼容**：自测时 `web/templates` 恰由前端并行落盘；Starlette 1.10 的 `TemplateResponse` 须用新签名 `TemplateResponse(request, name, context)`，旧式 `(name, {"request": ...})` 会报 `TypeError: unhashable type: 'dict'`，已按新签名写。`web/static` 不存在时不挂载，不报错。
10. **错误口径**：所有接口返回 JSON，错误用 HTTPException + `{detail}`（400 校验/排除/不存在目录、404 任务/计划/复核/文件不存在、500 规则或配置回写失败）。

## 自测结果（32/32 通过）

自测脚本 `/tmp/test_p2_backend.py`（TestClient，DB 用临时文件，不碰 `data/nas-curator.db`；工作目录用 /tmp 临时目录，不碰 NAS）：

- import：`from api.main import app` 通过（12 条路由）。
- dashboard / review 列表与 kind 过滤 / decide approved 真实移动文件成功（源消失、目标出现、operations 有记录）/ bulk rejected / 404 与 400 校验。
- jobs：POST 立即返回 running；后台 plan 完成（plan #1：2 项，0 可执行、2 进复核——测试小文件置信度低，均进复核）；apply / rollback 全流程完成且文件回到原处；排除路径（`/vol1/1000/下载/Wangzhen-Mac/muse`）400、不存在目录 400。
- browse：dir 过滤、q 搜索、file 详情含 operations 历史、不存在文件 404。
- rules：GET 三份 + 阈值；PUT work 原内容回写成功（version 1，生成 `.bak` 后已清理）；未知规则集 400。
- settings：GET 字段齐全（auto_apply=True 来自 media 配置兜底）；PUT thresholds 生效且返回「库路径须手动改文件」提示；POST key 返回掩码 `sk****gh`，GET 不见明文；空 key 400。
- 清理：自测产生的 `data/plan_1.csv` 与 `rules.baijing-supply-v8.json.bak` 已删除；`config/.env.local` 测试行已清除（文件不存在时不留空文件）；`data/nas-curator.db` 未被创建。

## 待前端/后续注意

- 自测 plan 的 2 项均为 review（测试文本太短、置信度低），apply 真实移动路径由 review decide 单条移动覆盖；整 plan 含高置信 move 项的端到端移动尚未实测，建议前端联调时用真实样本复核一次。
- `auto_exempt` 尚未有 plan_items 列支撑，目前恒为 False；如需「白名单免阈值自动执行」，需先在 plan-item 生成处打标记。
- 未部署、未打包；启动方式：项目根目录 `.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000`（待 P3 定端口与启动方式）。

## 联调闭环（2026-11-19，P2 负责人）

- 上述「高置信 move 端到端移动尚未实测」已闭环：`tests/test_web_flow.py::test_full_flow`
  用真实规则关键词（产品销售合同）跑通 plan → apply → 文件归位 → rollback → 回原位。
- 上述「plan_items 表无 auto_exempt 列」已闭环：`common/schema.sql` 加列，
  `common/db.py::init_db` 对 P1 旧库做 ALTER TABLE 迁移，`add_plan_item` 持久化该标志；
  `_run_auto_apply` 现能按 unit 豁免执行。
- 另两处联调修复：`GET /api/jobs/{id}` 加 `items` 别名（前端 jobs.html 用 `data.items`）；
  `api/main.py` 补 `/review` `/jobs` `/browse` `/rules` `/settings` 五条页面路由。
