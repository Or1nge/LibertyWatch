import { bindPriceChartInteraction, priceChart } from "./modules/chart.js";
import { hydrateIcons, icon } from "./modules/icons.js";
import { renderSafeMarkdown } from "./modules/safe_markdown.js";
import {
  alertStatus,
  badge,
  clamp,
  distanceClass,
  escapeHtml,
  filterSecurities,
  formatDateTime,
  formatMarketValue,
  formatMetric,
  formatNumber,
  formatPct,
  formatPrice,
  formatShortDate,
  heatLabel,
  initials,
  isFiniteNumber,
  marketLabel,
  sortSecurities,
  targetStatus,
  trendClass,
  valuationLabel,
  yieldToneClass
} from "./modules/presentation.js";

const main = document.querySelector("#app-main");
const sidebar = document.querySelector("#sidebar");
const sidebarScrim = document.querySelector("#sidebar-scrim");
const mobileMenuButton = document.querySelector("#mobile-menu-button");
const mobileMoreButton = document.querySelector("#mobile-more-button");
const refreshButton = document.querySelector("#refresh-button");
const drawer = document.querySelector("#detail-drawer");
const drawerBackdrop = document.querySelector("#drawer-backdrop");
const drawerCloseButton = document.querySelector("#drawer-close-button");
const drawerTitle = document.querySelector("#drawer-title");
const drawerBody = document.querySelector("#drawer-body");
const demoBanner = document.querySelector("#demo-banner");
const exitDemoButton = document.querySelector("#exit-demo-button");
const toastElement = document.querySelector("#toast");
const metricInfoPopover = document.querySelector("#metric-info-popover");
const mobileLayout = window.matchMedia("(max-width: 920px)");
const compactChartLayout = window.matchMedia("(max-width: 720px)");

const FILTER_DEFAULTS = {
  query: "",
  market: "",
  sector: "",
  status: "",
  valuation: "",
  alert: ""
};

const state = {
  data: null,
  error: null,
  loading: true,
  refreshing: false,
  demo: new URLSearchParams(location.search).get("demo") === "1",
  filters: { ...FILTER_DEFAULTS },
  sort: { key: "distance", direction: "asc" },
  opportunityView: "price",
  mobileFiltersOpen: false,
  nextRefreshAt: null,
  timer: null,
  toastTimer: null,
  drawerId: null,
  drawerReturnPath: "/watchlist",
  drawerTrigger: null,
  historyCache: new Map(),
  historyLoading: new Set(),
  historyErrors: new Map(),
  v2CompanyCache: new Map(),
  v2AnalysisCache: new Map(),
  v2PipelineStatus: null,
  v2PipelineLoading: false,
  v2Loading: new Set(),
  v2Errors: new Map(),
  metricInfoTrigger: null,
  metricInfoPinned: false,
  metricInfoCloseTimer: null,
  requestController: null,
  lastFetchFailedAt: null
};

const iconNames = [
  "grid",
  "list",
  "layers",
  "radar",
  "bell",
  "database",
  "book",
  "menu",
  "refresh",
  "more",
  "close",
  "flask"
];
iconNames.forEach(() => {});
hydrateIcons();
readUiStateFromUrl();
syncSidebarInert();

function readUiStateFromUrl() {
  const params = new URLSearchParams(location.search);
  state.demo = params.get("demo") === "1";
  state.filters = {
    query: params.get("q") ?? "",
    market: params.get("market") ?? "",
    sector: params.get("sector") ?? "",
    status: params.get("status") ?? "",
    valuation: params.get("valuation") ?? "",
    alert: params.get("alert") ?? ""
  };
  const allowedSorts = new Set([
    "name",
    "ticker",
    "market",
    "price",
    "change",
    "preferred",
    "distance",
    "yield",
    "updated"
  ]);
  state.sort.key = allowedSorts.has(params.get("sort"))
    ? params.get("sort")
    : "distance";
  state.sort.direction = params.get("dir") === "desc" ? "desc" : "asc";
  state.opportunityView = ["technical", "contrarian"].includes(
    params.get("view")
  )
    ? params.get("view")
    : "price";
}

function updateUrlState({ replace = true } = {}) {
  const params = new URLSearchParams();
  if (state.demo) params.set("demo", "1");
  if (state.filters.query) params.set("q", state.filters.query);
  if (state.filters.market) params.set("market", state.filters.market);
  if (state.filters.sector) params.set("sector", state.filters.sector);
  if (state.filters.status) params.set("status", state.filters.status);
  if (state.filters.valuation) {
    params.set("valuation", state.filters.valuation);
  }
  if (state.filters.alert) params.set("alert", state.filters.alert);
  if (state.sort.key !== "distance") params.set("sort", state.sort.key);
  if (state.sort.direction !== "asc") params.set("dir", state.sort.direction);
  if (
    location.pathname === "/opportunities" &&
    state.opportunityView !== "price"
  ) {
    params.set("view", state.opportunityView);
  }
  const query = params.toString();
  const next = `${location.pathname}${query ? `?${query}` : ""}`;
  history[replace ? "replaceState" : "pushState"](
    history.state,
    "",
    next
  );
}

function pathWithMode(path, { includeFilters = false } = {}) {
  const params = new URLSearchParams();
  if (state.demo) params.set("demo", "1");
  if (includeFilters) {
    if (state.filters.query) params.set("q", state.filters.query);
    if (state.filters.market) params.set("market", state.filters.market);
    if (state.filters.sector) params.set("sector", state.filters.sector);
    if (state.filters.status) params.set("status", state.filters.status);
    if (state.filters.valuation) {
      params.set("valuation", state.filters.valuation);
    }
    if (state.filters.alert) params.set("alert", state.filters.alert);
    if (state.sort.key !== "distance") params.set("sort", state.sort.key);
    if (state.sort.direction !== "asc") params.set("dir", state.sort.direction);
  }
  const query = params.toString();
  return `${path}${query ? `?${query}` : ""}`;
}

function safeDecode(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return null;
  }
}

function metricDefinition(metricId) {
  return (state.data?.metricDefinitionsV2 ?? []).find(
    (definition) => definition.id === metricId
  );
}

function metricInfoButton(metricId, currentStatus = "—") {
  const definition = metricDefinition(metricId);
  if (!definition) return "";
  return `
    <button class="metric-info-button" type="button"
      data-metric-info="${escapeHtml(metricId)}"
      data-metric-status="${escapeHtml(currentStatus ?? "—")}"
      aria-label="解释 ${escapeHtml(definition.label_zh)}"
      aria-controls="metric-info-popover" aria-expanded="false">!</button>
  `;
}

function metricHeader(label, metricId, currentStatus = "列表中各公司状态不同") {
  return `<span class="metric-header-label">${escapeHtml(label)}${metricInfoButton(
    metricId,
    currentStatus
  )}</span>`;
}

function v2Metric(security, metricId) {
  return security.shareholderReturnV2?.metrics?.[metricId] ?? null;
}

function v2Score(security, scoreId) {
  return security.shareholderReturnV2?.scores?.[scoreId] ?? null;
}

function publishedDisplay(record, fallback = "数据不足") {
  if (!record) return fallback;
  if (record.value === null || record.value === undefined) {
    return record.display || record.reason || fallback;
  }
  return record.display || String(record.value);
}

function dataStatusLabel(status) {
  return {
    VALID: "有效",
    PARTIAL: "数据不足",
    INVALID: "计算受阻",
    STALE: "数据更新受阻"
  }[status] ?? "等待披露";
}

function analysisStatusLabel(value) {
  const status = value?.status ?? value?.analysis_status?.status;
  return {
    SUCCEEDED: "已完成",
    PENDING: "排队中",
    RUNNING: "分析中",
    WAITING_RETRY: "等待重试",
    WAITING_MODEL: "等待模型",
    WAITING_AUTH: "等待认证",
    FAILED: "分析失败",
    INVALID_INPUT: "结构化输入无效",
    NOT_REQUESTED: "未触发"
  }[status] ?? "暂无报告";
}

function returnTypeLabel(value) {
  return {
    CASH_ANCHORED: "现金锚定型",
    GROWTH_SUPPLEMENTED: "增长补足型",
    BELOW_THRESHOLD: "未进入4%区间"
  }[value] ?? "暂不可分类";
}

function positionMetricInfoPopover(trigger) {
  if (!trigger || metricInfoPopover.hidden) return;
  const rect = trigger.getBoundingClientRect();
  const margin = 12;
  const width = Math.min(360, window.innerWidth - margin * 2);
  metricInfoPopover.style.width = `${width}px`;
  const height = metricInfoPopover.offsetHeight;
  let left = Math.min(
    window.innerWidth - width - margin,
    Math.max(margin, rect.right - width)
  );
  let top = rect.bottom + 8;
  if (top + height > window.innerHeight - margin) {
    top = Math.max(margin, rect.top - height - 8);
  }
  metricInfoPopover.style.left = `${left}px`;
  metricInfoPopover.style.top = `${top}px`;
}

function openMetricInfo(trigger, { pinned = false } = {}) {
  window.clearTimeout(state.metricInfoCloseTimer);
  const definition = metricDefinition(trigger.dataset.metricInfo);
  if (!definition) return;
  if (state.metricInfoTrigger && state.metricInfoTrigger !== trigger) {
    state.metricInfoTrigger.setAttribute("aria-expanded", "false");
  }
  state.metricInfoTrigger = trigger;
  state.metricInfoPinned = pinned;
  trigger.setAttribute("aria-expanded", "true");
  const caveats = (definition.caveats_zh ?? []).join("；") || "—";
  const applicability = (definition.applicability ?? []).join("、") || "—";
  metricInfoPopover.innerHTML = `
    <div class="metric-info-head">
      <div><small>指标解释</small><h3>${escapeHtml(definition.label_zh)}</h3></div>
      <button type="button" data-close-metric-info aria-label="关闭指标解释">×</button>
    </div>
    <p class="metric-info-summary">${escapeHtml(
      definition.simple_interpretation_zh
    )}</p>
    <dl>
      <div><dt>具体公式</dt><dd>${escapeHtml(definition.formula_symbolic)}</dd></div>
      <div><dt>通俗口径</dt><dd>${escapeHtml(definition.formula_plain_zh)}</dd></div>
      <div><dt>数据窗口</dt><dd>${escapeHtml(definition.data_window_zh)}</dd></div>
      <div><dt>高低理解</dt><dd>${escapeHtml(
        `${definition.good_range_zh} 警示：${definition.warning_range_zh}`
      )}</dd></div>
      <div><dt>适用行业</dt><dd>${escapeHtml(applicability)}</dd></div>
      <div><dt>主要局限</dt><dd>${escapeHtml(caveats)}</dd></div>
      <div><dt>当前版本</dt><dd>${escapeHtml(
        `${definition.version} · ${state.data?.shareholderReturnV2?.calculationVersion ?? definition.version}`
      )}</dd></div>
      <div><dt>当前数据状态</dt><dd>${escapeHtml(
        trigger.dataset.metricStatus || "—"
      )}</dd></div>
    </dl>
  `;
  metricInfoPopover.hidden = false;
  metricInfoPopover.setAttribute("aria-hidden", "false");
  requestAnimationFrame(() => positionMetricInfoPopover(trigger));
}

function closeMetricInfo({ force = false } = {}) {
  if (state.metricInfoPinned && !force) return;
  window.clearTimeout(state.metricInfoCloseTimer);
  state.metricInfoTrigger?.setAttribute("aria-expanded", "false");
  state.metricInfoTrigger = null;
  state.metricInfoPinned = false;
  metricInfoPopover.hidden = true;
  metricInfoPopover.setAttribute("aria-hidden", "true");
}

function scheduleMetricInfoClose() {
  window.clearTimeout(state.metricInfoCloseTimer);
  state.metricInfoCloseTimer = window.setTimeout(() => closeMetricInfo(), 120);
}

function currentRoute() {
  const path = location.pathname.replace(/\/+$/, "") || "/";
  if (path === "/") return { name: "overview" };
  if (path === "/watchlist") return { name: "watchlist" };
  if (path === "/sectors") return { name: "sectors" };
  if (path.startsWith("/sectors/")) {
    const sector = safeDecode(path.slice("/sectors/".length));
    if (sector === null) return { name: "not-found" };
    return {
      name: "sector-detail",
      sector
    };
  }
  if (path === "/opportunities") return { name: "opportunities" };
  if (path === "/alerts") return { name: "alerts" };
  if (path === "/data-status") return { name: "data-status" };
  if (path === "/methodology") return { name: "methodology" };
  if (path.startsWith("/securities/")) {
    const id = safeDecode(path.slice("/securities/".length));
    if (id === null) return { name: "not-found" };
    return {
      name: "security-detail",
      id
    };
  }
  return { name: "not-found" };
}

function navName(route = currentRoute()) {
  if (route.name === "sector-detail") return "sectors";
  if (route.name === "security-detail") return "watchlist";
  return route.name;
}

function setActiveNavigation() {
  const active = navName();
  document.querySelectorAll("[data-nav]").forEach((link) => {
    const selected = link.dataset.nav === active;
    link.classList.toggle("is-active", selected);
    if (selected) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

function closeSidebar() {
  document.body.classList.remove("is-sidebar-open");
  mobileMenuButton.setAttribute("aria-expanded", "false");
  syncSidebarInert();
}

function openSidebar() {
  document.body.classList.add("is-sidebar-open");
  mobileMenuButton.setAttribute("aria-expanded", "true");
  sidebar.removeAttribute("inert");
  requestAnimationFrame(() => sidebar.querySelector("a")?.focus());
}

function syncSidebarInert() {
  if (
    mobileLayout.matches &&
    !document.body.classList.contains("is-sidebar-open")
  ) {
    sidebar.setAttribute("inert", "");
  } else {
    sidebar.removeAttribute("inert");
  }
}

function navigate(path, { replace = false } = {}) {
  closeMetricInfo({ force: true });
  closeSidebar();
  closeDrawer({ changeHistory: false });
  state.mobileFiltersOpen = false;
  const destination = pathWithMode(path);
  history[replace ? "replaceState" : "pushState"]({}, "", destination);
  readUiStateFromUrl();
  renderRoute();
  window.scrollTo({ top: 0, behavior: "instant" });
}

function showToast(message) {
  window.clearTimeout(state.toastTimer);
  toastElement.textContent = message;
  toastElement.classList.add("is-visible");
  state.toastTimer = window.setTimeout(() => {
    toastElement.classList.remove("is-visible");
  }, 2800);
}

function snapshotScroll() {
  const table = document.querySelector(".table-scroll");
  const active = document.activeElement;
  const focusSelector =
    active?.dataset?.opportunityView
      ? `[data-opportunity-view="${CSS.escape(
          active.dataset.opportunityView
        )}"]`
      : active?.dataset?.sector
        ? `[data-sector="${CSS.escape(active.dataset.sector)}"]`
        : active?.dataset?.security
          ? `[data-security="${CSS.escape(active.dataset.security)}"]`
          : active?.dataset?.action
            ? `[data-action="${CSS.escape(active.dataset.action)}"]`
            : null;
  return {
    windowY: window.scrollY,
    tableTop: table?.scrollTop ?? 0,
    tableLeft: table?.scrollLeft ?? 0,
    focusSelector
  };
}

function restoreScroll(snapshot) {
  requestAnimationFrame(() => {
    window.scrollTo({ top: snapshot.windowY, behavior: "instant" });
    const table = document.querySelector(".table-scroll");
    if (table) {
      table.scrollTop = snapshot.tableTop;
      table.scrollLeft = snapshot.tableLeft;
    }
    if (snapshot.focusSelector) {
      document
        .querySelector(snapshot.focusSelector)
        ?.focus({ preventScroll: true });
    }
  });
}

function animateChange(element) {
  if (!element) return;
  element.classList.remove("value-changed");
  void element.offsetWidth;
  element.classList.add("value-changed");
  window.setTimeout(() => element.classList.remove("value-changed"), 950);
}

function newestQuoteTime(data) {
  const timestamps = (data?.securities ?? [])
    .map((security) => security.quote?.lastUpdatedAt ?? security.lastUpdate)
    .filter(Boolean)
    .map((value) => new Date(value))
    .filter((value) => !Number.isNaN(value.getTime()))
    .sort((left, right) => right - left);
  return timestamps[0]?.toISOString() ?? null;
}

function updateChrome() {
  const data = state.data;
  if (!data) return;
  const total = data.summary?.totalSecurities ?? data.securities?.length ?? 0;
  const isDemo = data.meta?.isDemo === true;
  const market = data.market ?? {};
  const interval =
    data.meta?.refreshIntervalMs && data.meta.refreshIntervalMs >= 30_000
      ? data.meta.refreshIntervalMs
      : 60_000;
  const successfulQuoteAt = new Date(
    data.meta?.lastQuoteRefreshSucceededAt ?? newestQuoteTime(data) ?? ""
  ).getTime();
  const collectorSnapshotAt = new Date(data.meta?.lastRefreshAt ?? "").getTime();
  const collectorStale =
    !isDemo &&
    total > 0 &&
    Number.isFinite(collectorSnapshotAt) &&
    Date.now() - collectorSnapshotAt > interval * 2;
  const stale =
    !isDemo &&
    (collectorStale ||
      (market.status === "open" &&
        Number.isFinite(successfulQuoteAt) &&
        Date.now() - successfulQuoteAt > interval * 2));
  const serverSnapshotError = Boolean(data.meta?.lastRefreshError);
  const fetchFailed =
    state.lastFetchFailedAt !== null || serverSnapshotError;
  const marketStatus = document.querySelector("#market-status");
  const statusClass =
    stale || fetchFailed
      ? "is-error"
      : isDemo || market.status === "unconfigured"
      ? "is-pending"
      : market.status === "open"
        ? "is-live"
        : market.status === "error"
          ? "is-error"
          : "is-paused";
  const marketText = isDemo
    ? "虚构演示"
    : fetchFailed
      ? `${
          serverSnapshotError ? "快照异常" : "刷新失败"
        } · 保留旧数据`
      : stale
        ? "行情已过期"
    : total === 0
      ? "行情未连接"
      : market.label ?? "状态未知";
  marketStatus.innerHTML = `
    <span class="status-dot ${statusClass}"></span>
    <span><small>市场状态</small><strong>${escapeHtml(marketText)}</strong></span>
  `;

  const sourceLabel = isDemo
    ? "虚构演示数据"
    : data.meta?.dataSource || (total ? "行情源待连接" : "等待配置");
  document.querySelector("#source-status-label").textContent = sourceLabel;
  const sourceDot = document.querySelector("#source-status-dot");
  sourceDot.className = `status-dot ${
    stale || fetchFailed
      ? "is-error"
      : data.meta?.isRealtime
        ? "is-live"
        : "is-pending"
  }`;
  document.querySelector("#nav-security-count").textContent = String(total);

  const quoteTime =
    data.meta?.lastQuoteRefreshSucceededAt || newestQuoteTime(data);
  document.querySelector("#quote-as-of").textContent = isDemo
    ? "固定演示快照"
    : formatDateTime(quoteTime);
  const refreshTime =
    data.meta?.lastQuoteRefreshSucceededAt ||
    data.meta?.lastRefreshAt ||
    data.meta?.lastConfigurationReloadAt;
  document.querySelector("#last-refresh").textContent =
    total === 0 && !isDemo
      ? "尚无"
      : formatDateTime(refreshTime, { timeOnly: true });

  demoBanner.hidden = !isDemo;
  document.title = `${data.meta?.title ?? "Liberty"} · Liberty`;

  const serverNext = new Date(data.meta?.nextRefreshAt ?? "").getTime();
  state.nextRefreshAt =
    Number.isFinite(serverNext) && serverNext > Date.now() + 1_000
      ? serverNext
      : Date.now() + interval;
  updateCountdown();
}

function updateCountdown() {
  const element = document.querySelector("#refresh-countdown");
  if (!state.data) {
    if (!state.nextRefreshAt) {
      element.textContent = "—";
      return;
    }
    const retryRemaining = Math.max(0, state.nextRefreshAt - Date.now());
    const retrySeconds = Math.ceil(retryRemaining / 1000);
    element.textContent = `重试 · 00:${String(retrySeconds).padStart(2, "0")}`;
    if (retryRemaining <= 0 && !state.refreshing) {
      void loadData({ initial: false, reason: "scheduled" });
    }
    return;
  }
  const total = state.data.summary?.totalSecurities ?? 0;
  const remaining = Math.max(0, (state.nextRefreshAt ?? Date.now()) - Date.now());
  const totalSeconds = Math.ceil(remaining / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  const clock = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(
    2,
    "0"
  )}`;
  if (total === 0 && !state.data.meta?.isDemo) {
    element.textContent = `等待配置 · ${clock}`;
    if (remaining <= 0 && !state.refreshing) {
      void loadData({ initial: false, reason: "scheduled" });
    }
    return;
  }
  element.textContent = clock;
  if (remaining <= 0 && !state.refreshing) {
    void loadData({ initial: false, reason: "scheduled" });
  }
}

function startCountdown() {
  window.clearInterval(state.timer);
  state.timer = window.setInterval(updateCountdown, 1_000);
}

async function loadData({ initial = false, reason = "manual" } = {}) {
  if (state.refreshing) return;
  state.refreshing = true;
  refreshButton.classList.add("is-spinning");
  state.requestController?.abort();
  const controller = new AbortController();
  state.requestController = controller;
  let didTimeout = false;
  const requestTimeout = window.setTimeout(() => {
    didTimeout = true;
    controller.abort();
  }, 15_000);
  const previous = state.data;
  try {
    const params = state.demo ? "?demo=1" : "";
    const response = await fetch(`/api/watchlist${params}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: controller.signal
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => null);
      throw new Error(
        detail?.detail || detail?.error?.message || `服务返回 ${response.status}`
      );
    }
    const payload = await response.json();
    if (!Array.isArray(payload.securities) || !payload.meta) {
      throw new Error("行情快照格式不完整");
    }
    if (
      previous?.meta?.historyGeneratedAt !==
      payload.meta.historyGeneratedAt
    ) {
      state.historyCache.clear();
      state.historyErrors.clear();
    }
    if (
      previous?.shareholderReturnV2?.releaseId !==
        payload.shareholderReturnV2?.releaseId ||
      previous?.shareholderReturnV2?.analysisReleaseId !==
        payload.shareholderReturnV2?.analysisReleaseId
    ) {
      state.v2CompanyCache.clear();
      state.v2AnalysisCache.clear();
      state.v2Errors.clear();
      state.v2PipelineStatus = null;
    }
    state.data = payload;
    state.error = null;
    state.lastFetchFailedAt = null;
    state.loading = false;
    updateChrome();
    if (initial || !previous) {
      renderRoute();
    } else {
      patchOrRender(previous, payload);
      if (reason === "manual") showToast("已检查最新快照");
    }
  } catch (error) {
    if (error.name === "AbortError" && !didTimeout) return;
    const effectiveError = didTimeout
      ? new Error("数据请求超过 15 秒")
      : error;
    state.error = effectiveError;
    state.lastFetchFailedAt = new Date();
    state.loading = false;
    if (!state.data) renderRoute();
    else updateChrome();
    showToast(`刷新失败：${effectiveError.message}`);
    state.nextRefreshAt = Date.now() + 30_000;
  } finally {
    window.clearTimeout(requestTimeout);
    state.refreshing = false;
    refreshButton.classList.remove("is-spinning");
  }
}

function patchText(selector, text, classNames = null) {
  document.querySelectorAll(selector).forEach((element) => {
    if (element.textContent !== text) {
      element.textContent = text;
      if (classNames) element.className = classNames;
      animateChange(element);
    } else if (classNames) {
      element.className = classNames;
    }
  });
}

function patchHtml(selector, html) {
  document.querySelectorAll(selector).forEach((element) => {
    if (element.innerHTML !== html) {
      element.innerHTML = html;
      animateChange(element);
    }
  });
}

function structuralSignature(security) {
  if (!security) return "missing";
  return JSON.stringify({
    name: security.name,
    ticker: security.ticker,
    market: security.market,
    currency: security.currency,
    sector: security.sector,
    industry: security.industry,
    targetPrices: security.targetPrices,
    expectedDividendYieldPct: security.expectedDividendYieldPct,
    valuationStatus: security.valuationStatus,
    investmentThesis: security.investmentThesis,
    risks: security.risks,
    notes: security.notes,
    targetRevisionHistory: security.targetRevisionHistory,
    history: security.history,
    technicalIndicators: security.technicalIndicators,
    shareholderReturnV2: security.shareholderReturnV2
  });
}

function reconcileDrawerAfterRefresh(data) {
  if (!state.drawerId) return;
  const selected = data.securities.find(
    (security) => security.id === state.drawerId
  );
  if (selected) {
    renderDrawer(selected);
    void loadSecurityHistory(selected.id);
  } else {
    closeDrawer();
  }
}

function patchOrRender(previous, next) {
  const route = currentRoute();
  const previousIds = previous.securities.map((security) => security.id).sort();
  const nextIds = next.securities.map((security) => security.id).sort();
  const sameUniverse =
    previousIds.length === nextIds.length &&
    previousIds.every((id, index) => id === nextIds[index]);
  if (
    !sameUniverse ||
    !["overview", "watchlist", "sector-detail", "security-detail"].includes(
      route.name
    )
  ) {
    reconcileDrawerAfterRefresh(next);
    renderRoute({ preserveScroll: true });
    return;
  }
  const previousById = new Map(
    previous.securities.map((security) => [security.id, security])
  );
  const structuralChange = next.securities.some(
    (security) =>
      structuralSignature(previousById.get(security.id)) !==
      structuralSignature(security)
  );
  if (structuralChange) {
    reconcileDrawerAfterRefresh(next);
    renderRoute({ preserveScroll: true });
    return;
  }

  const summary = next.summary;
  const kpis = {
    reached: summary.reachedTargetCount,
    within3: summary.within3PctCount,
    within10: summary.within10PctCount,
    coverage: summary.priceAvailableCount
  };
  Object.entries(kpis).forEach(([key, value]) => {
    patchText(`[data-kpi="${key}"]`, String(value ?? "—"));
  });

  for (const security of next.securities) {
    const id = CSS.escape(security.id);
    const currentPrice = security.quote?.currentPrice ?? security.currentPrice;
    const change = security.quote?.dailyChangePct ?? security.dailyChangePct;
    const distance = security.derived?.distanceToPreferredPct;
    const quoteTime = security.quote?.lastUpdatedAt ?? security.lastUpdate;
    patchHtml(
      `[data-security-id="${id}"][data-field="price"]`,
      priceMarkup(security, currentPrice)
    );
    patchText(
      `[data-security-id="${id}"][data-field="change"]`,
      formatPct(change, { signed: true }),
      trendClass(change)
    );
    patchText(
      `[data-security-id="${id}"][data-field="distance"]`,
      formatPct(distance, { signed: true }),
      `distance ${distanceClass(distance)}`
    );
    patchText(
      `[data-security-id="${id}"][data-field="shareholder-yield"]`,
      formatPct(security.currentShareholderYieldPct)
    );
    patchText(
      `[data-security-id="${id}"][data-field="updated"]`,
      security.quote?.status === "fictional"
        ? "演示快照"
        : formatDateTime(quoteTime, { timeOnly: true })
    );
    const target = targetStatus(security.derived?.targetStatus);
    const valuation = valuationBadgeInfo(security.valuationStatus);
    const alert = alertStatus(security.derived?.alertStatus);
    patchHtml(
      `[data-security-id="${id}"][data-field="target"]`,
      badge(target.label, target.className)
    );
    patchHtml(
      `[data-security-id="${id}"][data-field="valuation"]`,
      statusFilterBadge(
        "valuation",
        security.valuationStatus,
        valuation,
        security.name
      )
    );
    patchHtml(
      `[data-security-id="${id}"][data-field="alert"]`,
      statusFilterBadge(
        "alert",
        security.derived?.alertStatus,
        alert,
        security.name
      )
    );
  }

  const routeSource =
    route.name === "sector-detail"
      ? next.securities.filter(
          (security) => security.sector === route.sector
        )
      : next.securities;
  const filtered = visibleSecurities(routeSource);
  const filteredIds = filtered.map((security) => security.id);
  const tbody = document.querySelector("tbody[data-security-rows]");
  const mobileList = document.querySelector("[data-mobile-security-rows]");
  const rowIds = Array.from(tbody?.children ?? []).map(
    (row) => row.dataset.rowId
  );
  const sameRows =
    rowIds.length === filteredIds.length &&
    [...new Set(rowIds)].length === filteredIds.length &&
    filteredIds.every((id) => rowIds.includes(id));
  if (!sameRows) {
    renderRoute({ preserveScroll: true });
  } else {
    filteredIds.forEach((id) => {
      const row = tbody?.querySelector(`[data-row-id="${CSS.escape(id)}"]`);
      if (row) tbody.append(row);
      const card = mobileList?.querySelector(
        `[data-row-id="${CSS.escape(id)}"]`
      );
      if (card) mobileList.append(card);
    });
  }

  if (state.drawerId) {
    const selected = next.securities.find(
      (security) => security.id === state.drawerId
    );
    if (selected) {
      renderDrawer(selected);
      void loadSecurityHistory(selected.id);
    }
  }
}

function renderRoute({ preserveScroll = false } = {}) {
  setActiveNavigation();
  if (state.loading) return;
  const scroll = preserveScroll ? snapshotScroll() : null;
  if (state.error && !state.data) {
    main.innerHTML = errorPage();
  } else {
    const route = currentRoute();
    switch (route.name) {
      case "overview":
        main.innerHTML = overviewPage();
        break;
      case "watchlist":
        main.innerHTML = watchlistPage();
        break;
      case "sectors":
        main.innerHTML = sectorsPage();
        break;
      case "sector-detail":
        main.innerHTML = sectorDetailPage(route.sector);
        break;
      case "opportunities":
        main.innerHTML = opportunitiesPage();
        break;
      case "alerts":
        main.innerHTML = alertsPage();
        break;
      case "data-status":
        main.innerHTML = dataStatusPage();
        void loadV2PipelineStatus();
        break;
      case "methodology":
        main.innerHTML = methodologyPage();
        break;
      case "security-detail":
        main.innerHTML = watchlistPage();
        requestAnimationFrame(() => openDrawer(route.id, { push: false }));
        break;
      default:
        main.innerHTML = notFoundPage();
    }
  }
  hydrateIcons(main);
  if (scroll) restoreScroll(scroll);
}

function pageHead(eyebrow, title, description, actions = "") {
  return `
    <header class="page-head">
      <div>
        <p class="eyebrow">${escapeHtml(eyebrow)}</p>
        <h1>${escapeHtml(title)}</h1>
        ${description ? `<p>${escapeHtml(description)}</p>` : ""}
      </div>
      ${actions ? `<div class="page-actions">${actions}</div>` : ""}
    </header>
  `;
}

function summaryCards() {
  const summary = state.data.summary;
  const total = summary.totalSecurities ?? 0;
  const empty = total === 0;
  const value = (number) => (empty ? "—" : String(number ?? 0));
  const coverage = summary.priceAvailableCount ?? 0;
  return `
    <section class="summary-grid" aria-label="观察清单摘要">
      <article class="metric-card">
        <div class="metric-topline">
          <span class="metric-label">已到理想价</span>
          <span class="metric-actions">${metricInfoButton(
            "target_reached_count_v1",
            "v1兼容统计"
          )}<span class="metric-icon">${icon("target")}</span></span>
        </div>
        <div class="metric-value"><span data-kpi="reached">${value(
          summary.reachedTargetCount
        )}</span><small>只</small></div>
      </article>
      <article class="metric-card is-amber">
        <div class="metric-topline">
          <span class="metric-label">距理想价 0–3%</span>
          <span class="metric-actions">${metricInfoButton(
            "target_near_3_count_v1",
            "v1兼容统计"
          )}<span class="metric-icon">${icon("radar")}</span></span>
        </div>
        <div class="metric-value"><span data-kpi="within3">${value(
          summary.within3PctCount
        )}</span><small>只</small></div>
      </article>
      <article class="metric-card is-amber">
        <div class="metric-topline">
          <span class="metric-label">距理想价 3–10%</span>
          <span class="metric-actions">${metricInfoButton(
            "target_near_10_count_v1",
            "v1兼容统计"
          )}<span class="metric-icon">${icon("trend")}</span></span>
        </div>
        <div class="metric-value"><span data-kpi="within10">${value(
          summary.within10PctCount
        )}</span><small>只</small></div>
      </article>
      <article class="metric-card">
        <div class="metric-topline">
          <span class="metric-label">行情覆盖</span>
          <span class="metric-actions">${metricInfoButton(
            "quote_coverage_count",
            `${coverage}/${total}`
          )}<span class="metric-icon">${icon("database")}</span></span>
        </div>
        <div class="metric-value"><span data-kpi="coverage">${value(
          coverage
        )}</span><small>/ ${total}</small></div>
      </article>
    </section>
  `;
}

function emptyWatchlist({ compact = false, filtered = false } = {}) {
  const title = filtered ? "没有符合筛选条件的标的" : "等待观察名单";
  const copy = filtered
    ? "尝试清除部分筛选条件，当前排序和筛选会在刷新后保留。"
    : "表格结构、每分钟检查和数据推送链路已经就绪。载入标的后，系统会按股息回购率自动计算目标价。";
  return `
    <div class="empty-state ${compact ? "compact" : ""}">
      <div>
        <div class="empty-illustration">${icon(filtered ? "search" : "list")}</div>
        <h3>${title}</h3>
        <p>${copy}</p>
        <div class="empty-actions">
          ${
            filtered
              ? '<button class="button" type="button" data-action="clear-filters">清除筛选</button>'
              : `
                <button class="button button-primary" type="button" data-action="enter-demo">
                  ${icon("flask")} 查看界面演示
                </button>
                <a class="button" href="/data-status" data-route>查看接入状态</a>
              `
          }
        </div>
      </div>
    </div>
  `;
}

function filterMarkup() {
  const markets = Array.from(
    new Set(state.data.securities.map((security) => security.market))
  ).sort();
  const sectors = Array.from(
    new Set(state.data.securities.map((security) => security.sector))
  ).sort((left, right) => left.localeCompare(right, "zh-CN"));
  const option = (value, label, current) =>
    `<option value="${escapeHtml(value)}" ${
      value === current ? "selected" : ""
    }>${escapeHtml(label)}</option>`;
  return `
    <div class="filters ${
      state.mobileFiltersOpen ? "is-mobile-open" : ""
    }" id="watchlist-filters">
      <label class="field">
        <span data-icon="search"></span>
        <span class="sr-only">搜索标的</span>
        <input type="search" data-filter="query" value="${escapeHtml(
          state.filters.query
        )}" placeholder="搜索名称或代码" autocomplete="off" />
      </label>
      <label class="field field-select">
        <span class="sr-only">筛选市场</span>
        <select data-filter="market">
          ${option("", "全部市场", state.filters.market)}
          ${markets
            .map((market) =>
              option(market, marketLabel(market), state.filters.market)
            )
            .join("")}
        </select>
      </label>
      <label class="field field-select">
        <span class="sr-only">筛选行业</span>
        <select data-filter="sector">
          ${option("", "全部行业", state.filters.sector)}
          ${sectors
            .map((sector) => option(sector, sector, state.filters.sector))
            .join("")}
        </select>
      </label>
      <label class="field field-select">
        <span class="sr-only">筛选价格状态</span>
        <select data-filter="status">
          ${option("", "全部目标状态", state.filters.status)}
          ${option("reached", "已到理想价", state.filters.status)}
          ${option("within_3", "距目标 ≤ 3%", state.filters.status)}
          ${option("within_10", "距目标 ≤ 10%", state.filters.status)}
          ${option("far", "继续等待", state.filters.status)}
          ${option("unconfigured", "未设目标", state.filters.status)}
        </select>
      </label>
      <label class="field field-select">
        <span class="sr-only">筛选估值状态</span>
        <select data-filter="valuation">
          ${option("", "全部估值", state.filters.valuation)}
          ${option("deeply_attractive", "深度价值（≥5%）", state.filters.valuation)}
          ${option("attractive", "具吸引力（≥4%）", state.filters.valuation)}
          ${option("fair", "合理（≥3%）", state.filters.valuation)}
          ${option("expensive", "偏贵（<3%）", state.filters.valuation)}
        </select>
      </label>
      <label class="field field-select">
        <span class="sr-only">筛选提醒状态</span>
        <select data-filter="alert">
          ${option("", "全部提醒", state.filters.alert)}
          ${option("buy_zone", "已触发", state.filters.alert)}
          ${option("approaching", "即将触发", state.filters.alert)}
          ${option("watch", "关注中", state.filters.alert)}
          ${option("none", "未触发", state.filters.alert)}
        </select>
      </label>
    </div>
  `;
}

function visibleSecurities(source = state.data.securities) {
  return sortSecurities(
    filterSecurities(source, state.filters),
    state.sort.key,
    state.sort.direction
  );
}

function sortableHeader(label, key, numeric = false, metricId = null) {
  const active = state.sort.key === key;
  const ariaSort = active
    ? state.sort.direction === "asc"
      ? "ascending"
      : "descending"
    : "none";
  const glyph = active ? (state.sort.direction === "asc" ? "↑" : "↓") : "↕";
  return `
    <th class="${numeric ? "is-numeric" : ""}" aria-sort="${ariaSort}">
      <span class="sortable-header-wrap">
        <button class="sort-button ${active ? "is-active" : ""}" type="button"
          data-sort="${key}">
          ${escapeHtml(label)} <span class="sort-glyph">${glyph}</span>
        </button>
        ${metricId ? metricInfoButton(metricId, "列表中各公司状态不同") : ""}
      </span>
    </th>
  `;
}

function valuationBadgeInfo(status) {
  const token = String(status ?? "unconfigured").toLowerCase();
  const label = valuationLabel(status);
  if (
    [
      "deeply_attractive",
      "deeply-undervalued",
      "attractive",
      "undervalued",
      "深度低估",
      "低估"
    ].includes(token)
  ) {
    return { label, className: "is-reached" };
  }
  if (["fair", "合理"].includes(token)) {
    return { label, className: "is-watch" };
  }
  if (["expensive", "overvalued", "高估"].includes(token)) {
    return { label, className: "is-near" };
  }
  return { label, className: "is-missing" };
}

function priceMarkup(security, currentPrice) {
  const currentCny =
    security.quote?.currentPriceCny ?? security.currentPriceCny;
  const shareholderYield = security.currentShareholderYieldPct;
  const tone = yieldToneClass(shareholderYield);
  const title = isFiniteNumber(shareholderYield)
    ? `当前股息回购率 ${formatPct(shareholderYield)}；收益率越高，颜色越深`
    : "当前股息回购率数据不足";
  const cny =
    security.currency === "HKD" && isFiniteNumber(currentCny)
      ? `<small class="price-cny">≈ ${formatPrice(currentCny, "CNY")}</small>`
      : "";
  return `
    <span class="price-stack" title="${escapeHtml(title)}">
      <span class="price price-yield ${tone}">${formatPrice(
        currentPrice,
        security.currency
      )}</span>
      ${cny}
    </span>
  `;
}

function targetPriceMarkup(security, key) {
  const local = security.targetPrices?.[key];
  const cny = security.targetPricesCny?.[key];
  return `
    <span class="target-price-stack">
      <strong>${formatPrice(local, security.currency)}</strong>
      ${
        security.currency === "HKD" && isFiniteNumber(cny)
          ? `<small>≈ ${formatPrice(cny, "CNY")}</small>`
          : ""
      }
    </span>
  `;
}

function statusFilterBadge(kind, value, info, securityName = "") {
  const normalized = String(value ?? "");
  const active = state.filters[kind] === normalized;
  const kindLabel = kind === "valuation" ? "估值" : "提醒";
  return `
    <button class="badge status-filter-badge ${info.className} ${
      active ? "is-filter-active" : ""
    }" type="button" data-status-filter="${escapeHtml(
      kind
    )}" data-status-value="${escapeHtml(normalized)}"
      aria-pressed="${active}" title="筛选${escapeHtml(
        kindLabel
      )}状态：${escapeHtml(info.label)}">
      ${escapeHtml(info.label)}
    </button>
  `;
}

function v2TableMetric(security, metricId, { score = false } = {}) {
  const record = score ? v2Score(security, metricId) : v2Metric(security, metricId);
  return `<span class="published-metric is-${escapeHtml(
    String(record?.status ?? "missing").toLowerCase()
  )}">${escapeHtml(publishedDisplay(record))}</span>`;
}

function securityRow(security) {
  const id = escapeHtml(security.id);
  const currentPrice = security.quote?.currentPrice ?? security.currentPrice;
  const change = security.quote?.dailyChangePct ?? security.dailyChangePct;
  const distance = security.derived?.distanceToPreferredPct;
  const valuation = valuationBadgeInfo(security.valuationStatus);
  const alert = alertStatus(security.derived?.alertStatus);
  const updated = security.quote?.lastUpdatedAt ?? security.lastUpdate;
  return `
    <tr data-row-id="${id}" data-security="${id}">
      <td>
        <button class="security-name security-link" type="button"
          data-security="${id}" aria-label="查看 ${escapeHtml(security.name)} 详情">
          <span class="security-avatar">${escapeHtml(initials(security.name))}</span>
          <span>
            <strong>${escapeHtml(security.name)}</strong>
            <small>${escapeHtml(security.sector)} · ${escapeHtml(
              security.industry
            )}</small>
          </span>
        </button>
      </td>
      <td><span class="ticker">${escapeHtml(security.ticker || "—")}</span></td>
      <td>${escapeHtml(marketLabel(security.market))}</td>
      <td class="is-numeric"><span data-security-id="${id}" data-field="price">${priceMarkup(
        security,
        currentPrice
      )}</span></td>
      <td class="is-numeric"><span class="${trendClass(
        change
      )}" data-security-id="${id}" data-field="change">${formatPct(change, {
        signed: true
      })}</span></td>
      <td class="is-numeric">${targetPriceMarkup(security, "preferred")}</td>
      <td class="is-numeric"><span class="distance ${distanceClass(
        distance
      )}" data-security-id="${id}" data-field="distance">${formatPct(distance, {
        signed: true
      })}</span></td>
      <td class="is-numeric"><span data-security-id="${id}" data-field="shareholder-yield">${formatPct(
        security.currentShareholderYieldPct
      )}</span></td>
      <td class="is-numeric">${v2TableMetric(security, "raw_2y_shareholder_yield")}</td>
      <td class="is-numeric">${v2TableMetric(security, "sustainable_shareholder_yield")}</td>
      <td class="is-numeric">${v2TableMetric(security, "conservative_return_10y")}</td>
      <td class="is-numeric">${escapeHtml(
        publishedDisplay(
          security.shareholderReturnV2?.security_metrics?.[security.id]?.price_at_4pct
        )
      )}</td>
      <td class="is-numeric">${v2TableMetric(security, "recommendation_index", {
        score: true
      })}</td>
      <td class="is-numeric">${v2TableMetric(security, "entry_risk_index", {
        score: true
      })}</td>
      <td>${v2TableMetric(security, "distribution_trend")}</td>
      <td><span class="return-type is-${escapeHtml(
        String(security.shareholderReturnV2?.return_type ?? "unknown").toLowerCase()
      )}">${escapeHtml(
        returnTypeLabel(security.shareholderReturnV2?.return_type)
      )}</span></td>
      <td><span class="data-status is-${escapeHtml(
        String(security.shareholderReturnV2?.data_status ?? "missing").toLowerCase()
      )}">${escapeHtml(
        dataStatusLabel(security.shareholderReturnV2?.data_status)
      )}</span></td>
      <td>${escapeHtml(
        analysisStatusLabel(security.shareholderReturnV2?.analysis)
      )}</td>
      <td><span data-security-id="${id}" data-field="valuation">${statusFilterBadge(
        "valuation",
        security.valuationStatus,
        valuation,
        security.name
      )}</span></td>
      <td><span data-security-id="${id}" data-field="alert">${statusFilterBadge(
        "alert",
        security.derived?.alertStatus,
        alert,
        security.name
      )}</span></td>
      <td data-security-id="${id}" data-field="updated">${
        security.quote?.status === "fictional"
          ? "演示快照"
          : formatDateTime(updated, { timeOnly: true })
      }</td>
    </tr>
  `;
}

function mobileSecurityCard(security) {
  const id = escapeHtml(security.id);
  const currentPrice = security.quote?.currentPrice ?? security.currentPrice;
  const change = security.quote?.dailyChangePct ?? security.dailyChangePct;
  const distance = security.derived?.distanceToPreferredPct;
  const target = targetStatus(security.derived?.targetStatus);
  return `
    <article class="mobile-security-card" data-row-id="${id}" data-security="${id}"
      tabindex="0" role="button">
      <div class="mobile-card-head">
        <div class="security-name">
          <span class="security-avatar">${escapeHtml(initials(security.name))}</span>
          <span>
            <strong>${escapeHtml(security.name)}</strong>
            <small>${escapeHtml(security.ticker)} · ${escapeHtml(
              marketLabel(security.market)
            )}</small>
          </span>
        </div>
        <span data-security-id="${id}" data-field="target">${badge(
          target.label,
          target.className
        )}</span>
      </div>
      <div class="mobile-card-values mobile-card-primary-values">
        <span class="mobile-card-value">
          <small>现价</small>
          <strong data-security-id="${id}" data-field="price">${priceMarkup(
            security,
            currentPrice
          )}</strong>
        </span>
        <span class="mobile-card-value">
          <small>日涨跌</small>
          <strong class="${trendClass(
            change
          )}" data-security-id="${id}" data-field="change">${formatPct(change, {
            signed: true
          })}</strong>
        </span>
        <span class="mobile-card-value">
          <small>距理想价</small>
          <strong class="distance ${distanceClass(
            distance
          )}" data-security-id="${id}" data-field="distance">${formatPct(
            distance,
            { signed: true }
          )}</strong>
        </span>
        <span class="mobile-card-value is-core-return">
          <small>股息回购率</small>
          <strong data-security-id="${id}" data-field="shareholder-yield">${formatPct(
            security.currentShareholderYieldPct
          )}</strong>
        </span>
      </div>
      <div class="mobile-card-values mobile-card-v2-values">
        <span class="mobile-card-value">
          <small>${metricHeader("SSY", "sustainable_shareholder_yield")}</small>
          <strong>${escapeHtml(
            publishedDisplay(v2Metric(security, "sustainable_shareholder_yield"))
          )}</strong>
        </span>
        <span class="mobile-card-value">
          <small>${metricHeader("RI", "recommendation_index")}</small>
          <strong>${escapeHtml(
            publishedDisplay(v2Score(security, "recommendation_index"))
          )}</strong>
        </span>
        <span class="mobile-card-value">
          <small>${metricHeader("ERI", "entry_risk_index")}</small>
          <strong>${escapeHtml(
            publishedDisplay(v2Score(security, "entry_risk_index"))
          )}</strong>
        </span>
      </div>
    </article>
  `;
}

function watchlistPanel({
  title = "观察清单",
  compact = false,
  source = state.data.securities
} = {}) {
  const securities = visibleSecurities(source);
  const total = source.length;
  const rows = securities.map(securityRow).join("");
  const cards = securities.map(mobileSecurityCard).join("");
  return `
    <section class="panel">
      <div class="panel-header">
        <div class="panel-heading">
          <h2>${escapeHtml(title)}</h2>
          <p>默认按距理想价由近到远排序 · 刷新不会重置当前视图</p>
        </div>
        <button class="button mobile-filter-button" type="button" data-action="toggle-filters"
          aria-expanded="${state.mobileFiltersOpen}" aria-controls="watchlist-filters">
          ${icon("search")} 筛选
        </button>
        ${filterMarkup()}
      </div>
      <div class="watchlist-disclaimer" role="note">
        该系统用于筛选和风险研究，不构成收益保证。CR10 是保守情景下的估算基准，不是锁定收益；Codex 报告属于定性风险研究，不替代结构化财务数据。
      </div>
      ${
        securities.length
          ? `
            <div class="table-scroll">
              <table class="watchlist-table">
                <thead>
                  <tr>
                    ${sortableHeader("证券名称", "name")}
                    ${sortableHeader("代码", "ticker")}
                    ${sortableHeader("市场", "market")}
                    ${sortableHeader("现价", "price", true, "current_price")}
                    ${sortableHeader("日涨跌", "change", true, "daily_price_change")}
                    ${sortableHeader("理想价v1", "preferred", true, "legacy_preferred_price_v1")}
                    ${sortableHeader("距理想价v1", "distance", true, "legacy_distance_to_preferred_v1")}
                    ${sortableHeader("回报率v1", "yield", true, "legacy_shareholder_yield_v1")}
                    <th>${metricHeader("原始两年", "raw_2y_shareholder_yield")}</th>
                    <th>${metricHeader("SSY", "sustainable_shareholder_yield")}</th>
                    <th>${metricHeader("CR10", "conservative_return_10y")}</th>
                    <th>${metricHeader("4%价格线", "security_price_at_4pct")}</th>
                    <th>${metricHeader("RI", "recommendation_index")}</th>
                    <th>${metricHeader("ERI", "entry_risk_index")}</th>
                    <th>${metricHeader("分配趋势", "distribution_trend")}</th>
                    <th>${metricHeader("4%原因", "return_type_reason")}</th>
                    <th>${metricHeader("数据状态", "data_quality_status")}</th>
                    <th>${metricHeader("Codex", "codex_risk_report")}</th>
                    <th>${metricHeader("估值状态v1", "legacy_valuation_status_v1")}</th>
                    <th>${metricHeader("提醒状态v1", "legacy_alert_status_v1")}</th>
                    ${sortableHeader("行情更新", "updated", false, "quote_updated_at")}
                  </tr>
                </thead>
                <tbody data-security-rows>${rows}</tbody>
              </table>
            </div>
            <div class="mobile-security-list" data-mobile-security-rows>${cards}</div>
          `
          : emptyWatchlist({ compact, filtered: total > 0 })
      }
      <footer class="table-footer">
        <span>显示 <strong>${securities.length}</strong> / ${total || 0} 只标的</span>
        <span>${state.data.meta?.isRealtime ? "实时行情" : "只读快照"} · 每分钟检查</span>
      </footer>
    </section>
  `;
}

function overviewPage() {
  return `
    ${pageHead(
      "Investment watch",
      "等待好价格",
      "",
      `<a class="button" href="/data-status" data-route>${icon(
        "database"
      )} 数据状态</a>`
    )}
    ${summaryCards()}
    ${watchlistPanel({ title: "最接近理想价的标的", compact: true })}
  `;
}

function watchlistPage() {
  return `
    ${pageHead(
      "Watchlist",
      "全部观察标的",
      "港股同时显示人民币参考价；4% 股息回购率对应理想目标价，股价颜色随当前股息回购率加深。",
      `<button class="button" type="button" data-action="manual-refresh">${icon(
        "refresh"
      )} 检查快照</button>`
    )}
    ${summaryCards()}
    ${watchlistPanel()}
  `;
}

function sectorsPage() {
  const sectors = state.data.sectors ?? [];
  return `
    ${pageHead(
      "Sector ranking",
      "观察池行业排行",
      "只比较当前观察池内的行业相对状态，不代表全市场。点击行业可查看热度拆解与对应标的。",
      `<a class="button" href="/methodology" data-route>${icon(
        "book"
      )} 查看口径</a>`
    )}
    <div class="insight-banner">
      <span data-icon="info"></span>
      <span><strong>行业热度需要历史覆盖。</strong> 至少 3 个独立发行人且有效历史覆盖不低于 80% 才参与排名；数据不足时保持空白，不以单日涨跌替代。</span>
    </div>
    ${
      sectors.length
        ? `<section class="sector-grid">${sectors
            .map(sectorCard)
            .join("")}</section>`
        : `<section class="panel">${emptySectorState()}</section>`
    }
  `;
}

function sectorCard(sector) {
  const heat = sector.heatScore;
  const hotClass = isFiniteNumber(heat) && heat >= 60 ? "is-hot" : "";
  return `
    <article class="sector-card ${hotClass}" data-sector="${escapeHtml(
      sector.sector
    )}" tabindex="0" role="link">
      <div class="sector-card-top">
        <div>
          <span class="sector-rank">#${sector.rank ?? "—"} · ${
            sector.issuerCount ?? 0
          } 家发行人</span>
          <h3>${escapeHtml(sector.sector)}</h3>
        </div>
        <span class="heat-score">
          <strong>${isFiniteNumber(heat) ? formatNumber(heat, 0) : "—"}</strong>
          <small>${heatLabel(heat)}</small>
        </span>
      </div>
      <div class="heat-bar"><span style="width:${clamp(
        heat ?? 0,
        0,
        100
      )}%"></span></div>
      <div class="sector-stats">
        <span><small>行业内标的</small><strong>${sector.securityCount ?? 0} 只</strong></span>
        <span><small>已到目标</small><strong>${sector.reachedTargetCount ?? 0} 只</strong></span>
        <span><small>平均距离</small><strong>${formatPct(
          sector.averageDistanceToPreferredPct,
          { signed: true }
        )}</strong></span>
      </div>
      <span class="sector-card-arrow">${icon("arrow")}</span>
    </article>
  `;
}

function emptySectorState() {
  return `
    <div class="empty-state">
      <div>
        <div class="empty-illustration">${icon("layers")}</div>
        <h3>还没有可计算的行业</h3>
        <p>确定观察名单和行业映射后，这里会展示观察池内的行业热度、覆盖率、到价数量与对应标的。</p>
        <div class="empty-actions">
          <button class="button button-primary" type="button" data-action="enter-demo">${icon(
            "flask"
          )} 查看界面演示</button>
          <a class="button" href="/methodology" data-route>查看行业热度规则</a>
        </div>
      </div>
    </div>
  `;
}

function sectorDetailPage(sectorName) {
  const sector = (state.data.sectors ?? []).find(
    (item) => item.sector === sectorName
  );
  const securities = state.data.securities.filter(
    (security) => security.sector === sectorName
  );
  if (!sector && !securities.length) return notFoundPage("没有找到这个行业");
  const breakdown = sector?.heatComponents ?? sector ?? {};
  return `
    ${pageHead(
      "Sector detail",
      sectorName,
      `当前观察池中的 ${securities.length} 只标的；热度只反映观察池内部相对位置。`,
      `<a class="button" href="/sectors" data-route>${icon(
        "layers"
      )} 返回行业排行</a>`
    )}
    <section class="panel">
      <div class="panel-header">
        <div class="panel-heading">
          <h2>热度拆解</h2>
          <p>覆盖率不足时不参与排名</p>
        </div>
        ${badge(heatLabel(sector?.heatScore), isFiniteNumber(sector?.heatScore) ? "is-watch" : "is-missing")}
      </div>
      <div class="heat-breakdown">
        ${signalCell("1 日收益中位数", breakdown.return1dPct)}
        ${signalCell("5 日收益中位数", breakdown.return5dPct)}
        ${signalCell("20 日收益中位数", breakdown.return20dPct)}
        ${signalCell(
          "5 日上涨发行人",
          breakdown.breadth5Pct ?? breakdown.advancers5dPct
        )}
        ${signalCell("MA20 上方发行人", breakdown.aboveMa20Pct)}
      </div>
    </section>
    <div style="height:14px"></div>
    ${watchlistPanel({ title: `${sectorName}标的`, source: securities })}
  `;
}

function signalCell(label, value) {
  return `
    <div class="signal-cell">
      <small>${escapeHtml(label)}</small>
      <strong>${formatPct(value, { signed: label.includes("收益") })}</strong>
    </div>
  `;
}

function opportunitiesPage() {
  const priceCandidates = state.data.securities
    .filter((security) => {
      const distance = security.derived?.distanceToPreferredPct;
      return isFiniteNumber(distance) && distance <= 10;
    })
    .sort(
      (left, right) =>
        (right.derived?.opportunityScore ?? -Infinity) -
        (left.derived?.opportunityScore ?? -Infinity)
    );
  const technicalCandidates = state.data.securities.filter(
    (security) => security.derived?.hotSectorDislocation === true
  ).sort(
    (left, right) =>
      (right.derived?.opportunityTechnical ?? -Infinity) -
      (left.derived?.opportunityTechnical ?? -Infinity)
  );
  const contrarian = state.data.securities.filter(
    (security) =>
      security.derived?.contrarianLowPrice === true ||
      (security.derived?.targetStatus === "reached" &&
        isFiniteNumber(security.derived?.sectorHeatScore) &&
        security.derived.sectorHeatScore < 40)
  );
  const active =
    state.opportunityView === "technical"
      ? technicalCandidates
      : state.opportunityView === "contrarian"
        ? contrarian
        : priceCandidates;
  const title =
    state.opportunityView === "technical"
      ? "热门行业错杀"
      : state.opportunityView === "contrarian"
        ? "逆向观察"
        : "价格机会";
  return `
    ${pageHead(
      "Opportunity radar",
      "机会雷达",
      "把目标价接近程度、行业相对状态和技术超跌信号分开呈现；这里只做机械排序，不给出买入建议。",
      `<a class="button" href="/methodology" data-route>${icon(
        "book"
      )} 评分方法</a>`
    )}
    <div class="insight-banner">
      <span data-icon="info"></span>
      <span><strong>“到达理想价”不等于“技术超跌”。</strong> 后者必须同时具备 RSI、60 日回撤和相对行业表现；缺少历史数据时不会生成“热门行业错杀”。</span>
    </div>
    <div class="page-actions opportunity-tabs">
      ${opportunityTab("price", "价格机会", priceCandidates.length)}
      ${opportunityTab("technical", "热门行业错杀", technicalCandidates.length)}
      ${opportunityTab("contrarian", "逆向观察", contrarian.length)}
    </div>
    <section class="opportunity-layout">
      <div class="panel">
        <div class="panel-header">
          <div class="panel-heading">
            <h2>${title}</h2>
            <p>${opportunityDescription(state.opportunityView)}</p>
          </div>
          ${badge(`${active.length} 只`, active.length ? "is-watch" : "is-missing")}
        </div>
        ${
          active.length
            ? `<div class="opportunity-list">${active
                .map(opportunityCard)
                .join("")}</div>`
            : opportunityEmpty(state.opportunityView)
        }
      </div>
      ${opportunityMethodCard()}
    </section>
  `;
}

function opportunityTab(value, label, count) {
  return `
    <button class="button keep-mobile ${
      state.opportunityView === value ? "button-subtle" : ""
    }" type="button" data-opportunity-view="${value}">
      ${escapeHtml(label)} <span>${count}</span>
    </button>
  `;
}

function opportunityDescription(view) {
  if (view === "technical")
    return "行业热度 ≥ 60，且同时满足技术超跌与相对行业落后条件";
  if (view === "contrarian")
    return "偏冷行业中已经到达理想价的标的，单独观察、不等同推荐";
  return "距理想价不超过 10%，按机械机会分由高到低";
}

function opportunityCard(security) {
  const score =
    state.opportunityView === "technical"
      ? security.derived?.opportunityTechnical
      : security.derived?.opportunityScore;
  const components = security.derived?.opportunityComponents ?? {};
  return `
    <article class="opportunity-card" data-security="${escapeHtml(
      security.id
    )}" tabindex="0" role="button">
      <div class="opportunity-name">
        <span class="opportunity-score">${isFiniteNumber(score) ? formatNumber(
          score,
          0
        ) : "—"}</span>
        <span>
          <strong>${escapeHtml(security.name)}</strong>
          <small>${escapeHtml(security.sector)} · ${escapeHtml(
            security.ticker
          )}</small>
        </span>
      </div>
      <span class="mini-metric">
        <small>距理想价</small>
        <strong>${formatPct(security.derived?.distanceToPreferredPct, {
          signed: true
        })}</strong>
      </span>
      <span class="mini-metric">
        <small>股息回购率</small>
        <strong>${formatPct(security.currentShareholderYieldPct)}</strong>
      </span>
      <span class="mini-metric">
        <small>行业热度</small>
        <strong>${formatMetric(security.derived?.sectorHeatScore, "", 0)}</strong>
      </span>
      <span>${icon("arrow")}</span>
    </article>
  `;
}

function opportunityEmpty(view) {
  const dataMissing =
    view === "technical" && state.data.securities.length > 0;
  return `
    <div class="empty-state compact">
      <div>
        <div class="empty-illustration">${icon("radar")}</div>
        <h3>${
          dataMissing ? "历史数据不足，无法计算" : "本次没有符合规则的标的"
        }</h3>
        <p>${
          dataMissing
            ? "需要 RSI14、60 日回撤、5 日相对行业表现和合格行业热度，系统不会用价格接近目标来替代。"
            : "接入观察清单与行情后，这里会按当前所选规则展示候选。"
        }</p>
        ${
          state.data.securities.length === 0
            ? `<button class="button button-primary" type="button" data-action="enter-demo">${icon(
                "flask"
              )} 查看界面演示</button>`
            : ""
        }
      </div>
    </div>
  `;
}

function opportunityMethodCard() {
  if (state.opportunityView === "contrarian") {
    return `
      <aside class="panel method-card">
        <h3>逆向观察条件</h3>
        <div class="formula-stack">
          <div class="formula-row"><span>行业热度</span><div class="formula-track"><i style="width:40%"></i></div><strong>&lt; 40</strong></div>
          <div class="formula-row"><span>价格状态</span><div class="formula-track"><i style="width:100%"></i></div><strong>已到价</strong></div>
        </div>
        <p class="method-note">这是规则筛选而不是加权评分；偏冷行业的低价标的单独观察，不使用“推荐买入”措辞。</p>
      </aside>
    `;
  }
  const weights =
    state.opportunityView === "technical"
      ? [
          ["目标价吸引力", 55],
          ["技术超跌", 30],
          ["行业热度", 15]
        ]
      : [
          ["目标价吸引力", 80],
          ["股息回购率", 10],
          ["估值标签", 10]
        ];
  return `
    <aside class="panel method-card">
      <h3>排序分解</h3>
      <div class="formula-stack">
        ${weights
          .map(
            ([label, weight]) => `
              <div class="formula-row">
                <span>${label}</span>
                <div class="formula-track"><i style="width:${clamp(
                  weight,
                  0,
                  100
                )}%"></i></div>
                <strong>${weight}%</strong>
              </div>
            `
          )
          .join("")}
      </div>
      <p class="method-note">${
        state.opportunityView === "technical"
          ? "只有行业热度和技术输入完整并满足错杀条件时才进入本页签。"
          : "目标价、股息回购率或估值标签缺失时不生成综合分；仍可按目标距离查看。"
      } 所有分数仅用于观察顺序。</p>
    </aside>
  `;
}

function alertsPage() {
  const alerts = state.data.securities.filter((security) =>
    ["buy_zone", "approaching", "watch"].includes(
      security.derived?.alertStatus
    )
  );
  return `
    ${pageHead(
      "Alert center",
      "提醒中心",
      "集中查看新近触发、已经到价和临近目标的标的。正式提醒将按跨越阈值事件去重，不会每分钟重复生成。",
      `<a class="button" href="/methodology" data-route>${icon(
        "book"
      )} 提醒规则</a>`
    )}
    <section class="panel">
      <div class="panel-header">
        <div class="panel-heading">
          <h2>价格提醒</h2>
          <p>当前快照中的触发状态</p>
        </div>
        ${badge(`${alerts.length} 条`, alerts.length ? "is-new" : "is-missing")}
      </div>
      ${
        alerts.length
          ? `<div class="alert-list">${alerts.map(alertRow).join("")}</div>`
          : `
            <div class="empty-state">
              <div>
                <div class="empty-illustration">${icon("bell")}</div>
                <h3>暂无价格提醒</h3>
                <p>${
                  state.data.securities.length
                    ? "当前没有标的进入设定的提醒区间。"
                    : "确定观察标的和三档目标价后，价格跨越阈值的事件会出现在这里。"
                }</p>
                ${
                  state.data.securities.length === 0
                    ? `<button class="button button-primary" type="button" data-action="enter-demo">${icon(
                        "flask"
                      )} 查看界面演示</button>`
                    : ""
                }
              </div>
            </div>
          `
      }
    </section>
  `;
}

function alertRow(security) {
  const alert = alertStatus(security.derived?.alertStatus);
  const distance = security.derived?.distanceToPreferredPct;
  const reason =
    security.derived?.targetStatus === "reached"
      ? `现价已低于理想价 ${formatPct(Math.abs(distance ?? 0))}`
      : `距理想价 ${formatPct(distance)}`;
  return `
    <article class="alert-row" data-security="${escapeHtml(
      security.id
    )}" tabindex="0" role="button">
      <span class="alert-icon ${
        security.derived?.alertStatus === "buy_zone" ? "" : "is-amber"
      }">${icon("bell")}</span>
      <span class="alert-copy">
        <strong>${escapeHtml(security.name)}</strong>
        <small>${escapeHtml(security.ticker)} · ${badge(
          alert.label,
          alert.className
        )}</small>
      </span>
      <span class="alert-reason">${reason}</span>
      <time class="alert-time">${
        security.quote?.status === "fictional"
          ? "演示快照"
          : formatDateTime(
              security.quote?.lastUpdatedAt ?? security.lastUpdate,
              { timeOnly: true }
            )
      }</time>
    </article>
  `;
}

async function loadV2PipelineStatus() {
  if (state.v2PipelineStatus || state.v2PipelineLoading) return;
  state.v2PipelineLoading = true;
  try {
    const response = await fetch("/api/v1/pipeline/status", {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    const payload = await response.json().catch(() => null);
    state.v2PipelineStatus = response.ok
      ? payload
      : { enabled: false, last_error: payload?.detail || `状态返回 ${response.status}` };
  } catch (error) {
    state.v2PipelineStatus = { enabled: false, last_error: error.message || "状态不可用" };
  } finally {
    state.v2PipelineLoading = false;
    if (currentRoute().name === "data-status") renderRoute({ preserveScroll: true });
  }
}

function dataStatusPage() {
  const meta = state.data.meta;
  const summary = state.data.summary;
  const total = summary.totalSecurities ?? 0;
  const provider = meta.isDemo
    ? "虚构演示固定数据"
    : meta.dataSource || "等待连接 Futu OpenD";
  const snapshotHealthy = !meta.lastRefreshError;
  const peCoverage = summary.peAvailableCount ?? 0;
  const pbCoverage = summary.pbAvailableCount ?? 0;
  const valuationCoverage = Math.min(peCoverage, pbCoverage);
  const v2 = state.v2PipelineStatus || {};
  const v2Meta = state.data.shareholderReturnV2 || {};
  const statusCounts = v2.company_status_counts || {};
  const jobs = v2.jobs || {};
  const v2Healthy = v2Meta.available === true && !v2.last_error;
  return `
    ${pageHead(
      "Data health",
      "数据与连接状态",
      "这里把行情时间、服务器读取时间和配置状态分开显示，方便判断页面展示的究竟是不是新鲜数据。",
      `<button class="button" type="button" data-action="manual-refresh">${icon(
        "refresh"
      )} 重新检查</button>`
    )}
    <section class="status-grid">
      ${statusCard(
        "观察清单",
        total ? `${total} 只` : "未配置",
        total ? "清单已载入" : "等待正式观察名单",
        total ? "is-live" : "is-pending"
      )}
      ${statusCard(
        "行情来源",
        provider,
        meta.isRealtime ? "实时快照已连接" : "当前不是实时行情",
        meta.isRealtime ? "is-live" : "is-pending"
      )}
      ${statusCard(
        "最新快照",
        meta.isDemo
          ? "固定演示"
          : formatDateTime(meta.lastQuoteRefreshSucceededAt),
        snapshotHealthy ? "服务器读取正常" : meta.lastRefreshError,
        snapshotHealthy ? "is-live" : "is-error"
      )}
      ${statusCard(
        "估值快照",
        `${valuationCoverage} / ${total}`,
        `PE ${peCoverage} / PB ${pbCoverage}，随每分钟快照更新`,
        valuationCoverage === total && total > 0 ? "is-live" : "is-pending"
      )}
      ${statusCard(
        "十年周线",
        `${meta.historyAvailableCount ?? 0} / ${total}`,
        meta.lastHistoryError ||
          (meta.historyGeneratedAt
            ? `周线生成于 ${formatDateTime(meta.historyGeneratedAt)}`
            : "等待首轮周线采集"),
        (meta.historyAvailableCount ?? 0) === total && total > 0
          ? "is-live"
          : meta.lastHistoryError
            ? "is-error"
            : "is-pending"
      )}
      ${statusCard(
        "股东回报 v2",
        v2Meta.available ? `${v2Meta.companyCount ?? 0} 家` : "等待首个发布",
        v2.last_structured_calculation_at
          ? `计算于 ${formatDateTime(v2.last_structured_calculation_at)}`
          : v2.last_error || "结构化发布尚未就绪",
        v2Healthy ? "is-live" : v2.last_error ? "is-error" : "is-pending"
      )}
      ${statusCard(
        "Codex任务",
        `${jobs.queued ?? 0} 排队 / ${jobs.running ?? 0} 运行`,
        `${jobs.waiting_retry ?? 0} 等待重试 · ${jobs.failed ?? 0} 失败`,
        (jobs.failed ?? 0) > 0 ? "is-error" : (jobs.running ?? 0) > 0 ? "is-live" : "is-pending"
      )}
    </section>
    <section class="panel">
      <div class="panel-header">
        <div class="panel-heading">
          <h2>行情链路</h2>
          <p>本机只读采集，SSH 主动推送，Ali 只读展示</p>
        </div>
        ${badge(snapshotHealthy ? "服务正常" : "需要检查", snapshotHealthy ? "is-live" : "is-error")}
      </div>
      <div class="readiness-list">
        ${readinessRow(
          "FastAPI 展示服务",
          "公网服务只提供 GET 接口，不开放行情写入端点。",
          true,
          "Ali"
        )}
        ${readinessRow(
          "本机 Futu OpenD",
          "只监听 127.0.0.1:11111，由本机采集脚本读取快照。",
          total > 0 && meta.dataSource?.toLowerCase?.().includes("futu"),
          "Linux"
        )}
        ${readinessRow(
          "每分钟 SSH 推送",
          "先上传临时文件，再在 Ali 原子替换 latest_snapshot.json。",
          Boolean(meta.lastQuoteRefreshSucceededAt),
          "systemd"
        )}
        ${readinessRow(
          "十年前复权周线",
          "每周从 Futu OpenD 独立采集，页面只在打开标的详情时读取该标的历史。",
          (meta.historyAvailableCount ?? 0) === total && total > 0,
          `${meta.historyAvailableCount ?? 0} / ${total}`
        )}
        ${readinessRow(
          "自动目标价",
          "按确认现金分红与净注销回购的周期年均每股人民币金额，自动生成 3% / 4% / 5% 三档目标价。",
          (summary.targetConfiguredCount ?? 0) > 0,
          `${summary.targetConfiguredCount ?? 0} / ${total}`
        )}
        ${readinessRow(
          "富途估值字段",
          "PE、PE-TTM、PB、TTM 股息率、总市值、EPS 和每股净资产随行情快照传入网页。",
          (summary.futuMetricCompleteCount ?? 0) === total && total > 0,
          `${summary.futuMetricCompleteCount ?? 0} / ${total}`
        )}
        ${readinessRow(
          "v2结构化发布",
          "Linux以Decimal完成慢变量与快变量计算；INVALID公司使用最后合法快照，不影响其他公司。",
          v2Healthy,
          `VALID ${statusCounts.VALID ?? 0} / PARTIAL ${statusCounts.PARTIAL ?? 0} / 阻塞 ${(statusCounts.INVALID ?? 0) + (statusCounts.STALE ?? 0)}`
        )}
        ${readinessRow(
          "本地Codex风险服务",
          `固定 ${v2.model || "gpt-5.6-sol"} / ${v2.reasoning_effort || "xhigh"}；FastAPI只读取最后成功报告。`,
          Boolean(v2.analysis_release),
          v2.analysis_release || "等待合法报告"
        )}
        ${readinessRow(
          "原子发布与同步",
          "结构化数据和分析报告使用独立manifest、SHA-256、incoming目录和current符号链接。",
          Boolean(v2.last_sync),
          v2.structured_release || "等待发布"
        )}
      </div>
    </section>
  `;
}

function statusCard(title, value, copy, dotClass) {
  return `
    <article class="status-card">
      <div class="status-card-head">
        <h3>${escapeHtml(title)}</h3>
        <span class="status-dot ${dotClass}"></span>
      </div>
      <div class="status-card-value">${escapeHtml(value)}</div>
      <p>${escapeHtml(copy)}</p>
    </article>
  `;
}

function readinessRow(title, copy, ready, locationLabel) {
  return `
    <div class="readiness-row">
      <span class="check-mark ${ready ? "" : "is-pending"}">${icon(
        ready ? "check" : "more"
      )}</span>
      <strong>${escapeHtml(title)}</strong>
      <p>${escapeHtml(copy)}</p>
      <small>${escapeHtml(locationLabel)}</small>
    </div>
  `;
}

function methodologyPage() {
  const v2Definitions = [
    "sustainable_shareholder_yield",
    "conservative_return_10y",
    "recommendation_index",
    "entry_risk_index",
  ]
    .map(metricDefinition)
    .filter(Boolean);
  return `
    ${pageHead(
      "Methodology",
      "规则与数据口径",
      "每一个状态都应能追溯到输入数据和确定公式。缺失值保持为空，不用零值或主观判断补齐。"
    )}
    <section class="methodology-grid">
      <article class="panel">
        <section class="method-section" id="shareholder-return-v2">
          <h2>1. 股东回报 v2（当前口径）</h2>
          <p>Linux 数据服务器完成来源校验、Decimal 计算、自动评分和触发判断；浏览器只显示已发布字段，不重新计算收益率或评分。特别股息单列，只有实际注销且形成稀释后净股本减少的回购才进入基础分配。</p>
          ${v2Definitions
            .map(
              (definition) => `<article class="method-definition">
                <h3>${escapeHtml(definition.label_zh)}${metricInfoButton(
                  definition.id,
                  "具体公司状态请在详情页查看"
                )}</h3>
                <div class="formula">${escapeHtml(definition.formula_symbolic)}</div>
                <p>${escapeHtml(definition.simple_interpretation_zh)}</p>
              </article>`
            )
            .join("")}
          <p><strong>重要：</strong>缺失、过期、行业不适用或来源冲突不会显示为 0；关键校验失败时保留上一个合法快照并关闭本次推荐。</p>
        </section>
        <section class="method-section" id="target-distance">
          <h2>2. 旧版自动目标价与估值（v1迁移期）</h2>
          <p>目标价不手工填写。先计算观察期内“确认现金分红 + 净注销回购”的年度每股人民币金额算术平均，再反推对应股息回购率的价格。</p>
          <div class="formula">目标价 = 周期年均每股净现金回报 ÷ 目标股息回购率</div>
          <ul>
            <li>3%：关注价</li>
            <li>4%：理想目标价，也是提醒主阈值</li>
            <li>5%：深度价值价</li>
          </ul>
          <p>估值状态同样由当前股息回购率自动确定：≥5% 为“深度价值”，4%–5% 为“具吸引力”，3%–4% 为“合理”，低于 3% 为“偏贵”。表格中的估值与提醒标签均可点击筛选。</p>
          <div class="formula">距离 (%) = (当前价格 − 理想价) ÷ 理想价 × 100</div>
          <ul>
            <li>距离 ≤ 0：已到理想价</li>
            <li>0 &lt; 距离 ≤ 3%：临近理想价</li>
            <li>3% &lt; 距离 ≤ 10%：进入观察区</li>
            <li>没有有效行情或理想价：数据不足，不参与计数</li>
          </ul>
        </section>
        <section class="method-section" id="sector-heat">
          <h2>3. 观察池行业热度</h2>
          <p>行业热度只在当前观察池内比较。A/H 双重上市公司先在发行人层面合并，再按发行人等权，避免重复影响。</p>
          <div class="formula">Heat = 20%×P(R1) + 30%×P(R5) + 30%×P(R20) + 10%×P(Breadth5) + 10%×P(AboveMA20)</div>
          <p>至少需要 3 个独立发行人且历史覆盖率不低于 80%。不足时显示“数据不足”，不会把单日涨跌伪装成完整行业热度。</p>
        </section>
        <section class="method-section" id="oversold">
          <h2>4. 技术超跌与价格机会</h2>
          <p>接近理想价只说明进入预先设定的估值关注区，并不等于技术超跌。“热门行业错杀”还必须具备 RSI14、60 日回撤、5 日相对行业表现以及合格行业热度。</p>
          <h3>通用价格机会</h3>
          <div class="formula">PriceOpportunity = TargetAttractiveness</div>
          <p>通用价格机会只按现价与 4% 理想目标价的距离排序；当前股息回购率和估值状态作为可核验的上下文展示。</p>
          <h3>热门行业错杀</h3>
          <div class="formula">Opportunity = 55%×Target + 30%×Technical + 15%×SectorHeat</div>
          <p>若技术指标缺失，系统只展示“价格机会”，不会生成“热门行业错杀”。偏冷行业中已经到价的标的单列为“逆向观察”。</p>
        </section>
        <section class="method-section" id="refresh">
          <h2>5. 刷新与提醒</h2>
          <p>本机每分钟向 Futu OpenD 请求一次快照，再通过 SSH 原子推送到 Ali。浏览器只更新发生变化的数值，保留筛选、排序和滚动位置。</p>
          <p>提醒状态按现价相对 4% 理想目标价机械计算：已到价为“已触发”，高出 0–3% 为“即将触发”，高出 3–10% 为“关注中”，更远为“未触发”。</p>
          <p>港股人民币价使用 HKD/CNY 日参考汇率换算并标注“≈”；它用于统一显示和计算，不代表可成交汇率。</p>
        </section>
      </article>
      <aside class="panel toc">
        <h3>本页内容</h3>
        <a href="#shareholder-return-v2">股东回报 v2</a>
        <a href="#target-distance">距理想价</a>
        <a href="#sector-heat">行业热度</a>
        <a href="#oversold">技术超跌</a>
        <a href="#refresh">刷新与提醒</a>
        <div class="disclaimer"><strong>重要说明</strong><br />本网站是长期投资候选观察工具，不是交易终端、持仓系统或投资建议服务。任何机械状态都需要结合最新公开信息独立判断。</div>
      </aside>
    </section>
  `;
}

function errorPage() {
  return `
    ${pageHead(
      "Connection error",
      "暂时无法读取观察快照",
      state.error?.message ?? "服务未返回有效数据。"
    )}
    <section class="panel">
      <div class="empty-state">
        <div>
          <div class="empty-illustration">${icon("alert")}</div>
          <h3>数据服务未就绪</h3>
          <p>请确认 FastAPI 服务和只读快照文件状态，然后重新检查。</p>
          <button class="button button-primary" type="button" data-action="manual-refresh">重新检查</button>
        </div>
      </div>
    </section>
  `;
}

function notFoundPage(message = "这个页面不存在") {
  return `
    ${pageHead("Not found", "没有找到页面", message)}
    <section class="panel">
      <div class="empty-state">
        <div>
          <div class="empty-illustration">${icon("search")}</div>
          <h3>${escapeHtml(message)}</h3>
          <p>返回总览继续查看观察清单。</p>
          <a class="button button-primary" href="/" data-route>返回总览</a>
        </div>
      </div>
    </section>
  `;
}

function openDrawer(id, { push = true } = {}) {
  const security = state.data?.securities.find((item) => item.id === id);
  if (!security) {
    showToast("没有找到这个标的");
    return;
  }
  if (push && currentRoute().name !== "security-detail") {
    state.drawerReturnPath = `${location.pathname}${location.search}`;
    history.pushState(
      { drawer: true, returnPath: state.drawerReturnPath },
      "",
      pathWithMode(`/securities/${encodeURIComponent(id)}`)
    );
  } else if (!history.state?.returnPath) {
    state.drawerReturnPath = "/watchlist";
  }
  if (!state.drawerId && document.activeElement instanceof HTMLElement) {
    state.drawerTrigger = document.activeElement;
  }
  state.drawerId = id;
  renderDrawer(security);
  document.body.classList.add("is-drawer-open");
  drawer.setAttribute("aria-hidden", "false");
  drawer.removeAttribute("inert");
  drawerCloseButton.focus({ preventScroll: true });
  setActiveNavigation();
  void loadSecurityHistory(id);
  void loadV2CompanyDetail(security);
}

async function loadV2CompanyDetail(security) {
  const companyId = security?.issuerId;
  if (
    !companyId ||
    !security.shareholderReturnV2 ||
    state.v2CompanyCache.has(companyId) ||
    state.v2Loading.has(companyId)
  ) {
    return;
  }
  state.v2Loading.add(companyId);
  state.v2Errors.delete(companyId);
  if (state.drawerId === security.id) renderDrawer(security);
  try {
    const detailResponse = await fetch(
      `/api/v1/companies/${encodeURIComponent(companyId)}`,
      { cache: "no-store", headers: { Accept: "application/json" } }
    );
    const detail = await detailResponse.json().catch(() => null);
    if (!detailResponse.ok || !detail || typeof detail !== "object") {
      throw new Error(detail?.detail || `新版详情返回 ${detailResponse.status}`);
    }
    state.v2CompanyCache.set(companyId, detail);

    const analysisResponse = await fetch(
      `/api/v1/companies/${encodeURIComponent(companyId)}/analysis/latest`,
      { cache: "no-store", headers: { Accept: "application/json" } }
    );
    if (analysisResponse.ok) {
      const analysis = await analysisResponse.json();
      state.v2AnalysisCache.set(companyId, analysis);
    } else if (analysisResponse.status === 404) {
      state.v2AnalysisCache.set(companyId, null);
    } else {
      const problem = await analysisResponse.json().catch(() => null);
      throw new Error(problem?.detail || `Codex报告返回 ${analysisResponse.status}`);
    }
  } catch (error) {
    state.v2Errors.set(companyId, error.message || "无法读取新版详情");
  } finally {
    state.v2Loading.delete(companyId);
    if (state.drawerId === security.id) {
      const selected = state.data?.securities.find((item) => item.id === security.id);
      if (selected) renderDrawer(selected);
    }
  }
}

async function loadSecurityHistory(id) {
  if (
    state.historyCache.has(id) ||
    state.historyLoading.has(id) ||
    !state.data
  ) {
    return;
  }
  state.historyLoading.add(id);
  state.historyErrors.delete(id);
  if (state.drawerId === id) {
    const selected = state.data.securities.find((item) => item.id === id);
    if (selected) renderDrawer(selected);
  }
  const params = state.demo ? "?demo=1" : "";
  try {
    const response = await fetch(
      `/api/securities/${encodeURIComponent(id)}/history${params}`,
      {
        cache: "no-store",
        headers: { Accept: "application/json" }
      }
    );
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(
        payload?.error?.message || `周线服务返回 ${response.status}`
      );
    }
    if (!Array.isArray(payload?.points)) {
      throw new Error("周线数据格式不完整");
    }
    state.historyCache.set(id, payload);
  } catch (error) {
    state.historyErrors.set(id, error.message || "无法读取周线");
  } finally {
    state.historyLoading.delete(id);
    if (state.drawerId === id) {
      const selected = state.data?.securities.find((item) => item.id === id);
      if (selected) renderDrawer(selected);
    }
  }
}

function renderDrawer(security) {
  const currentPrice = security.quote?.currentPrice ?? security.currentPrice;
  const currentPriceCny =
    security.quote?.currentPriceCny ?? security.currentPriceCny;
  const change = security.quote?.dailyChangePct ?? security.dailyChangePct;
  const distance = security.derived?.distanceToPreferredPct;
  const historyRecord = state.historyCache.get(security.id);
  const historyLoading = state.historyLoading.has(security.id);
  const historyError = state.historyErrors.get(security.id);
  const chartSecurity = historyRecord
    ? { ...security, history: historyRecord.points }
    : security;
  const chart = priceChart(chartSecurity, { compact: compactChartLayout.matches });
  const historyBadge = historyRecord
    ? `${historyRecord.pointCount} 周 · 截至 ${historyRecord.asOf}`
    : historyLoading
      ? "正在加载"
      : historyError
        ? "加载失败"
        : state.demo
          ? "虚构历史"
          : "等待周线";
  const chartEmptyText = historyLoading
    ? "正在加载近10年前复权周线…"
    : historyError
      ? `周线暂不可用：${historyError}`
      : "十年周线尚未就绪。";
  drawerTitle.innerHTML = `
    <div class="drawer-title-line">
      <h2>${escapeHtml(security.name)}</h2>
      <span>${escapeHtml(security.ticker)}</span>
    </div>
    <p class="drawer-title-meta">${escapeHtml(marketLabel(security.market))} · ${escapeHtml(
      security.sector
    )} · ${
      security.quote?.status === "fictional"
        ? "虚构演示快照"
        : `行情 ${formatDateTime(
            security.quote?.lastUpdatedAt ?? security.lastUpdate,
            { timeOnly: true }
          )}`
    }</p>
  `;
  drawerBody.innerHTML = `
    <section class="drawer-quote">
      <div class="drawer-metric primary ${yieldToneClass(
        security.currentShareholderYieldPct
      )}">
        <small>${metricHeader("当前价格", "current_price", security.quote?.status || "等待行情")}</small>
        <strong>${formatPrice(currentPrice, security.currency)}</strong>
        ${
          security.currency === "HKD" && isFiniteNumber(currentPriceCny)
            ? `<span class="drawer-price-cny">≈ ${formatPrice(
                currentPriceCny,
                "CNY"
              )}</span>`
            : ""
        }
      </div>
      <div class="drawer-metric">
        <small>${metricHeader("日涨跌", "daily_price_change", security.quote?.status || "等待行情")}</small>
        <strong class="${trendClass(change)}">${formatPct(change, {
          signed: true
        })}</strong>
      </div>
      <div class="drawer-metric">
        <small>${metricHeader("距理想价v1", "legacy_distance_to_preferred_v1", isFiniteNumber(distance) ? "v1兼容值" : "数据不足")}</small>
        <strong class="distance ${distanceClass(distance)}">${formatPct(
          distance,
          { signed: true }
        )}</strong>
      </div>
    </section>
    ${shareholderReturnV2Section(security)}
    <section class="chart-card">
      <div class="chart-head">
        <div>
          <h3>近10年周线与目标线</h3>
          <small>前复权周收盘价 · 滑动查看日期与按当前周期基数折算的股息回购率</small>
        </div>
        <span class="badge is-idle">${escapeHtml(historyBadge)}</span>
      </div>
      ${
        chart ??
        `<div class="chart-empty"><div><span data-icon="linechart"></span><p>${escapeHtml(
          chartEmptyText
        )}</p></div></div>`
      }
      <div class="chart-legend">
        <span><i></i>周收盘</span>
        <span><i class="watch"></i>3% 关注价</span>
        <span><i class="preferred"></i>4% 理想价</span>
        <span><i class="deep"></i>5% 深度价值价</span>
      </div>
    </section>
    ${targetSection(security)}
    ${valuationSection(security)}
    ${textSection("target", "投资逻辑", security.investmentThesis)}
    ${textSection("alert", "主要风险", security.risks)}
    ${textSection("note", "观察笔记", security.notes)}
  `;
  hydrateIcons(drawerBody);
  if (chart) {
    bindPriceChartInteraction(drawerBody.querySelector(".price-chart"), chartSecurity);
  }
}

function detailRecord(detail, id, { score = false } = {}) {
  return (score ? detail?.scores : detail?.metrics)?.[id] ?? null;
}

function v2DetailMetricCard(detail, id, { score = false, fallback = "数据不足" } = {}) {
  const definition = metricDefinition(id);
  const record = detailRecord(detail, id, { score });
  const status = record?.reason || record?.status || detail?.data_status || fallback;
  return `
    <article class="v2-detail-card">
      <div class="v2-detail-card-head">
        <small>${escapeHtml(definition?.short_label_zh || definition?.label_zh || id)}</small>
        ${metricInfoButton(id, status)}
      </div>
      <strong>${escapeHtml(publishedDisplay(record, fallback))}</strong>
      <span>${escapeHtml(record?.reason || dataStatusLabel(detail?.data_status))}</span>
    </article>
  `;
}

function v2DetailSection(title, metricCards, note = "") {
  return `
    <section class="v2-detail-group">
      <h4>${escapeHtml(title)}</h4>
      <div class="v2-detail-grid">${metricCards.join("")}</div>
      ${note ? `<p>${escapeHtml(note)}</p>` : ""}
    </section>
  `;
}

function distributionHistoryMarkup(detail) {
  const rows = Array.isArray(detail?.distribution_history)
    ? detail.distribution_history
    : [];
  if (!rows.length) return '<p class="v2-empty">等待完整财年分配数据。</p>';
  return `
    <div class="distribution-history-scroll">
      <table class="distribution-history-table">
        <thead><tr><th>财年</th><th>${metricHeader("普通分红", "ordinary_cash_dividend")}</th><th>${metricHeader("特别股息", "special_dividend")}</th><th>${metricHeader("合格回购", "eligible_buyback")}</th><th>${metricHeader("有效分配 X", "effective_distribution")}</th></tr></thead>
        <tbody>${rows
          .map(
            (row) => `<tr><td>${escapeHtml(row.fiscal_year)}</td><td>${escapeHtml(
              row.ordinary_dividend?.display || "数据不足"
            )}</td><td>${escapeHtml(row.special_dividend?.display || "等待披露")}</td><td>${escapeHtml(
              row.eligible_buyback?.display || "数据不足"
            )}</td><td>${escapeHtml(row.effective_distribution?.display || "暂不可计算")}</td></tr>`
          )
          .join("")}</tbody>
      </table>
    </div>
  `;
}

function sourceSummaryMarkup(detail) {
  const source = detail?.source_summary;
  if (!source || (typeof source === "object" && !Object.keys(source).length)) {
    return '<p class="v2-empty">来源索引等待接入；本次推荐保持关闭或受限。</p>';
  }
  const entries = Array.isArray(source)
    ? source
    : Object.entries(source).map(([key, value]) => ({ key, value }));
  return `<ul class="source-summary-list">${entries
    .slice(0, 12)
    .map((entry) => {
      const label = entry.title || entry.source_name || entry.key || "来源";
      const value = entry.document || entry.value || entry.as_of_date || "已记录";
      return `<li><strong>${escapeHtml(label)}</strong><span>${escapeHtml(
        typeof value === "string" ? value : JSON.stringify(value)
      )}</span></li>`;
    })
    .join("")}</ul>`;
}

function codexReportMarkup(security, detail) {
  const companyId = security.issuerId;
  const loading = state.v2Loading.has(companyId);
  const error = state.v2Errors.get(companyId);
  const hasLoaded = state.v2AnalysisCache.has(companyId);
  const analysis = state.v2AnalysisCache.get(companyId);
  const status = detail?.analysis_status ?? security.shareholderReturnV2?.analysis;
  if (loading && !hasLoaded) return '<p class="v2-empty">正在读取已发布报告…</p>';
  if (error) return `<p class="v2-error">${escapeHtml(error)}；页面继续保留上一次合法状态。</p>`;
  if (!analysis) {
    return `<p class="v2-empty">${escapeHtml(analysisStatusLabel(status))}。FastAPI 不会在请求中等待或调用 Codex。</p>`;
  }
  return `
    <div class="codex-report-meta">
      <span>任务 ${escapeHtml(analysisStatusLabel(status))}</span>
      <span>结论 ${escapeHtml(analysis.verdict || "—")}</span>
      <span>风险 ${escapeHtml(analysis.risk_overlay || "—")}</span>
      <span>更新 ${escapeHtml(analysis.as_of_date || "—")}</span>
    </div>
    <p class="codex-one-line">${escapeHtml(analysis.one_sentence_conclusion || "")}</p>
    <div class="safe-markdown">${renderSafeMarkdown(analysis.report_markdown || "")}</div>
  `;
}

function shareholderReturnV2Section(security) {
  const summary = security.shareholderReturnV2;
  if (!summary) {
    return `
      <section class="drawer-section shareholder-v2">
        <h3><span data-icon="database"></span> 股东回报 v2</h3>
        <p>新版结构化数据尚未发布；继续显示标记为 v1 的旧口径，不混合计算。</p>
      </section>
    `;
  }
  const companyId = security.issuerId;
  const detail = state.v2CompanyCache.get(companyId) || summary;
  const statusText = dataStatusLabel(detail.data_status);
  const vetoes = Array.isArray(detail.veto_flags) ? detail.veto_flags : [];
  const coverage = detail.coverage_adapter || {};
  const validationMessages = [
    ...(detail.validation_errors || []),
    ...(detail.blocked_update?.errors || [])
  ];
  const securityPrice = detail.security_metrics?.[security.id]?.price_at_4pct;
  const securityPriceCard = `
    <article class="v2-detail-card">
      <div class="v2-detail-card-head"><small>证券层4%价格</small>${metricInfoButton(
        "security_price_at_4pct",
        securityPrice?.reason || securityPrice?.status || statusText
      )}</div>
      <strong>${escapeHtml(publishedDisplay(securityPrice))}</strong>
      <span>经股份权利与币种校验后计算</span>
    </article>`;
  return `
    <section class="drawer-section shareholder-v2">
      <div class="v2-source-boundary">
        <span>自动计算</span>
        <strong class="data-status is-${escapeHtml(String(detail.data_status || "missing").toLowerCase())}">${escapeHtml(
          statusText
        )}</strong>
        <small>${escapeHtml(detail.calculation_version || "shareholder-return-v2")}</small>
      </div>
      <div class="v2-disclaimer">
        该系统用于筛选和风险研究，不构成收益保证。CR10 是保守情景估算基准，不是锁定收益。Codex 报告是定性风险研究，不替代结构化财务数据。
      </div>
      ${v2DetailSection("当前估值与4%位置", [
        v2DetailMetricCard(detail, "company_market_cap"),
        v2DetailMetricCard(detail, "raw_2y_shareholder_yield"),
        v2DetailMetricCard(detail, "sustainable_shareholder_yield"),
        v2DetailMetricCard(detail, "company_value_at_4pct"),
        securityPriceCard,
      ])}
      <section class="v2-detail-group">
        <h4>股东分配历史</h4>
        ${distributionHistoryMarkup(detail)}
        <div class="v2-detail-grid">${[
          v2DetailMetricCard(detail, "recent_2y_distribution"),
          v2DetailMetricCard(detail, "median_5y_distribution"),
          v2DetailMetricCard(detail, "winsorized_10y_distribution"),
          v2DetailMetricCard(detail, "historical_conservative_distribution"),
        ].join("")}</div>
      </section>
      ${v2DetailSection(
        "现金流或资本覆盖",
        [
          v2DetailMetricCard(detail, "sustainable_distribution"),
          v2DetailMetricCard(detail, "coverage_ratio"),
          v2DetailMetricCard(detail, "net_debt_ebitda"),
        ],
        `行业适配器：${coverage.name || "等待识别"}；状态：${coverage.status || "数据不足"}${
          coverage.caveats?.length ? `；${coverage.caveats.join("；")}` : ""
        }`
      )}
      ${v2DetailSection("回购与股本变化", [
        v2DetailMetricCard(detail, "buyback_persistence_factor"),
        v2DetailMetricCard(detail, "eligible_buyback"),
      ])}
      ${v2DetailSection("十年保守回报", [
        v2DetailMetricCard(detail, "conservative_growth"),
        v2DetailMetricCard(detail, "valuation_drag"),
        v2DetailMetricCard(detail, "conservative_return_10y"),
      ])}
      ${v2DetailSection("自动推荐指数", [
        v2DetailMetricCard(detail, "return_score", { score: true }),
        v2DetailMetricCard(detail, "payout_quality", { score: true }),
        v2DetailMetricCard(detail, "business_durability", { score: true }),
        v2DetailMetricCard(detail, "governance_capital_allocation", { score: true }),
        v2DetailMetricCard(detail, "recommendation_index", { score: true }),
      ], `分类 ${detail.classification || "C"} · ${returnTypeLabel(detail.return_type)}`)}
      ${v2DetailSection("入手风险指数", [
        v2DetailMetricCard(detail, "entry_risk_index", { score: true }),
      ], vetoes.length ? `未解决否决项：${vetoes.map((flag) => flag.message_zh || flag.code).join("；")}` : "未触发结构化否决项。")}
      <section class="v2-detail-group">
        <h4>数据质量和来源</h4>
        <p>${escapeHtml(validationMessages.join("；") || "结构化校验未报告错误。")}</p>
        ${sourceSummaryMarkup(detail)}
      </section>
      <section class="v2-detail-group codex-analysis-section">
        <div class="v2-source-boundary"><span>Codex 定性分析</span><small>不参与自动评分</small></div>
        ${codexReportMarkup(security, detail)}
      </section>
    </section>
  `;
}

function targetSection(security) {
  const targetMap = Object.fromEntries(
    (security.targetLines ?? []).map((target) => [target.key, target])
  );
  const fallback = security.targetPrices ?? {};
  const cell = (key, label, className) => {
    const value = targetMap[key]?.price ?? fallback[key];
    const cny = targetMap[key]?.priceCny ?? security.targetPricesCny?.[key];
    return `
      <div class="target-cell ${className}">
        <small>${label}</small>
        <span class="target-price-stack">
          <strong>${formatPrice(value, security.currency)}</strong>
          ${
            security.currency === "HKD" && isFiniteNumber(cny)
              ? `<small>≈ ${formatPrice(cny, "CNY")}</small>`
              : ""
          }
        </span>
      </div>
    `;
  };
  const alert = alertStatus(security.derived?.alertStatus);
  return `
    <section class="drawer-section">
      <h3><span data-icon="target"></span> 三档自动目标价</h3>
      <div class="target-lines">
        ${cell("watch", "3% 关注价", "watch")}
        ${cell("preferred", "4% 理想目标价", "preferred")}
        ${cell("deep", "5% 深度价值价", "deep")}
      </div>
      <div class="target-model-note">
        <span>提醒状态</span>
        ${statusFilterBadge(
          "alert",
          security.derived?.alertStatus,
          alert,
          security.name
        )}
        <p>目标价 = 周期年均每股净现金回报 ÷ 目标股息回购率；4% 为提醒主阈值。</p>
      </div>
    </section>
  `;
}

function valuationSection(security) {
  const metrics = security.metrics ?? {};
  const technical = security.technicalIndicators ?? {};
  const basis = security.yieldBasis ?? {};
  const valuation = valuationBadgeInfo(security.valuationStatus);
  const range =
    Number.isInteger(basis.startYear) && Number.isInteger(basis.endYear)
      ? `${basis.startYear}–${basis.endYear}（${basis.windowYears ?? "—"} 年）`
      : "—";
  const cells = [
    [
      "估值状态",
      statusFilterBadge(
        "valuation",
        security.valuationStatus,
        valuation,
        security.name
      )
    ],
    ["当前股息回购率", formatPct(security.currentShareholderYieldPct)],
    [
      "周期年均每股净现金回报",
      formatPrice(
        metrics.annualAverageShareholderReturnPerShareCny ??
          basis.annualAveragePerShareCny,
        "CNY"
      )
    ],
    ["统计区间", range],
    ["市盈率", formatMetric(metrics.pe, "×", 2)],
    [
      "市盈率 TTM",
      formatMetric(metrics.peTtm ?? metrics.pe, "×", 2)
    ],
    ["市净率", formatMetric(metrics.pb, "×", 1)],
    ["TTM 股息率", formatPct(metrics.dividendYieldTtmPct)],
    [
      `总市值（${security.currency || "本币"}）`,
      formatMarketValue(metrics.totalMarketValue, security.currency)
    ],
    [
      "每股收益",
      formatPrice(metrics.earningsPerShare, security.currency)
    ],
    [
      "每股净资产",
      formatPrice(metrics.bookValuePerShare, security.currency)
    ],
    ["RSI14", formatMetric(technical.rsi14 ?? metrics.rsi14, "", 1)],
    [
      "60 日回撤",
      formatPct(
        technical.drawdown60dPct ??
          technical.drawdown60Pct ??
          metrics.drawdown60Pct
      )
    ]
  ];
  return `
    <section class="drawer-section">
      <h3><span data-icon="grid"></span> 估值与质量</h3>
      <div class="valuation-grid">
        ${cells
          .map(
            ([label, value]) => `
              <div class="valuation-cell">
                <small>${label}</small>
                <strong>${value}</strong>
              </div>
            `
          )
          .join("")}
      </div>
      <p class="valuation-model-note">PE、PE-TTM、PB、TTM 股息率、总市值、EPS 和每股净资产来自同一份富途市场快照，每分钟检查更新；估值状态仍只按项目的当前股息回购率机械分档。</p>
    </section>
  `;
}

function textSection(iconName, title, content) {
  const list = Array.isArray(content)
    ? content.filter(Boolean)
    : content
      ? [content]
      : [];
  return `
    <section class="drawer-section">
      <h3><span data-icon="${iconName}"></span> ${title}</h3>
      ${
        list.length
          ? `<ul>${list.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
          : "<p>尚未填写。</p>"
      }
    </section>
  `;
}

function revisionSection(security) {
  const revisions = security.targetRevisionHistory ?? [];
  return `
    <section class="drawer-section">
      <h3><span data-icon="refresh"></span> 目标价修订记录</h3>
      ${
        revisions.length
          ? `<div class="revision-list">${revisions
              .map(
                (revision) => `
                  <div class="revision-row">
                    <time>${formatShortDate(revision.changedAt)}</time>
                    <div>
                      <strong>${escapeHtml(revision.label || "目标价修订")} · ${formatPrice(
                        revision.preferredPrice,
                        security.currency
                      )}</strong>
                      <p>${escapeHtml(revision.reason || "未填写修订原因")}</p>
                    </div>
                  </div>
                `
              )
              .join("")}</div>`
          : "<p>尚无修订记录。</p>"
      }
    </section>
  `;
}

function closeDrawer({ changeHistory = true } = {}) {
  closeMetricInfo({ force: true });
  const wasOpen = state.drawerId !== null;
  const returnFocus = state.drawerTrigger;
  state.drawerId = null;
  state.drawerTrigger = null;
  document.body.classList.remove("is-drawer-open");
  drawer.setAttribute("aria-hidden", "true");
  drawer.setAttribute("inert", "");
  if (changeHistory && wasOpen && currentRoute().name === "security-detail") {
    if (history.state?.drawer) {
      history.back();
    } else {
      const returnPath =
        history.state?.returnPath || state.drawerReturnPath || "/watchlist";
      history.replaceState({}, "", returnPath);
      readUiStateFromUrl();
      setActiveNavigation();
    }
  }
  if (wasOpen && returnFocus?.isConnected) {
    requestAnimationFrame(() => returnFocus.focus({ preventScroll: true }));
  }
}

function clearFilters() {
  state.filters = { ...FILTER_DEFAULTS };
  updateUrlState();
  renderRoute({ preserveScroll: true });
}

async function enterDemo() {
  state.demo = true;
  state.filters = { ...FILTER_DEFAULTS };
  state.historyCache.clear();
  state.historyErrors.clear();
  const params = new URLSearchParams();
  params.set("demo", "1");
  history.pushState({}, "", `${location.pathname}?${params}`);
  await loadData({ initial: true, reason: "demo" });
}

async function exitDemo() {
  state.demo = false;
  state.filters = { ...FILTER_DEFAULTS };
  state.historyCache.clear();
  state.historyErrors.clear();
  history.pushState({}, "", location.pathname);
  await loadData({ initial: true, reason: "live" });
}

document.addEventListener("click", (event) => {
  const metricInfo = event.target.closest("[data-metric-info]");
  if (metricInfo) {
    event.preventDefault();
    event.stopPropagation();
    if (state.metricInfoTrigger === metricInfo && state.metricInfoPinned) {
      closeMetricInfo({ force: true });
    } else {
      openMetricInfo(metricInfo, { pinned: true });
    }
    return;
  }
  if (event.target.closest("[data-close-metric-info]")) {
    event.preventDefault();
    closeMetricInfo({ force: true });
    return;
  }
  if (
    state.metricInfoPinned &&
    !event.target.closest("#metric-info-popover")
  ) {
    closeMetricInfo({ force: true });
  }

  const routeLink = event.target.closest("[data-route]");
  if (routeLink) {
    const url = new URL(routeLink.href, location.origin);
    if (url.origin === location.origin) {
      event.preventDefault();
      navigate(url.pathname);
      return;
    }
  }

  const action = event.target.closest("[data-action]")?.dataset.action;
  if (action === "manual-refresh") {
    void loadData({ initial: false, reason: "manual" });
    return;
  }
  if (action === "enter-demo") {
    void enterDemo();
    return;
  }
  if (action === "clear-filters") {
    clearFilters();
    return;
  }
  if (action === "toggle-filters") {
    state.mobileFiltersOpen = !state.mobileFiltersOpen;
    renderRoute({ preserveScroll: true });
    return;
  }

  const sortButton = event.target.closest("[data-sort]");
  if (sortButton) {
    const key = sortButton.dataset.sort;
    if (state.sort.key === key) {
      state.sort.direction = state.sort.direction === "asc" ? "desc" : "asc";
    } else {
      state.sort = { key, direction: "asc" };
    }
    updateUrlState();
    renderRoute({ preserveScroll: true });
    return;
  }

  const statusFilter = event.target.closest("[data-status-filter]");
  if (statusFilter) {
    const kind = statusFilter.dataset.statusFilter;
    const value = statusFilter.dataset.statusValue ?? "";
    if (kind === "valuation" || kind === "alert") {
      state.filters[kind] = state.filters[kind] === value ? "" : value;
      updateUrlState();
      renderRoute({ preserveScroll: true });
    }
    return;
  }

  const security = event.target.closest("[data-security]");
  if (security) {
    openDrawer(security.dataset.security);
    return;
  }

  const sector = event.target.closest("[data-sector]");
  if (sector) {
    navigate(`/sectors/${encodeURIComponent(sector.dataset.sector)}`);
    return;
  }

  const view = event.target.closest("[data-opportunity-view]");
  if (view) {
    state.opportunityView = view.dataset.opportunityView;
    updateUrlState();
    renderRoute({ preserveScroll: true });
  }
});

document.addEventListener("input", (event) => {
  if (!event.target.matches("[data-filter='query']")) return;
  state.filters.query = event.target.value;
  updateUrlState();
  renderRoute({ preserveScroll: true });
  requestAnimationFrame(() => {
    const input = document.querySelector("[data-filter='query']");
    if (input) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  });
});

document.addEventListener("change", (event) => {
  const filter = event.target.dataset.filter;
  if (!filter || filter === "query") return;
  state.filters[filter] = event.target.value;
  updateUrlState();
  renderRoute({ preserveScroll: true });
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Tab" && state.drawerId) {
    const focusable = Array.from(
      drawer.querySelectorAll(
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    ).filter((element) => !element.hasAttribute("inert"));
    const first = focusable[0];
    const last = focusable.at(-1);
    if (first && last) {
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  }
  if (
    (event.key === "Enter" || event.key === " ") &&
    event.target.matches("[data-security], [data-sector]")
  ) {
    event.preventDefault();
    event.target.click();
  }
  if (event.key === "Escape") {
    if (!metricInfoPopover.hidden) closeMetricInfo({ force: true });
    else if (state.drawerId) closeDrawer();
    else closeSidebar();
  }
});

document.addEventListener("pointerover", (event) => {
  const trigger = event.target.closest("[data-metric-info]");
  if (trigger && event.pointerType !== "touch") {
    openMetricInfo(trigger, { pinned: false });
  }
});

document.addEventListener("pointerout", (event) => {
  const trigger = event.target.closest("[data-metric-info]");
  if (
    trigger &&
    event.pointerType !== "touch" &&
    !trigger.contains(event.relatedTarget)
  ) {
    scheduleMetricInfoClose();
  }
});

document.addEventListener("focusin", (event) => {
  const trigger = event.target.closest("[data-metric-info]");
  if (trigger) openMetricInfo(trigger, { pinned: false });
});

document.addEventListener("focusout", (event) => {
  const trigger = event.target.closest("[data-metric-info]");
  if (
    trigger &&
    !trigger.contains(event.relatedTarget) &&
    !metricInfoPopover.contains(event.relatedTarget)
  ) {
    scheduleMetricInfoClose();
  }
});

metricInfoPopover.addEventListener("pointerenter", () => {
  window.clearTimeout(state.metricInfoCloseTimer);
});
metricInfoPopover.addEventListener("pointerleave", scheduleMetricInfoClose);
metricInfoPopover.addEventListener("focusin", () => {
  window.clearTimeout(state.metricInfoCloseTimer);
});
metricInfoPopover.addEventListener("focusout", (event) => {
  if (!metricInfoPopover.contains(event.relatedTarget)) scheduleMetricInfoClose();
});
window.addEventListener("resize", () =>
  positionMetricInfoPopover(state.metricInfoTrigger)
);
window.addEventListener(
  "scroll",
  () => positionMetricInfoPopover(state.metricInfoTrigger),
  true
);

mobileMenuButton.addEventListener("click", () => {
  if (document.body.classList.contains("is-sidebar-open")) closeSidebar();
  else openSidebar();
});
mobileMoreButton.addEventListener("click", openSidebar);
mobileLayout.addEventListener("change", syncSidebarInert);
compactChartLayout.addEventListener("change", () => {
  if (!state.drawerId) return;
  const selected = state.data?.securities.find((item) => item.id === state.drawerId);
  if (selected) renderDrawer(selected);
});
sidebarScrim.addEventListener("click", closeSidebar);
refreshButton.addEventListener("click", () =>
  void loadData({ initial: false, reason: "manual" })
);
drawerBackdrop.addEventListener("click", () => closeDrawer());
drawerCloseButton.addEventListener("click", () => closeDrawer());
exitDemoButton.addEventListener("click", () => void exitDemo());

window.addEventListener("popstate", () => {
  readUiStateFromUrl();
  const route = currentRoute();
  if (route.name === "security-detail") {
    openDrawer(route.id, { push: false });
  } else {
    closeDrawer({ changeHistory: false });
    renderRoute();
  }
});

window.addEventListener("online", () => {
  showToast("网络已恢复，正在检查快照");
  void loadData({ initial: false, reason: "online" });
});
window.addEventListener("offline", () => {
  showToast("当前离线，继续显示最后一次有效快照");
});

startCountdown();
void loadData({ initial: true, reason: "initial" });
