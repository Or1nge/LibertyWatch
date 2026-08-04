# Change log

## 2026-08-04

- 获得批量外发与持续触发授权后，生产开关已切换为
  `SHAREHOLDER_SCREEN_ENABLED=true`、`CODEX_ANALYSIS_MODE=PUBLIC`；当前31家触发
  公司的冻结`research_bundle`均以`INITIAL_TRIGGER_BACKLOG`入队。首个苏泊尔
  `SZ002032`分析使用`gpt-5.6-sol`/`xhigh`/`risk-review-v2.0.1`通过Schema和公开来源
  门禁，8个冻结输入文件的审计副本逐字节一致；analysis release
  `20260804T125207Z-971dae6b1eae`在阿里激活，本地/远端manifest及公司JSON SHA一致，
  公网API返回同一analysis ID。其余任务由并发1的常驻worker持续处理。
- 修复真实不可变安装环境暴露的三处worker兼容问题：Codex CLI显式跳过安装release
  的Git仓库检查；冻结输入归档不再复制setgid权限元数据；systemd只对白名单中的
  `published/analysis`开放写入并继续隔离structured发布。完整Python回归305项、前端
  14项及systemd unit校验通过。
- 生产安装、影子计算与阿里验收完成：本机正式结构化release
  `20260804T121601Z-3c07e8154f9b`已原子同步，阿里Web release
  `20260804T121500Z-fd1086718d`为healthy；公网逐一验证67个公司详情均返回200，
  67/67 watchlist enrichment、双分数、全局canary、manifest/SHA、零非有限数和零计算
  失败全部通过。当时安全门禁为`SHAREHOLDER_SCREEN_ENABLED=true`、
  `CODEX_ANALYSIS_MODE=OFF`，数据与publisher timer已启用，Codex worker在批量外发
  获得单独授权前保持disabled。修复了阿里Python 3.6激活助手、公开release权限、
  bind mount可移植相对链接、Web镜像漏装`liberty_v2`及远端canary解释器边界。
- 海尔智家`A600690`真实INTERNAL Codex smoke完成模型推理、本地latest和analysis
  release；真实接口暴露并修复Structured Outputs要求枚举/常量显式类型、且不接受
  `format: uri`的问题。事后审计发现模型把4个冻结输入伪装为`invalid.local`来源，
  因此新增公开来源网址门禁并将Prompt补丁版本升至`risk-review-v2.0.1`；原成功
  产物保留审计。获单独授权后的v2.0.1真实smoke一次成功，11条来源均为巨潮、
  上交所、国家统计局或商务部公开网址，禁用占位域名为0，latest与release校验通过。
- LibertyWatch V2公开计算升级为`shareholder-screen-v2.2.0`：67家公司全部进入
  价格机会分与财务韧性分双支柱筛选，缺失组成项通过coverage向50收缩；旧
  SEEV/SSY/CR10/RI/ERI退出公开主路径但保留legacy/internal回放。
- Futu详细财务不可变证据扩展到67/67家并纳入利润表；真实dry-run为价格机会分
  67/67、财务韧性分67/67、READY 11、DATA_LIMITED 56、触发候选31、零计算失败，
  schema、manifest、SHA及非有限数检查全部通过。56家DATA_LIMITED来自现有周线
  配额只覆盖11家，不以伪造数据补齐。
- Codex升级为逐公司触发的`risk-review-v2.0.0`，任务冻结`research_bundle.json`
  及精确SHA文件集；新增OFF/INTERNAL/PUBLIC三态开关，成功报告立即形成本地
  analysis release，publisher重试周期缩短到2分钟。安装器补齐资本结构配置，并
  在切换`current`前运行incoming health和67家公司覆盖smoke。

- v2.1只读API增加发布契约校验，前端直接展示公司层级、置信度、时效、口径、警告
  与阻断项。新增版本化人工审批清单和三段激活canary；67条、至少5家真实RI/ERI、
  分数有限且全部人工审批缺一不可，Codex dispatch也在5家前关闭。
- 股东回报计算与指标注册表升级至v2.1.0：删除qB阻断，未验证回购不授予贡献；
  简化FCF使用85%容量；增长正贡献按连续历史分档；当前估值没有可比历史时不做
  机械扣减；ReturnScore、PayoutQuality、RI和ERI按新政策落地。
- mixed-tier release在生成前验证记录合法性，BLOCKED公司发布当前空分数状态而非
  旧分数。真实67家公司dry-run为67个合法BLOCKED记录、零计算失败，第二次运行
  67/67命中慢缓存并通过manifest/SHA-256；因真实RI/ERI为0，v2仍默认关闭且未上线。
- 从安全基线 `c3009c7` 建立股东回报v2.1真实数据接入分支：新增67家公司资本
  结构授权表，正式采用“监控证券等价权益价值（SEEV）”，并对Futu隐含股数按
  2%/5%阈值复核；未经授权的供应商市值不会进入分母。
- 行情、汇率和当前PE/PB改为从 `latest_snapshot.json` 直接形成内存快变量覆盖层，
  不再每分钟改写慢staging；已有Futu资产负债表响应可确定性映射净负债/权益或
  负债/资产代理。
- 新增统一输入选择、领域置信度和公司assessment，拆分release合法性、公司数据
  层级、指标basis和行情freshness。真实67家公司dry-run可完整生成阻断原因，
  不再要求未计入的回购或不适用的四项对账来源。

## 2026-08-03

- 将原独立 `webapp/` 仓库迁入 Liberty 根级 Git 仓库，并沿用现有
  `Or1nge/LibertyWatch` 远端历史；新增根级敏感信息与运行数据隔离规则，明确
  只版本化采集/WebApp 代码、公开配置、文档和公开输出，不上传登录状态、密钥、
  SQLite、原始响应、年报 PDF、Futu 二进制或本地部署产物。
- 修复两份“PDF容器合法但不是完整年报”的来源误选：山东药玻FY2016由1页更正
  公告替换为139页完整年报，公牛集团FY2020由9页短附件替换为223页完整年报；
  旧文件保留并标记`SUPERSEDED_SOURCE_SELECTION`。重新提取后经营现金流
  `VALID`由221增至223。修复股息生命周期判定并全量重跑后，共保留3,296条
  证据，真正满足窄条件的候选由原先误报的13条降为4条。
- 在不写生产staging的隔离目录完成候选对账：223条经营现金流和45条资本开支
  全部通过本期年报、相邻年报比较数及Futu同财年三方核对。历史13条股息审计
  记录保留完整：原记录接受3条、拒绝10条；9个被拒原记录对应的实际分配由后续
  年报和实施事件重新建立，最终有11个完整财年普通股息总额可供后续受控导入。
  杭氧FY2023仍缺中期分红最终现金总额。
- 核实创科实业和安踏体育6个财年的注销事实；安踏FY2025由表格舍入候选
  26,571,000股纠正为逐笔合计26,570,200股。因认股权、股份奖励或可转债导致
  稀释后股本端点无法对账，6条均不计算`net_reduction_factor`或`B_eligible`。

## 2026-08-02

- 为此前缺少正式年报归档的56家公司新增526份官方PDF：巨潮资讯法定披露419份、
  港交所披露易107份；连同原11家，67家公司均有通过PDF头、页数和SHA-256校验的
  来源manifest。该`VERIFIED`仅表示官方文件归档完整，不表示财务字段已经验证。
- 通过Futu详细财务报表接口保存56家、112次不可变原始响应并建立独立v2来源账本：
  最近5年280个公司财年均取得经营现金流，262个取得资本开支；租赁本金、发行/
  稀释股数及注销桥接仍保持`MISSING`，56家公司均为`PARTIAL`且不得计算公司级收益率。
- 从56家的526份A/H股官方年报提取发行股本桥候选：
  `VALID=162 / REVIEW=325 / CONFLICT=39`，另有6份明确实际注销候选。`VALID`
  仅代表发行股数对账通过，不代表稀释后股本、核心注销数或公司v2状态。
- 建立526份年报的普通/特别股息候选账本，保留3,232条带页码、行号、
  财年、币种、单位、性质和审批状态的证据；仅13条满足人工复核后可能导入
  的窄条件，特别、拟派、多组件和冲突项均不进入核心普通股息。
- 建立官方年报与Futu现金流对账候选：经营现金流
  `VALID=221 / REVIEW=231 / CONFLICT=74`，资本开支`45 / 435 / 46`。
  所有候选均位于隔离目录，三类核心字段自动写入数仍为0。
- WebApp计算/定义升级为v2.0.2、Prompt v1.1、输出Schema 1.1：所有非紧急Codex
  分析共用同公司30日冷却，新增紧急事件绕过；Codex只可提出业务耐久度和治理候选，
  经版本化量表、来源、日期和有效期的确定性审核后写入Linux私有overlay。
- Codex worker默认复用现有项目所有者及其认证目录；独立`liberty-codex`账户降为
  可选加固，仍保留ephemeral、read-only sandbox、无shell调用和公开输出白名单。

## 2026-08-01

- 在独立 `webapp/` Git仓库完成 shareholder-return v2.0.1 工程实现：版本化Decimal指标、
  来源和对账契约、A/H与行业适配器、FastAPI/前端解释卡片、本地Codex队列、
  双通道原子发布、systemd、fake端到端测试与回滚；父项目原始数据不被迁移脚本
  改写。当前67家迁移记录仍待财年来源账本和行业字段回填，均按INVALID关闭推荐；
  专用用户安装、真实Codex推理和Ali发布尚未执行。

- 将主项目范围固定为 67 家公司、每家公司一只证券；正式名单从网站当前清单
  只读复制后独立维护，网站本身未纳入本次改动。
- 删除 93 个非正式公司目录及旧 105/143 公司全局研究、缓存、构建脚本和重复
  年报集；删除详情记录于 `data/monitor/prune_manifest_20260801.json`。
- 为 67 家公司建立独立的 `分红回购历史.md`，并以 SQLite 保存分红、回购、
  财报期、相关资讯及其修订历史。
- 新增 Futu OpenD 增量采集和公司级 `codex exec` 维护链路；无新增内容时不会
  调用 Codex，有新增时只更新受影响公司的历史文件。
- 新增约两小时一次的用户级 systemd 定时器、并发锁、接口限速、失败重试和
  项目完整性检查。
