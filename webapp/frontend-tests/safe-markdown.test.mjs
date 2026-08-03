import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

import { renderSafeMarkdown } from "../public/modules/safe_markdown.js";

test("safe markdown escapes raw HTML and script payloads", () => {
  const rendered = renderSafeMarkdown("# 标题\n<script>alert(1)</script>");
  assert.doesNotMatch(rendered, /<script>/);
  assert.match(rendered, /&lt;script&gt;/);
});

test("safe markdown only links http and https with safe attributes", () => {
  const rendered = renderSafeMarkdown(
    "[safe](https://example.com/a) [bad](javascript:alert(1))"
  );
  assert.match(rendered, /rel="noopener noreferrer nofollow"/);
  assert.doesNotMatch(rendered, /href="javascript:/);
});

test("metric explanations and v2 detail are accessible display-only UI", () => {
  const app = readFileSync(new URL("../public/app.js", import.meta.url), "utf8");
  for (const label of [
    "当前估值与4%位置",
    "统一数据评估",
    "股东分配历史",
    "现金流或资本覆盖",
    "十年保守回报",
    "自动推荐指数",
    "入手风险指数",
    "数据质量和来源",
    "Codex 定性分析",
  ]) {
    assert.match(app, new RegExp(label));
  }
  assert.match(app, /aria-controls="metric-info-popover"/);
  assert.match(app, /pointerover/);
  assert.match(app, /focusin/);
  assert.match(app, /metricInfoPopover\.addEventListener\("focusin"/);
  assert.match(app, /event\.key === "Escape"/);
  assert.doesNotMatch(app, /innerHTML\s*=\s*analysis\.report_markdown/);
  assert.match(app, /dataTierLabel/);
  assert.match(app, /freshnessLabel/);
  assert.match(app, /metricBasisLabel/);
  assert.doesNotMatch(app, /v2DetailMetricCard\(detail, "buyback_persistence_factor"/);
});

test("every literal metric explanation entry comes from the registry", () => {
  const app = readFileSync(new URL("../public/app.js", import.meta.url), "utf8");
  const registry = JSON.parse(
    readFileSync(
      new URL("../config/metric_definitions_v2.json", import.meta.url),
      "utf8"
    )
  );
  const defined = new Set(registry.metrics.map((item) => item.id));
  const patterns = [
    /metricInfoButton\(\s*"([^"]+)"/g,
    /metricHeader\(\s*"[^"]*"\s*,\s*"([^"]+)"/g,
    /v2DetailMetricCard\(\s*detail\s*,\s*"([^"]+)"/g,
  ];
  const used = new Set();
  for (const pattern of patterns) {
    for (const match of app.matchAll(pattern)) used.add(match[1]);
  }
  assert.ok(used.size > 20);
  assert.deepEqual([...used].filter((id) => !defined.has(id)), []);
});
