# scripts/ — 运维与批处理脚本

> 0.2.5（2026-11-28）起：所有在仓外跑过的操作脚本一律纳入本目录。
> 目的：回归与维护有据可查——跑过的操作都能在仓里找到对应脚本，
> 而不是散落在 NAS /app/scripts/ 或各人工作目录里。

## 可重复的入口（日常用）

| 脚本 | 用途 |
| --- | --- |
| `reevaluate_review.py` | 对复核队列重评：按当前阈值重算可自动数，dry-run 默认，不动文件 |
| `vlm_reclassify.py` | VLM 重分类：对一批复核行跑 VLM 重新判定（写回 payload，不移动） |
| `vlm_apply.py` | VLM 批准应用：把 VLM 判定 >= 阈值的行按建议移动 |
| `vlm045_photos.py` / `vlm045_video.py` / `vlm045_frames.py` / `vlm045_apply.py` | 2026-11-28 的 0.45 批 VLM 复核流水线（照片/视频/抽帧/应用） |
| `vlm_apply_dedup.py` | VLM 批次去重应用 |
| `dup_sweep_20261128.py` / `dup_sweep_804_20261128.py` | 精确重复清扫（2026-11-28 两批，已跑完；删前写清单、备 DB） |
| `dedup_107_sweep.py` / `dedup_baidu_batch.py` | 早期精确重复清扫 |
| `year_siglip.py` / `year_register.py` / `year_summary.py` | 年份归档三件套：siglip 判定年份 → 登记 → 汇总 |
| `year_moveback*.py` | 年份归档回迁（把错放的文件按年份移回） |
| `analyze_pending.py` | 积压队列构成分析 |
| `audit_delete_manifest.py` | 删除清单审计（删前逐名清单核对） |

## 一次性批处理脚本（历史记录，勿直接重跑）

`vlm_reclassify_batch2..5.py`、`year_*_batch2..5.py`、`remaining4796_*.py`、
`pending_triage_*.py`、`triage_*.py`、`batch4_delete*.py` 等是 2026-11-23
至 11-25 各批次的**一次性**操作脚本，批次已跑完。留存目的是审计
（当时跑了什么、按什么规则跑的），不是可重复工具。重跑前必须先读
脚本头部的批次说明并确认目标批次仍然存在。

`archive/` 子目录另存了同一时期 37 个一次性分析/探查脚本
（`an1_*`~`an7_*`、`side_*`、`local_vlm_test.py`），来源是
`~/workspace/goals/nas-curator-app/hidden_files/`，同样只作审计记录。

## 安全约定

- 任何删除类脚本（`*delete*.py`、`*_cleanup.py`）执行前必须先写逐名
  清单、备份 DB（`/data/backups/`，带时间戳），并按王震已定规则执行
  （哈希相同 + 保留份完整可用才能删；同名不同内容两份都留）。
- 脚本里不许出现密钥/Token：凭据只从环境变量或 `~/.config/` 下的
  600 权限文件读，绝不写进脚本。
- 容器内跑脚本用 `python3 /app/scripts/<name>.py`；本地跑先确认
  DB 路径指向测试库，不要直连线上库。
