# LibertyWatch V2 双支柱筛选（v2.2）

## 公开契约

公开schema为`shareholder-screen-v2`，计算与指标定义版本均为
`shareholder-screen-v2.2.0`，policy为`shareholder-screen-policy-v2.2.0`。
release必须恰有67家公司，状态仅为`READY`、`DATA_LIMITED`、`STALE`或
`UNAVAILABLE`。缺失字段保持`null`；只有身份冲突、无合法价格、结构损坏或非有限
值会形成公司级致命项。

价格机会分由Futu TTM股息率40%、当前估值锚35%和五年前复权周线价格位置25%
组成。非金融企业优先正PE-TTM/PE折算盈利收益率，PB只作降级；金融企业PB优先。
财务韧性分对非金融企业等权使用净利润、经营现金流、简化FCF和资产负债表，金融
企业使用净利润、ROE、净资产增长与监管资本/资产质量。缺失组成项不重分配权重：

```text
raw_score   = sum(valid_component_score * effective_weight) / coverage
coverage    = sum(valid_effective_weight)
final_score = 50 + coverage * (raw_score - 50)
```

完整分段、阈值和来源字段在`config/metric_policy_v2.json`。旧SEEV、SSY、CR10、
RI和ERI只保留在legacy/internal代码与注册表条目中，不进入v2.2公开门禁。

## Codex研究

确定性触发包括：价格机会分至少75；价格机会分至少65且财务韧性至少60；Futu
TTM股息率至少4%；达到可追溯v1理想价；或重大事件。首次启用用
`initial-backlog`补建当前已满足条件的公司。纯价格触发冷却7日，普通分析成功冷却
30日，重大事件绕过普通冷却，同公司锁和全局并发1保留。

每个任务输入目录精确包含公司快照、指标定义、触发、上一成功分析、来源索引、
prompt元数据、`research_bundle.json`和逐文件SHA。新版Schema只要求价格判断、触发
有效性、现金回报可持续性、机会或陷阱、最终结论、风险、来源与安全Markdown；
Codex不能修改任何量化分数或财务事实。

## 安全开关与安装

```text
SHAREHOLDER_SCREEN_ENABLED=false
CODEX_ANALYSIS_MODE=OFF
```

`INTERNAL`允许任务、验证和本地analysis release，但不向Ali发布；只有`PUBLIC`
才加载和同步analysis。公开筛选激活采用一次全局审批，绑定计算版本、policy SHA、
public contract SHA、批准时间与复核人。默认审批文件保持关闭。
显式`sync --channel structured`可在筛选开关关闭时先把已校验release预置到Ali；
这不会让Web读取新版。analysis同步仍严格要求`CODEX_ANALYSIS_MODE=PUBLIC`。

生产安装器先将代码和配置复制到incoming release，用incoming解释器运行
`health-check`并核对watchlist与公司配置均为67家，成功后才原子切换`current`。
无root权限时可执行无持久副作用的安装态smoke：

```bash
root=$(mktemp -d -p /tmp liberty-v2-install-smoke.XXXXXX)
./scripts/install_shareholder_v2_services.sh --smoke-only "$root"
```

## 验收与回滚

本地验收顺序：

```bash
.venv/bin/pytest -q
node --test frontend-tests/*.test.mjs
node --check public/app.js
python scripts/shareholder_v2.py health-check
python scripts/shareholder_v2.py refresh-prices
python scripts/shareholder_v2.py compute
python scripts/shareholder_v2.py readiness --compact
```

当前真实数据限制是Futu历史K线新增额度为0，现有周线只覆盖11家公司，因此其余
56家价格位置为空、价格机会分按75% coverage收缩并标记`DATA_LIMITED`。这不影响
67家公司进入release，也不会伪造周线。结构化/分析回滚仍使用：

```bash
python scripts/shareholder_v2.py rollback --channel structured --release-id <id>
python scripts/shareholder_v2.py rollback --channel analysis --release-id <id>
```
