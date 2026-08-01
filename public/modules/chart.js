import {
  escapeHtml,
  formatNumber,
  formatPrice,
  isFiniteNumber
} from "./presentation.js";

function targetColor(key) {
  if (key === "deep") return "#0f7b50";
  if (key === "preferred") return "#0a0a0a";
  return "#5f6368";
}

function linePath(points) {
  if (!points.length) return "";
  return points
    .map((point, index) => `${index ? "L" : "M"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`)
    .join(" ");
}

export function priceChart(security) {
  const history = Array.isArray(security.history)
    ? security.history.filter((point) => isFiniteNumber(point.price))
    : [];
  if (history.length < 2) {
    return null;
  }

  const width = 720;
  const height = 286;
  const margin = { top: 22, right: 78, bottom: 34, left: 54 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;
  const validTargets = (security.targetLines ?? [])
    .filter((target) => isFiniteNumber(target.price));
  const values = [
    ...history.map((point) => point.price),
    ...validTargets.map((target) => target.price)
  ];
  let minimum = Math.min(...values);
  let maximum = Math.max(...values);
  const pad = Math.max((maximum - minimum) * 0.12, maximum * 0.025, 0.5);
  minimum = Math.max(0, minimum - pad);
  maximum += pad;
  const range = Math.max(maximum - minimum, 1);
  const x = (index) =>
    margin.left + (index / Math.max(history.length - 1, 1)) * innerWidth;
  const y = (value) =>
    margin.top + ((maximum - value) / range) * innerHeight;
  const points = history.map((point, index) => ({
    ...point,
    x: x(index),
    y: y(point.price)
  }));
  const path = linePath(points);
  const areaPath = `${path} L ${points.at(-1).x.toFixed(2)} ${(
    margin.top + innerHeight
  ).toFixed(2)} L ${points[0].x.toFixed(2)} ${(
    margin.top + innerHeight
  ).toFixed(2)} Z`;
  const gradientId = `price-area-${String(security.id).replace(/[^a-z0-9]/gi, "-")}`;

  const grids = Array.from({ length: 5 }, (_, index) => {
    const gridValue = maximum - (range * index) / 4;
    const gridY = y(gridValue);
    return `
      <line x1="${margin.left}" y1="${gridY}" x2="${width - margin.right}" y2="${gridY}"
        stroke="#ededed" stroke-width="1" />
      <text x="${margin.left - 10}" y="${gridY + 3}" text-anchor="end"
        fill="#777777" font-size="9">${formatNumber(gridValue, gridValue >= 100 ? 0 : 1)}</text>
    `;
  }).join("");

  const targets = validTargets
    .map((target) => {
      const targetY = y(target.price);
      const color = targetColor(target.key);
      return `
        <line x1="${margin.left}" y1="${targetY}" x2="${width - margin.right}" y2="${targetY}"
          stroke="${color}" stroke-width="1.2" stroke-dasharray="5 5" opacity="0.8" />
        <rect x="${width - margin.right + 7}" y="${targetY - 9}" width="65" height="18"
          rx="5" fill="${color}" opacity="0.1" />
        <text x="${width - margin.right + 39.5}" y="${targetY + 3}" text-anchor="middle"
          fill="${color}" font-size="8.2" font-weight="650">${escapeHtml(
            target.label
          )} ${formatNumber(target.price, 1)}</text>
      `;
    })
    .join("");

  const labelIndexes = Array.from(
    new Set([
      0,
      Math.round((history.length - 1) / 2),
      history.length - 1
    ])
  );
  const xLabels = labelIndexes
    .map((index) => {
      const point = points[index];
      const anchor =
        index === 0 ? "start" : index === history.length - 1 ? "end" : "middle";
      return `<text x="${point.x}" y="${height - 11}" text-anchor="${anchor}"
        fill="#777777" font-size="8.5">${escapeHtml(point.label ?? "")}</text>`;
    })
    .join("");

  const last = points.at(-1);
  const currency = security.currency ?? "";
  return `
    <svg class="price-chart" viewBox="0 0 ${width} ${height}" role="img"
      aria-label="${escapeHtml(security.name)}历史价格及三档目标价">
      <defs>
        <linearGradient id="${gradientId}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#080808" stop-opacity="0.12" />
          <stop offset="100%" stop-color="#080808" stop-opacity="0" />
        </linearGradient>
      </defs>
      ${grids}
      ${targets}
      <path d="${areaPath}" fill="url(#${gradientId})" />
      <path d="${path}" fill="none" stroke="#080808" stroke-width="2.4"
        stroke-linecap="round" stroke-linejoin="round" />
      <circle cx="${last.x}" cy="${last.y}" r="5.5" fill="#fff" stroke="#080808"
        stroke-width="2.4" />
      <g transform="translate(${Math.max(margin.left, last.x - 32)}, ${Math.max(
        margin.top,
        last.y - 31
      )})">
        <rect width="64" height="21" rx="6" fill="#252631" />
        <text x="32" y="14" text-anchor="middle" fill="#fff" font-size="9"
          font-weight="650">${escapeHtml(formatPrice(last.price, currency))}</text>
      </g>
      ${xLabels}
    </svg>
  `;
}
