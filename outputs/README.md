# Outputs

- [START_HERE.md](START_HERE.md)：67 家公司的唯一总索引；
- `companies/<发行人ID_公司名>/README.md`：公司入口，旧研究只在仍有保留价值时存在；
- `companies/<发行人ID_公司名>/分红回购历史.md`：分红、年度回购汇总、已出现的
  财报期、相关公告资讯和 Codex 增量维护记录。

公司目录必须与 `data/source/companies.json` 一一对应。初始化基线由采集脚本
确定性生成；之后只有检测到新增或修订事件时，`codex exec` 才更新对应公司的
历史文件。
