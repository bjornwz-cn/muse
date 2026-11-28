# archive/ — 2026-11-23 至 11-25  triage 期间的一次性分析/探查脚本

来源：`~/workspace/goals/nas-curator-app/hidden_files/`（分析脚本）。

- `an1_*` ~ `an7_*`：收件箱 26,067 个文件收尾批次的分析与应用脚本
  （损坏核查、文档应用、重复清理、空目录清理、复核收集）。
- `side_*`：并行探查脚本（分类、抽取、命名、DB 探查、删除复核）。
- `local_vlm_test.py`：NAS 本地 Ollama 视觉模型对照测试（只读 DB）。

**全部是历史记录，勿直接重跑。** 它们针对的是当时特定的批次与
快照，批次已跑完、数据状态已变。留存目的只有一个：审计——当时
跑了什么、按什么规则跑的。需要同类能力时，以 `scripts/` 根目录下
的可重复入口（`reevaluate_review.py`、`vlm_reclassify.py`、
`vlm_apply.py`、`year_*.py`）为准重写，不要复活这些一次性脚本。
