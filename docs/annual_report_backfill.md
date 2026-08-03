# 67家公司年报来源回填

更新日期：2026-08-03

## 范围与边界

`scripts/support/annual_report_backfill.py`只建立正式年报的来源账本和不可变PDF
证据，不从PDF猜测财务数值，也不把缺失年份写成0。默认跳过已有完整历史归档的
11家公司，对其余56家公司回填最近最多10个已经结束且正式披露的完整财年。

- A股：巨潮资讯网法定信息披露平台；
- 港股：香港交易所披露易标题搜索及其官方PDF；
- 财年：以财年结束年份表示，不固定为自然年。德昌电机控股的
  `2025/26年度年报`记录为`fiscal_year=2026`、
  `fiscal_year_end_date=2026-03-31`；
- 重述：修订或重述报告作为新公告ID保留，旧PDF不覆盖、不删除；
- 缺失：上市历史不足或官方库未返回报告时保持`NO_REPORT_FOUND/PARTIAL`，等待
  补证，绝不补0。

## 目录和契约

默认输出位于：

```text
data/raw/annual_reports/official_backfill_v1/
├── coverage.json
├── coverage.csv
├── source_index/
├── metadata/<company_id>/
└── companies/<company_id_公司名>/
    ├── manifest.json
    └── documents/FY<结束年份>/<公告ID>_annual_report.pdf
```

每条已验证文档至少记录公司/证券ID、市场和股份类别、官方来源、公告标题、官方
URL、公告日期、抓取时间、财年、财年结束日、币种、本地路径、字节数、PDF页数、
SHA-256、重述状态和数据状态。官方查询原始响应单独按SHA-256保存，方便复核
“为什么选中这份报告”。写入使用临时文件加原子重命名；进程锁阻止两个回填任务
同时修改账本。

同标题、同日期存在多个附件时，选择器优先更大的官方附件；“年报更正
公告”不得当作完整年报。后续发现选择错误时，旧PDF保留为来源链，但标记为
`SUPERSEDED_SOURCE_SELECTION`，不再进入任何字段提取。

## 命令

只检索公告元数据：

```bash
python3 scripts/support/annual_report_backfill.py discover
```

检索并下载剩余56家公司（默认全局并发2）：

```bash
python3 scripts/support/annual_report_backfill.py fetch --workers 2
```

按市场或单公司恢复：

```bash
python3 scripts/support/annual_report_backfill.py fetch --market CN --workers 2
python3 scripts/support/annual_report_backfill.py fetch --market HK --workers 2
python3 scripts/support/annual_report_backfill.py fetch --issuer HK0179
```

离线校验所有已下载PDF的文件头、页数、大小和SHA-256：

```bash
python3 scripts/support/annual_report_backfill.py verify
python3 scripts/support/annual_report_backfill.py summary
```

退出码`2`表示至少一个公司或财年失败。失败详情保留在对应`manifest.json`，再次
执行同一命令会跳过SHA-256仍合法的文件并只恢复缺口。普通单元测试不访问网络：

```bash
pytest -q scripts/tests/test_annual_report_backfill.py
```

## 与结构化财务账本的关系

PDF和公告清单是股息、注销回购、稀释后股本、经营现金流、资本开支及租赁本金
字段的来源底稿，不是这些字段本身。后续确定性提取必须逐字段写入币种、单位、
财年、来源文档和重述状态；只有完成对账的数据才能从`INVALID/PARTIAL`升级，
Codex不得用自行估算的数字回写结构化账本。

## 2026-08-02实际回填结果

- 新增56家公司清单，全部完成官方可用历史的PDF下载和离线校验；
- 当前选中526份完整PDF，共3,410,965,510字节；
- 其中419份来自巨潮法定信息披露平台，107份直接来自香港交易所披露易；
- 连同原有11家公司，`coverage.json`现为67家`VERIFIED`，无manifest错误；
- 德昌电机控股最新报告为FY2026，财年结束日为2026-03-31；
- 全量`verify`通过；专项测试与既有监控测试合计15项通过。
- 2026-08-03语义复核发现并修复两份误选：山东药玻FY2016由1页“年报更正
  公告”更正为139页完整年报，公牛集团FY2020由9页同名短附件更正为223页
  完整年报。两份旧文件未删除，仅标记为已被替代；修复后67家仍全部
  `VERIFIED`，526份当前年报的最小页数为99页。

这里的`VERIFIED`只表示“官方检索选中的全部可用PDF均已下载并通过SHA-256、
文件头和页数校验”，不等于公司已经拥有10年公开历史，更不等于v2结构化数据
已经变为`VALID`。官方归档少于10份的公司为：华能水电9份、公牛集团7份、
迈瑞医疗8份、百胜中国6份、华润饮料2份、百威亚太7份、华住集团6份、
中通快递6份、百济神州A股披露5份。其早期财年必须结合上市日期、其他上市地
官方年报或招股书另行核验；当前不得据此虚构长期稳定性。
