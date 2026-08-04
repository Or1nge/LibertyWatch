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
    "公司观察",
    "关键数据",
    "筛选结果",
    "观察理由",
    "数据依据",
    "风险复核",
  ]) {
    assert.match(app, new RegExp(label));
  }
  assert.match(app, /aria-controls="metric-info-popover"/);
  assert.match(app, /pointerover/);
  assert.match(app, /focusin/);
  assert.match(app, /metricInfoPopover\.addEventListener\("focusin"/);
  assert.match(app, /event\.key === "Escape"/);
  const currentDetail = app.slice(
    app.indexOf("function screeningResearchMarkup"),
    app.indexOf("function shareholderReturnV2Section")
  );
  assert.doesNotMatch(currentDetail, /JSON\.stringify/);
  assert.doesNotMatch(currentDetail, /trigger_type|calculation_version/);
  assert.doesNotMatch(currentDetail, /analysis\.report_markdown/);
  assert.match(app, /screeningStatusLabel/);
  assert.match(app, /freshnessLabel/);
  assert.match(app, /screeningWarningLabel/);
  assert.match(app, /function analysisConclusion/);
  assert.match(app, /INITIAL_TRIGGER/);
});

test("company detail uses readable desktop type and responsive source cards", () => {
  const css = readFileSync(new URL("../public/styles.css", import.meta.url), "utf8");
  assert.match(css, /\.detail-drawer\s*\{[^}]*width:\s*min\(760px, 96vw\)/s);
  assert.match(css, /\.drawer-section p,[\s\S]*?font-size:\s*14px/s);
  assert.match(css, /\.v2-detail-card-head > small\s*\{[^}]*font-size:\s*12px/s);
  assert.match(css, /\.source-summary-grid\s*\{[^}]*repeat\(3,/s);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.source-summary-grid\s*\{[^}]*grid-template-columns:\s*1fr/s);
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
