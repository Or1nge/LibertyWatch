import {
  escapeHtml,
  formatNumber,
  formatPct,
  formatPrice,
  formatShortDate,
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

function preferredTargetPrice(security) {
  const targetLine = (security.targetLines ?? []).find(
    (target) => target.key === "preferred" && isFiniteNumber(target.price)
  );
  return (
    targetLine?.price ??
    security.targetPrices?.preferred ??
    security.preferredPrice ??
    null
  );
}

export function historicalShareholderYieldPct(security, price) {
  const preferred = preferredTargetPrice(security);
  if (
    !isFiniteNumber(preferred) ||
    preferred <= 0 ||
    !isFiniteNumber(price) ||
    price <= 0
  ) {
    return null;
  }
  return (preferred * 4) / price;
}

export function priceChart(security, { compact = false } = {}) {
  const history = Array.isArray(security.history)
    ? security.history.filter((point) => isFiniteNumber(point.price))
    : [];
  if (history.length < 2) {
    return null;
  }

  const width = compact ? 440 : 720;
  const height = compact ? 300 : 286;
  const margin = compact
    ? { top: 24, right: 82, bottom: 42, left: 62 }
    : { top: 22, right: 78, bottom: 34, left: 54 };
  const axisFontSize = compact ? 13.5 : 9;
  const targetFontSize = compact ? 10.5 : 8.2;
  const latestFontSize = compact ? 10.5 : 9;
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
        fill="#666666" font-size="${axisFontSize}" font-weight="560">${formatNumber(
          gridValue,
          gridValue >= 100 ? 0 : 1
        )}</text>
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
          fill="${color}" font-size="${targetFontSize}" font-weight="650">${escapeHtml(
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
        fill="#666666" font-size="${compact ? 11.5 : 8.5}" font-weight="540">${escapeHtml(
          point.label ?? ""
        )}</text>`;
    })
    .join("");

  const last = points.at(-1);
  const currency = security.currency ?? "";
  return `
    <svg class="price-chart" viewBox="0 0 ${width} ${height}" role="img" tabindex="0"
      data-chart-left="${margin.left}" data-chart-right="${width - margin.right}"
      data-chart-top="${margin.top}" data-chart-bottom="${margin.top + innerHeight}"
      aria-label="${escapeHtml(security.name)}历史价格及三档目标价，可滑动逐周查看">
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
      <g data-chart-latest>
        <circle cx="${last.x}" cy="${last.y}" r="5.5" fill="#fff" stroke="#080808"
          stroke-width="2.4" />
        <g transform="translate(${Math.max(margin.left, last.x - 32)}, ${Math.max(
        margin.top,
        last.y - 31
      )})">
          <rect width="64" height="21" rx="6" fill="#252631" />
          <text x="32" y="14" text-anchor="middle" fill="#fff" font-size="${latestFontSize}"
            font-weight="650">${escapeHtml(formatPrice(last.price, currency))}</text>
        </g>
      </g>
      ${xLabels}
      <rect data-chart-hit-area x="${margin.left}" y="${margin.top}"
        width="${innerWidth}" height="${innerHeight}" fill="transparent" />
      <g data-chart-inspector hidden aria-hidden="true">
        <line data-chart-inspector-line y1="${margin.top}" y2="${margin.top + innerHeight}"
          stroke="#555861" stroke-width="1" stroke-dasharray="3 3" />
        <circle data-chart-inspector-point r="5" fill="#fff" stroke="#080808"
          stroke-width="2.4" />
        <g data-chart-inspector-tooltip>
          <rect width="218" height="54" rx="8" fill="#252631" opacity="0.96" />
          <text data-chart-inspector-date x="11" y="20" fill="#fff"
            font-size="11.5" font-weight="680"></text>
          <text data-chart-inspector-yield x="11" y="40" fill="#dfe4df"
            font-size="10.5" font-weight="560"></text>
        </g>
      </g>
    </svg>
  `;
}

export function bindPriceChartInteraction(svg, security) {
  if (!svg) return;
  const history = Array.isArray(security.history)
    ? security.history.filter((point) => isFiniteNumber(point.price))
    : [];
  if (history.length < 2) return;

  const inspector = svg.querySelector("[data-chart-inspector]");
  const line = svg.querySelector("[data-chart-inspector-line]");
  const pointMarker = svg.querySelector("[data-chart-inspector-point]");
  const tooltip = svg.querySelector("[data-chart-inspector-tooltip]");
  const dateText = svg.querySelector("[data-chart-inspector-date]");
  const yieldText = svg.querySelector("[data-chart-inspector-yield]");
  if (!inspector || !line || !pointMarker || !tooltip || !dateText || !yieldText) return;

  const left = Number(svg.dataset.chartLeft);
  const right = Number(svg.dataset.chartRight);
  const top = Number(svg.dataset.chartTop);
  const bottom = Number(svg.dataset.chartBottom);
  const prices = history.map((point) => point.price);
  const minimum = Math.min(
    ...prices,
    ...(security.targetLines ?? [])
      .filter((target) => isFiniteNumber(target.price))
      .map((target) => target.price)
  );
  const maximum = Math.max(
    ...prices,
    ...(security.targetLines ?? [])
      .filter((target) => isFiniteNumber(target.price))
      .map((target) => target.price)
  );
  const pad = Math.max((maximum - minimum) * 0.12, maximum * 0.025, 0.5);
  const chartMinimum = Math.max(0, minimum - pad);
  const chartMaximum = maximum + pad;
  const range = Math.max(chartMaximum - chartMinimum, 1);
  const currency = security.currency ?? "";
  let selectedIndex = history.length - 1;

  const showIndex = (rawIndex) => {
    selectedIndex = Math.max(0, Math.min(history.length - 1, rawIndex));
    const record = history[selectedIndex];
    const pointX = left + (selectedIndex / (history.length - 1)) * (right - left);
    const pointY = top + ((chartMaximum - record.price) / range) * (bottom - top);
    const tooltipWidth = 218;
    const tooltipHeight = 54;
    const tooltipX = Math.max(
      left,
      Math.min(
        right - tooltipWidth,
        pointX + (pointX > (left + right) / 2 ? -tooltipWidth - 10 : 10)
      )
    );
    const tooltipY = Math.max(
      top,
      Math.min(bottom - tooltipHeight, pointY - tooltipHeight - 10)
    );
    const date = record.timestamp
      ? formatShortDate(record.timestamp)
      : record.label || "日期未知";
    const shareholderYield = historicalShareholderYieldPct(security, record.price);
    const priceLabel = formatPrice(record.price, currency);
    const yieldLabel = isFiniteNumber(shareholderYield)
      ? `股息回购率 ${formatPct(shareholderYield)} · 现周期口径`
      : "股息回购率 — · 当前基数不足";

    line.setAttribute("x1", pointX);
    line.setAttribute("x2", pointX);
    pointMarker.setAttribute("cx", pointX);
    pointMarker.setAttribute("cy", pointY);
    tooltip.setAttribute("transform", `translate(${tooltipX}, ${tooltipY})`);
    dateText.textContent = `${date} · ${priceLabel}`;
    yieldText.textContent = yieldLabel;
    inspector.removeAttribute("hidden");
    inspector.setAttribute("aria-hidden", "false");
    svg.classList.add("is-inspecting");
    svg.setAttribute(
      "aria-label",
      `${security.name}，${date}，周收盘${priceLabel}，${yieldLabel}`
    );
  };

  const hide = () => {
    inspector.setAttribute("hidden", "");
    inspector.setAttribute("aria-hidden", "true");
    svg.classList.remove("is-inspecting");
  };

  const selectFromPointer = (event) => {
    const bounds = svg.getBoundingClientRect();
    const viewBox = svg.viewBox.baseVal;
    if (!bounds.width || !viewBox.width) return;
    const svgX = viewBox.x + ((event.clientX - bounds.left) / bounds.width) * viewBox.width;
    const ratio = (Math.max(left, Math.min(right, svgX)) - left) / (right - left);
    showIndex(Math.round(ratio * (history.length - 1)));
  };

  svg.addEventListener("pointerdown", (event) => {
    selectFromPointer(event);
    if (event.pointerType === "touch" && svg.setPointerCapture) {
      svg.setPointerCapture(event.pointerId);
    }
  });
  svg.addEventListener("pointermove", (event) => {
    if (
      event.pointerType === "touch" &&
      event.buttons === 0 &&
      !svg.hasPointerCapture?.(event.pointerId)
    ) {
      return;
    }
    selectFromPointer(event);
  });
  svg.addEventListener("pointerleave", (event) => {
    if (event.pointerType !== "touch") hide();
  });
  svg.addEventListener("focus", () => showIndex(selectedIndex));
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Home") showIndex(0);
    else if (event.key === "End") showIndex(history.length - 1);
    else showIndex(selectedIndex + (event.key === "ArrowLeft" ? -1 : 1));
  });
}
