# Monitor data

- `liberty_monitor.sqlite3`：67 家公司的当前事件、事件修订和运行记录；
- `raw/<issuer_id>/latest.json`：每家公司最近一次完整 Futu 响应；
- `raw/<issuer_id>/snapshots/`：初始基线及发生变化时的原始快照；
- `staging/`：Codex 单文件维护时临时使用，任务结束后自动清空；
- `run.lock`：防止定时任务重叠运行。

初始化基线使用 `scripts/dividend_buyback_monitor.py bootstrap`。日常增量运行使用
`run`；数据库中未同步到 Markdown 的修订会在后续运行继续交给 Codex 重试。
