# 分红回购监控定时器

`liberty-dividend-buyback-monitor.timer` 在开机十分钟后首次运行，之后每次任务
完成两小时后再次运行，并附加最多五分钟随机延迟。

每轮只读取 `data/source/companies.json` 中的 67 家公司。Futu 原始记录和
SQLite 数据库写入 `data/monitor/`；只有出现新增或修订事件时才启动
`codex exec --ephemeral`。每次 Codex 只接触隔离暂存目录中的单一历史文件；
结构校验通过后才原子替换正式的 `分红回购历史.md`。

安装：

```bash
install -d -m 0755 ~/.config/systemd/user
install -m 0644 systemd/liberty-dividend-buyback-monitor.service \
  systemd/liberty-dividend-buyback-monitor.timer \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now liberty-dividend-buyback-monitor.timer
```

检查：

```bash
systemctl --user status liberty-dividend-buyback-monitor.timer
journalctl --user -u liberty-dividend-buyback-monitor.service -n 100
python3 scripts/dividend_buyback_monitor.py status
```
