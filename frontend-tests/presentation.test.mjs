import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { priceChart } from "../public/modules/chart.js";
import {
  escapeHtml,
  filterSecurities,
  formatMarketValue,
  formatPct,
  sortSecurities,
  targetStatus,
  yieldToneClass
} from "../public/modules/presentation.js";

const securities = [
  {
    id: "missing",
    name: "待行情",
    ticker: "WAIT",
    market: "CN",
    sector: "公用事业",
    industry: "电力",
    derived: { distanceToPreferredPct: null }
  },
  {
    id: "near",
    name: "临近标的",
    ticker: "NEAR",
    market: "HK",
    sector: "金融",
    industry: "银行",
    valuationStatus: "attractive",
    derived: { distanceToPreferredPct: 2.5, alertStatus: "approaching" }
  },
  {
    id: "reached",
    name: "到价标的",
    ticker: "HIT",
    market: "CN",
    sector: "公用事业",
    industry: "水务",
    derived: { distanceToPreferredPct: -3.1 }
  }
];

test("distance sort keeps missing values last in both directions", () => {
  assert.deepEqual(
    sortSecurities(securities, "distance", "asc").map((item) => item.id),
    ["reached", "near", "missing"]
  );
  assert.deepEqual(
    sortSecurities(securities, "distance", "desc").map((item) => item.id),
    ["near", "reached", "missing"]
  );
});

test("filters compose query, market, sector and status", () => {
  const result = filterSecurities(securities, {
    query: "水务",
    market: "CN",
    sector: "公用事业",
    status: ""
  });
  assert.deepEqual(result.map((item) => item.id), ["reached"]);
});

test("valuation and alert status filters compose and yield tones deepen", () => {
  const result = filterSecurities(securities, {
    valuation: "attractive",
    alert: "approaching"
  });
  assert.deepEqual(result.map((item) => item.id), ["near"]);
  assert.equal(yieldToneClass(null), "yield-tone-0");
  assert.equal(yieldToneClass(2.99), "yield-tone-1");
  assert.equal(yieldToneClass(3), "yield-tone-2");
  assert.equal(yieldToneClass(4), "yield-tone-3");
  assert.equal(yieldToneClass(5), "yield-tone-4");
});

test("missing numbers stay blank and user content is escaped", () => {
  assert.equal(formatPct(null), "—");
  assert.equal(formatPct(1.25, { signed: true }), "+1.25%");
  assert.equal(escapeHtml('<img src=x onerror="x">'), "&lt;img src=x onerror=&quot;x&quot;&gt;");
  assert.deepEqual(targetStatus("within_3"), {
    label: "距目标 ≤ 3%",
    className: "is-near"
  });
  assert.equal(formatMarketValue(123_456_789_000, "CNY"), "1,234.57 亿元");
  assert.equal(formatMarketValue(null, "HKD"), "—");
});

test("chart returns no fake line without real history", () => {
  assert.equal(priceChart({ id: "empty", history: [] }), null);
});

test("chart renders target labels from supplied values only", () => {
  const svg = priceChart({
    id: "chart",
    name: "示例",
    currency: "CNY",
    history: [
      { label: "T-1", price: 10 },
      { label: "T", price: 9 }
    ],
    targetLines: [
      { key: "preferred", label: "理想价", price: 8.5 }
    ]
  });
  assert.match(svg, /理想价 8\.5/);
  assert.match(svg, /aria-label="示例历史价格及三档目标价"/);
});

test("chart handles a full ten-year weekly series", () => {
  const history = Array.from({ length: 522 }, (_, index) => ({
    timestamp: `W${index}`,
    label: index === 0 ? "2016-08" : index === 521 ? "2026-07" : "",
    price: 8 + Math.sin(index / 20)
  }));
  const svg = priceChart({
    id: "ten-years",
    name: "十年周线",
    currency: "HKD",
    history,
    targetLines: [
      { key: "watch", label: "3% 关注价", price: 11 },
      { key: "preferred", label: "4% 理想价", price: 9 },
      { key: "deep", label: "5% 深度价值价", price: 7 }
    ]
  });
  assert.match(svg, /<svg/);
  assert.match(svg, /2016-08/);
  assert.match(svg, /2026-07/);
  assert.match(svg, /4% 理想价 9/);
});

test("watchlist table keeps horizontal containment without trapping page wheel", () => {
  const css = readFileSync(
    new URL("../public/styles.css", import.meta.url),
    "utf8"
  );
  const rule = css.match(/\.table-scroll\s*\{([^}]*)\}/)?.[1] ?? "";
  assert.match(rule, /overflow-x:\s*auto/);
  assert.match(rule, /overscroll-behavior-x:\s*contain/);
  assert.match(rule, /overscroll-behavior-y:\s*auto/);
  assert.doesNotMatch(rule, /overscroll-behavior:\s*contain/);
});
