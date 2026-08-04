const marketLabels = {
  CN: "A 股",
  HK: "港股",
  US: "美股",
  SH: "沪市",
  SZ: "深市",
  DEMO: "演示"
};

const valuationLabels = {
  deeply_attractive: "深度价值",
  "deeply-undervalued": "深度价值",
  attractive: "具吸引力",
  undervalued: "偏低估",
  fair: "合理",
  expensive: "偏贵",
  overvalued: "偏贵",
  unconfigured: "待评估",
  深度低估: "深度价值",
  低估: "偏低估",
  合理: "合理",
  高估: "偏贵"
};

const targetLabels = {
  reached: ["已到理想价", "is-reached"],
  within_3: ["距目标 ≤ 3%", "is-near"],
  within_10: ["距目标 ≤ 10%", "is-watch"],
  far: ["继续等待", "is-waiting"],
  unconfigured: ["未设目标", "is-missing"],
  price_unavailable: ["待行情", "is-missing"]
};

const alertLabels = {
  buy_zone: ["已触发", "is-new"],
  approaching: ["即将触发", "is-near"],
  watch: ["关注中", "is-watch"],
  none: ["未触发", "is-idle"],
  unavailable: ["数据不足", "is-missing"],
  not_configured: ["未配置", "is-missing"]
};

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

export function formatNumber(value, digits = 2) {
  if (!isFiniteNumber(value)) return "—";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  }).format(value);
}

export function formatPrice(value, currency = "") {
  if (!isFiniteNumber(value)) return "—";
  const symbols = { CNY: "¥", HKD: "HK$", USD: "$" };
  return `${symbols[currency] ?? ""}${formatNumber(value, value >= 1000 ? 0 : 2)}`;
}

export function yieldToneClass(value) {
  if (!isFiniteNumber(value)) return "yield-tone-0";
  if (value >= 5) return "yield-tone-4";
  if (value >= 4) return "yield-tone-3";
  if (value >= 3) return "yield-tone-2";
  return "yield-tone-1";
}

export function formatPct(value, { signed = false, digits = 2 } = {}) {
  if (!isFiniteNumber(value)) return "—";
  const sign = signed && value > 0 ? "+" : "";
  return `${sign}${formatNumber(value, digits)}%`;
}

export function formatMetric(value, suffix = "", digits = 1) {
  if (!isFiniteNumber(value)) return "—";
  return `${formatNumber(value, digits)}${suffix}`;
}

export function formatMarketValue(value, currency = "") {
  if (!isFiniteNumber(value)) return "—";
  const units = {
    CNY: "亿元",
    HKD: "亿港元",
    USD: "亿美元"
  };
  const unit = units[currency] ?? `亿 ${currency}`.trim();
  return `${formatNumber(value / 100_000_000, 2)} ${unit}`;
}

export function formatDateTime(value, { timeOnly = false } = {}) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: timeOnly ? undefined : "2-digit",
    day: timeOnly ? undefined : "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai"
  }).format(date);
}

export function formatShortDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone: "Asia/Shanghai"
  }).format(date);
}

export function marketLabel(value) {
  return marketLabels[String(value ?? "").toUpperCase()] ?? value ?? "—";
}

export function valuationLabel(value) {
  return valuationLabels[value] ?? value ?? "待评估";
}

export function targetStatus(value) {
  const [label, className] = targetLabels[value] ?? ["数据不足", "is-missing"];
  return { label, className };
}

export function alertStatus(value) {
  const [label, className] = alertLabels[value] ?? ["数据不足", "is-missing"];
  return { label, className };
}

export function badge(label, className = "is-idle") {
  return `<span class="badge ${className}">${escapeHtml(label)}</span>`;
}

export function trendClass(value) {
  if (!isFiniteNumber(value) || value === 0) return "neutral";
  return value > 0 ? "positive" : "negative";
}

export function distanceClass(value) {
  if (!isFiniteNumber(value)) return "";
  if (value <= 0) return "is-reached";
  if (value <= 10) return "is-near";
  return "";
}

export function initials(name) {
  return String(name ?? "—")
    .replace(/（.*?）|\(.*?\)/g, "")
    .replace(/\s+/g, "")
    .slice(0, 2);
}

export function heatLabel(score) {
  if (!isFiniteNumber(score)) return "数据不足";
  if (score >= 80) return "偏热";
  if (score >= 60) return "升温";
  if (score >= 40) return "中性";
  if (score >= 20) return "降温";
  return "偏冷";
}

export function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function getSortValue(security, key) {
  const screening = security.shareholderReturnV2?.schema_version === "shareholder-screen-v2"
    ? security.shareholderReturnV2
    : null;
  const sortableNumber = (value) => value === null || value === undefined || value === ""
    ? Number.NaN
    : Number(value);
  switch (key) {
    case "name":
      return security.name;
    case "ticker":
      return security.ticker;
    case "market":
      return security.market;
    case "price":
      return security.quote?.currentPrice ?? security.currentPrice;
    case "change":
      return security.quote?.dailyChangePct ?? security.dailyChangePct;
    case "preferred":
      return security.targetPrices?.preferred ?? security.preferredPrice;
    case "distance":
      return security.derived?.distanceToPreferredPct;
    case "yield":
      return (
        security.currentShareholderYieldPct ??
        security.expectedDividendYieldPct
      );
    case "updated":
      return security.quote?.lastUpdatedAt ?? security.lastUpdate;
    case "opportunity":
      return sortableNumber(screening?.opportunity_score?.value);
    case "resilience":
      return sortableNumber(screening?.financial_resilience_score?.value);
    case "dividend":
      return sortableNumber(
        screening?.opportunity_score?.components?.dividend_yield?.input_value
      );
    default:
      return screening
        ? sortableNumber(screening.opportunity_score?.value)
        : security.derived?.distanceToPreferredPct;
  }
}

export function sortSecurities(securities, sortKey = "distance", direction = "asc") {
  const multiplier = direction === "desc" ? -1 : 1;
  return [...securities].sort((left, right) => {
    const leftValue = getSortValue(left, sortKey);
    const rightValue = getSortValue(right, sortKey);
    const leftMissing =
      leftValue === null || leftValue === undefined || leftValue === "" ||
      (typeof leftValue === "number" && !Number.isFinite(leftValue));
    const rightMissing =
      rightValue === null || rightValue === undefined || rightValue === "" ||
      (typeof rightValue === "number" && !Number.isFinite(rightValue));
    if (leftMissing && rightMissing) {
      return String(left.name).localeCompare(String(right.name), "zh-CN");
    }
    if (leftMissing) return 1;
    if (rightMissing) return -1;
    const comparison =
      typeof leftValue === "string"
        ? leftValue.localeCompare(rightValue, "zh-CN")
        : leftValue - rightValue;
    return (
      comparison * multiplier ||
      String(left.name).localeCompare(String(right.name), "zh-CN")
    );
  });
}

export function filterSecurities(securities, filters = {}) {
  const query = String(filters.query ?? "").trim().toLocaleLowerCase("zh-CN");
  return securities.filter((security) => {
    if (query) {
      const haystack = [
        security.name,
        security.ticker,
        security.market,
        security.sector,
        security.industry
      ]
        .join(" ")
        .toLocaleLowerCase("zh-CN");
      if (!haystack.includes(query)) return false;
    }
    if (filters.market && security.market !== filters.market) return false;
    if (filters.sector && security.sector !== filters.sector) return false;
    if (
      filters.status &&
      security.derived?.targetStatus !== filters.status
    ) {
      return false;
    }
    if (
      filters.valuation &&
      security.valuationStatus !== filters.valuation
    ) {
      return false;
    }
    if (
      filters.alert &&
      security.derived?.alertStatus !== filters.alert
    ) {
      return false;
    }
    return true;
  });
}
