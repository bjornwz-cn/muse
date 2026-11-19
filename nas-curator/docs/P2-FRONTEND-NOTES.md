# NAS Curator P2 前端开发说明（2026-11-19）

> P2 前端：只写 `web/` 下的 Jinja2 模板 + 静态文件，不碰其他目录。
> 后端 FastAPI 由另一人并行开发；以下所有 fetch 调用均按任务给定的路由约定写，
> 以后端实际实现为准，联调时修正。

## 1. 页面清单

| 路由（建议挂载） | 模板 | 内容 |
| --- | --- | --- |
| `/` | `templates/index.html` | 总览：三库卡片（文件数/体积/待复核）、今日新增、重复可回收、最近任务列表 |
| `/review` | `templates/review.html` | 复核队列（主界面）：打开先看到待点条数；每行来源路径、kind 徽标、建议分类、置信度条、reason + evidence、建议目标路径；批准/拒绝/改分类/改名/打标签；复选框批量批准/拒绝；点完一条即从列表消失并刷新计数 |
| `/jobs` | `templates/jobs.html` | 整理任务：发起表单（root 或 library 下拉：工作/家庭/媒体/笔记本备份/统一收件箱；dry-run 默认勾选；use_llm/use_sem/rename 默认不勾）→ 提交后轮询 `GET /api/jobs/{id}` 显示进度 → 计划明细表 → 确认执行（POST apply）→ 执行后显示报告与回滚按钮（POST rollback） |
| `/browse` | `templates/browse.html` | 浏览：目录路径输入 + 分类树（工作/家庭/媒体/统一收件箱/笔记本备份顶层）+ 标签筛选 + 搜索框（文件名/正文 q）→ 条目表；点行弹详情（分类依据 reason/evidence、标签、历史操作） |
| `/rules` | `templates/rules.html` | 规则：三组规则 JSON 只读查看（work/family/media 折叠区）+ thresholds 四项可编辑保存 |
| `/settings` | `templates/settings.html` | 设置：库路径/收件箱只读、模型端点只读、auto_apply 开关、定时（收件箱扫描分钟、settle 分钟）可编辑保存；火山 key 输入框（password 类型，提交后只显示掩码） |
| 共用 | `templates/base.html` | 导航（总览/复核/任务/浏览/规则/设置）、复核待处理数徽标（调 `/api/dashboard` 刷新）、手机 viewport、简洁样式 |
| 共用 | `static/style.css` + `static/app.js` | 共用样式与少量 JS（fetch 封装、轮询、批量选择、置信度条渲染） |

## 2. API 调用约定（按任务给定，后端实际实现为准）

| 页面 | 调用 |
| --- | --- |
| base（徽标） | `GET /api/dashboard` → `review_pending` |
| 总览 | `GET /api/dashboard` |
| 复核 | `GET /api/review?status=pending&kind=`；`POST /api/review/{id}/decide`；`POST /api/review/bulk` |
| 任务 | `POST /api/jobs`；轮询 `GET /api/jobs/{id}`；`GET /api/jobs`；`POST /api/jobs/{plan_id}/apply`；`POST /api/jobs/{plan_id}/rollback` |
| 浏览 | `GET /api/browse?dir=&tag=&q=&limit=100`；`GET /api/browse/file?path=` |
| 规则 | `GET /api/rules`；`PUT /api/rules/thresholds`（见 §4） |
| 设置 | `GET /api/settings`；`PUT /api/settings`；`POST /api/settings/key` |

## 3. 设计要点

- **纯模板 + 静态文件**：无 React/Vue 等构建链；Jinja2 继承（`base.html` + `{% block %}`），
  页面数据全部走 `/api/*` fetch，模板本身不依赖任何服务端变量、不写死假数据。
  因此 Jinja2 渲染时用空上下文即可通过（见 §5）。
- **复核是主界面**：导航栏徽标常驻待处理数；复核页顶部大字显示待点条数；单条裁定成功后
  前端立即移除该行并刷新计数；批量操作只移除后端返回 `ok` 的条目。
- **计划先行、一键回滚**：任务页 dry-run 默认勾选；计划明细表先展示再确认执行；
  执行报告区出现后才显示回滚按钮，回滚前有 `confirm` 确认。
- **解释依据**：复核行与浏览详情都展示 `reason` + `evidence`；浏览详情另展示标签与历史操作。
- **手机能看**：`max-width: 1080px` 容器、viewport、表格横向滚动、表单栅格自适应、640px 以下断点。
- **kind 徽标**：`low_conf/dup/blur/ambiguous/sensitive/shell` 六种，按中文标签着色
  （`static/style.css` 的 `.tag.kind-*`）。
- **置信度条**：`≥0.85` 绿、`0.60–0.85` 黄、`<0.60` 红，与架构阈值口径一致。
- **复核改分类下拉**：目前选项为三库顶级分类（工作/家庭/媒体）；后端返回完整 categories 后可扩展为细分类下拉。
- **任务 library 下拉**：`work/family/media/mac-backup/inbox` 五项，取值与 `config/libraries.json` 的库 id / 输入源 id 对齐；
  `root` 与 `library` 二选一填一个，两个都空时前端拦截提示。
- **Key 安全**：火山 key 输入框为 `password` 类型；提交后清空输入框，只显示后端返回的掩码，不回显明文。

## 4. 待联调确认点（以后端实际实现为准）

1. **阈值保存路由（二选一，已写死一种）**：本前端写死 `PUT /api/rules/thresholds`。
   若后端未实现该路由，改为 `PUT /api/settings {thresholds}`（一处改动：`templates/rules.html` 内联脚本的保存处理）。
2. **`GET /api/jobs/{id}` 的 plan 标识**：前端取 `plan.id`，回退 `job.plan_id`；apply/rollback 用该 plan_id。
   若后端返回结构不同，按实际字段调整。
3. **任务状态值**：轮询结束条件为 `done/failed/rolled_back`；若后端使用其他终态值（如 `completed`），需同步调整
   （`templates/jobs.html` 的 `finished` 判断）。
4. **复核 `decide` 的 category 取值**：前端提交改分类下拉的值（目前为工作/家庭/媒体顶级名）；
   若后端要求分类 id/路径，按实际约定调整。
5. **静态文件挂载路径**：模板引用 `/static/style.css` 与 `/static/app.js`；后端挂载静态目录时保持此外缀。
6. **页面路由**：导航链接为 `/`、`/review`、`/jobs`、`/browse`、`/rules`、`/settings`；
   后端挂载模板路由时保持一致（或按实际路由改导航 href）。

## 5. 验证：Jinja2 假数据渲染（2026-11-19）

- 环境：临时 venv（`/tmp/jinja-venv`，Jinja2 3.1.6），系统 Python 无 Jinja2，未动项目外持久文件。
- 方法：`Environment(FileSystemLoader('templates'))`，空上下文 `{}` 渲染每个模板；
  另用 `StrictUndefined` 复核，确认模板不依赖任何未传入变量。
- 结果：**7/7 通过**（含 base），StrictUndefined 下 6 个页面模板全部通过。

| 模板 | 渲染结果 | 长度 |
| --- | --- | --- |
| `base.html` | OK | 1,067 字符 |
| `index.html` | OK | 3,864 字符 |
| `review.html` | OK | 7,208 字符 |
| `jobs.html` | OK | 8,876 字符 |
| `browse.html` | OK | 6,598 字符 |
| `rules.html` | OK | 4,049 字符 |
| `settings.html` | OK | 5,494 字符 |

- 关键约定字符串抽查：每个模板对应的 `/api/*` 端点、关键元素 id（如 `review-badge`、
  `btn-bulk-approve`、`review-check`、`btn-apply`、`btn-rollback`、`tree-btn`、
  `/api/rules/thresholds`、`auto_apply`）**全部命中**。
- `static/app.js`（5,413 字符）：含 fetch 封装 `api()`、轮询 `poll()`、置信度条 `confBar()`；
  `node --check` 语法通过（node v24.22.1）。
- `static/style.css`（7,909 字符）：导航、徽标、卡片、表格横向滚动、置信度条、弹窗、提示条、手机断点。

## 6. 未做事项

- 不部署、不打包；未联调真实后端（后端并行开发中，`api/` 目录当时为空）。
- 未碰 NAS、未写项目外文件。
