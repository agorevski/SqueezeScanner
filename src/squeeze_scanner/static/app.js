const form = document.querySelector("#scan-form");
const button = document.querySelector("#scan-button");
const mostShortedButton = document.querySelector("#most-shorted-button");
const statusEl = document.querySelector("#status");
const resultsEl = document.querySelector("#results");
const summaryEl = document.querySelector("#summary");
const filterStatusEl = document.querySelector("#filter-status");
const filterSearchEl = document.querySelector("#filter-search");
const filterModelEl = document.querySelector("#filter-model");
const filterRiskEl = document.querySelector("#filter-risk");
const filterMinScoreEl = document.querySelector("#filter-min-score");
const filterMinShortFloatEl = document.querySelector("#filter-min-short-float");
const filterMinRelativeVolumeEl = document.querySelector("#filter-min-relative-volume");
const filterMaxFloatEl = document.querySelector("#filter-max-float");
const clearFiltersButton = document.querySelector("#clear-filters");
const filterControls = [
  filterSearchEl,
  filterModelEl,
  filterRiskEl,
  filterMinScoreEl,
  filterMinShortFloatEl,
  filterMinRelativeVolumeEl,
  filterMaxFloatEl,
].filter(Boolean);

const screenedResults = new Map();

const numberFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 2,
});
const compactFormatter = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 2,
});
const currencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});
let modelMetadata = null;
let modelDefinitions = {};
let modelOrder = [];

document.addEventListener("DOMContentLoaded", async () => {
  await loadModelMetadata();
  loadRecentScans();
});

document.addEventListener("click", (event) => {
  const deleteButton = event.target.closest("[data-delete-symbol]");
  if (deleteButton) {
    deleteScreenedSymbol(deleteButton.dataset.deleteSymbol);
    return;
  }

  const tooltipTarget = event.target.closest(".has-tooltip");
  document
    .querySelectorAll(".has-tooltip.tooltip-open")
    .forEach((element) => {
      if (element !== tooltipTarget) {
        element.classList.remove("tooltip-open");
      }
    });

  if (!tooltipTarget) {
    return;
  }

  tooltipTarget.classList.toggle("tooltip-open");
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") {
    return;
  }
  document
    .querySelectorAll(".has-tooltip.tooltip-open")
    .forEach((element) => element.classList.remove("tooltip-open"));
});

filterControls.forEach((control) => {
  control.addEventListener("input", renderScreenedResults);
  control.addEventListener("change", renderScreenedResults);
});

clearFiltersButton?.addEventListener("click", () => {
  filterControls.forEach((control) => {
    control.value = "";
  });
  renderScreenedResults();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setLoading(true, "Scanning...", "Checking cache and refreshing stale market data.");
  clearErrors();

  const symbols = new FormData(form).get("symbols");

  try {
    const payload = await fetchJson("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols }),
    });

    applyModelMetadata(payload.model);
    renderSignalGuide();
    mergeResults(payload.results);
    renderScreenedResults();
    renderErrors(payload.errors);

    statusEl.textContent = payload.count
      ? `Scan complete. Added or refreshed ${payload.count} screened stock${payload.count === 1 ? "" : "s"}.`
      : "No ticker data was returned.";
    statusEl.className = payload.count ? "status success" : "status error";
  } catch (error) {
    statusEl.textContent = error.message;
    statusEl.className = "status error";
  } finally {
    setLoading(false);
  }
});

mostShortedButton.addEventListener("click", async () => {
  setLoading(
    true,
    "Scanning...",
    "Loading Yahoo's most-shorted universe, then analyzing each ticker.",
    mostShortedButton,
    "Loading...",
  );
  clearErrors();

  try {
    const payload = await fetchJson("/api/scan/most-shorted?count=100", {
      method: "POST",
    });

    applyModelMetadata(payload.model);
    renderSignalGuide();
    mergeResults(payload.results);
    renderScreenedResults();
    renderErrors(payload.errors);

    const errorCount = payload.errors?.length || 0;
    statusEl.textContent = `Loaded Yahoo most-shorted stocks and analyzed ${payload.count} ticker${
      payload.count === 1 ? "" : "s"
    }${errorCount ? ` (${errorCount} unavailable)` : ""}.`;
    statusEl.className = payload.count ? "status success" : "status error";
  } catch (error) {
    statusEl.textContent = `Could not load Yahoo most-shorted stocks: ${error.message}`;
    statusEl.className = "status error";
  } finally {
    setLoading(false);
  }
});

async function loadRecentScans() {
  try {
    const payload = await fetchJson("/api/scans/recent");
    applyModelMetadata(payload.model);
    renderSignalGuide();
    mergeResults(payload.results);
    renderScreenedResults();
    statusEl.textContent = payload.count
      ? `Loaded ${payload.count} stock${payload.count === 1 ? "" : "s"} screened within the last hour.`
      : "No stocks have been screened within the last hour.";
    statusEl.className = payload.count ? "status success" : "status";
  } catch (error) {
    statusEl.textContent = `Could not load recent screened stocks: ${error.message}`;
    statusEl.className = "status error";
  }
}

async function loadModelMetadata() {
  try {
    applyModelMetadata(await fetchJson("/api/model"));
    renderSignalGuide();
  } catch (error) {
    statusEl.textContent = `Could not load scoring model metadata: ${error.message}`;
    statusEl.className = "status error";
  }
}

function applyModelMetadata(model) {
  if (!model || !Array.isArray(model.models)) {
    return;
  }

  modelMetadata = model;
  modelDefinitions = Object.fromEntries(
    model.models.map((scoringModel) => [
      scoringModel.key,
      {
        key: scoringModel.key,
        category: scoringModel.category,
        label: scoringModel.label,
        definition: scoringModel.definition,
        signals: scoringModel.signals.map((signal) => ({
          key: signal.key,
          label: signal.label,
          max: signal.weight,
          means: signal.means,
          calculation: signal.calculation,
          favorable: signal.favorable,
        })),
      },
    ]),
  );
  modelOrder = model.models.map((scoringModel) => scoringModel.key);
  renderModelFilterOptions();
}

function renderModelFilterOptions() {
  if (!filterModelEl || !modelOrder.length) {
    return;
  }

  const currentValue = filterModelEl.value;
  filterModelEl.innerHTML = `
    <option value="">Any top model</option>
    ${modelOrder
      .map((key) => {
        const definition = modelDefinition(key);
        return `<option value="${escapeHtml(key)}">${escapeHtml(definition.label)}</option>`;
      })
      .join("")}
  `;

  if ([...filterModelEl.options].some((option) => option.value === currentValue)) {
    filterModelEl.value = currentValue;
  }
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail || "Request failed");
  }
  return payload;
}

function setLoading(
  isLoading,
  primaryLoadingText = "Scanning...",
  message = "Checking cache and refreshing stale market data.",
  activeButton = button,
  activeLoadingText = primaryLoadingText,
) {
  button.disabled = isLoading;
  mostShortedButton.disabled = isLoading;

  button.textContent = isLoading && activeButton === button ? primaryLoadingText : "Scan";
  mostShortedButton.textContent =
    isLoading && activeButton === mostShortedButton ? activeLoadingText : "Load Yahoo most-shorted";

  if (isLoading) {
    statusEl.textContent = message;
    statusEl.className = "status";
  }
}

function clearErrors() {
  document.querySelectorAll(".scan-error").forEach((element) => element.remove());
}

function mergeResults(results) {
  results.forEach((result) => screenedResults.set(result.symbol, result));
}

function sortedScreenedResults() {
  return [...screenedResults.values()].sort((left, right) => {
    if (right.score !== left.score) return right.score - left.score;
    if (right.data_quality !== left.data_quality) return right.data_quality - left.data_quality;
    return left.symbol.localeCompare(right.symbol);
  });
}

function renderScreenedResults() {
  const allResults = sortedScreenedResults();
  const visibleResults = filterScreenedResults(allResults);
  renderSummary(visibleResults, allResults.length);
  renderFilterStatus(visibleResults.length, allResults.length);
  renderResults(visibleResults, allResults.length);
}

function renderSummary(results, totalCount) {
  if (!totalCount) {
    summaryEl.innerHTML = "";
    return;
  }

  const highCount = results.filter((result) =>
    result.risk_level.toLowerCase().includes("high"),
  ).length;
  const watchCount = results.filter((result) =>
    result.risk_level.toLowerCase().includes("watch"),
  ).length;
  const best = results[0];

  summaryEl.innerHTML = `
    ${summaryCard("Showing", `${results.length}/${totalCount}`)}
    ${summaryCard("High setups", highCount)}
    ${summaryCard("Watchlist", watchCount)}
    ${summaryCard("Top score", best ? `${best.symbol} ${best.score}` : "None")}
  `;
}

function summaryCard(label, value) {
  return `
    <article class="summary-card">
      <span>${label}</span>
      <strong>${value}</strong>
    </article>
  `;
}

function filterScreenedResults(results) {
  const filters = currentFilters();
  return results.filter((result) => matchesFilters(result, filters));
}

function currentFilters() {
  const maxFloatMillions = numberFilterValue(filterMaxFloatEl);
  return {
    search: (filterSearchEl?.value || "").trim().toUpperCase(),
    model: filterModelEl?.value || "",
    risk: filterRiskEl?.value || "",
    minScore: numberFilterValue(filterMinScoreEl),
    minShortFloat: numberFilterValue(filterMinShortFloatEl),
    minRelativeVolume: numberFilterValue(filterMinRelativeVolumeEl),
    maxFloatShares: maxFloatMillions === null ? null : maxFloatMillions * 1_000_000,
  };
}

function numberFilterValue(element) {
  if (!element || element.value.trim() === "") {
    return null;
  }

  const value = Number(element.value);
  return Number.isFinite(value) ? value : null;
}

function matchesFilters(result, filters) {
  const metrics = result.metrics || {};
  const searchableText = `${result.symbol} ${result.company_name || ""}`.toUpperCase();

  if (filters.search && !searchableText.includes(filters.search)) {
    return false;
  }
  if (filters.model && result.primary_model !== filters.model) {
    return false;
  }
  if (filters.risk && result.risk_level !== filters.risk) {
    return false;
  }
  if (!meetsMinimum(result.score, filters.minScore)) {
    return false;
  }
  if (!meetsMinimum(metrics.short_percent_float, filters.minShortFloat)) {
    return false;
  }
  if (!meetsMinimum(metrics.relative_volume, filters.minRelativeVolume)) {
    return false;
  }
  if (!meetsMaximum(metrics.float_shares, filters.maxFloatShares)) {
    return false;
  }

  return true;
}

function meetsMinimum(value, minimum) {
  if (minimum === null) {
    return true;
  }

  return typeof value === "number" && value >= minimum;
}

function meetsMaximum(value, maximum) {
  if (maximum === null) {
    return true;
  }

  return typeof value === "number" && value <= maximum;
}

function renderFilterStatus(visibleCount, totalCount) {
  if (!filterStatusEl) {
    return;
  }

  if (!totalCount) {
    filterStatusEl.textContent = "Filters apply after stocks are screened.";
    return;
  }

  const filters = currentFilters();
  const active = hasActiveFilters(filters);
  filterStatusEl.textContent = active
    ? `Showing ${visibleCount} of ${totalCount} screened stock${totalCount === 1 ? "" : "s"}.`
    : `Showing all ${totalCount} screened stock${totalCount === 1 ? "" : "s"}.`;
}

function hasActiveFilters(filters) {
  return Boolean(
    filters.search ||
      filters.model ||
      filters.risk ||
      filters.minScore !== null ||
      filters.minShortFloat !== null ||
      filters.minRelativeVolume !== null ||
      filters.maxFloatShares !== null,
  );
}

function renderResults(results, totalCount) {
  if (!results.length) {
    resultsEl.innerHTML = totalCount
      ? `<div class="panel empty-results">No screened stocks match the current filters.</div>`
      : "";
    return;
  }

  resultsEl.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Top score</th>
            <th>Setup</th>
            ${activeModelKeys(results[0]).map((key) => `<th>${escapeHtml(modelShortLabel(modelDefinition(key)))}</th>`).join("")}
            <th>Price</th>
            <th>Short % Float</th>
            <th>Rel Vol</th>
            <th>Float</th>
            <th>Scanned</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${results.map(renderResultRow).join("")}
        </tbody>
      </table>
    </div>
    <div class="cards">
      ${results.map(renderResultCard).join("")}
    </div>
  `;
}

function renderResultRow(result) {
  const metrics = result.metrics;
  const primaryModel = modelDefinition(result.primary_model);
  return `
    <tr>
      <td>
        <strong>${escapeHtml(result.symbol)}</strong>
        <span>${escapeHtml(result.company_name || "")}</span>
      </td>
      <td>
        <strong>${formatNumber(result.score)}</strong>
        <span>${escapeHtml(primaryModel.label)}</span>
      </td>
      <td><span class="badge ${badgeClass(result.risk_level)}">${escapeHtml(result.risk_level)}</span></td>
      ${activeModelKeys(result).map((key) => `<td>${formatNumber(modelScore(result, key))}</td>`).join("")}
      <td>${formatCurrency(metrics.price)}</td>
      <td>${formatPercent(metrics.short_percent_float)}</td>
      <td>${formatMultiple(metrics.relative_volume)}</td>
      <td>${formatCompact(metrics.float_shares)}</td>
      <td>${formatScanAge(result.minutes_since_scan)}</td>
      <td>${deleteButton(result.symbol)}</td>
    </tr>
  `;
}

function renderResultCard(result) {
  const warnings = result.warnings.length
    ? `<div class="warnings"><strong>Data notes:</strong> ${escapeHtml(result.warnings.join(" "))}</div>`
    : "";

  return `
    <article class="result-card">
      <div class="card-heading">
        <div>
          <h2>${escapeHtml(result.symbol)}</h2>
          <p>${escapeHtml(result.company_name || "Company name unavailable")}</p>
          <p class="scan-age">Scanned ${formatScanAge(result.minutes_since_scan)}</p>
        </div>
        <div class="card-actions">
          <div
            class="score has-tooltip"
            data-tooltip="${escapeHtml(totalScoreTooltip(result))}"
            title="${escapeHtml(totalScoreTooltip(result))}"
            tabindex="0"
          >
            <span>${formatNumber(result.score)}</span>
            <small>${escapeHtml(modelDefinition(result.primary_model).label)}</small>
          </div>
          ${deleteButton(result.symbol)}
        </div>
      </div>
      <div class="model-score-grid">
        ${activeModelKeys(result).map((key) => modelScoreTile(result, key)).join("")}
      </div>
      ${renderModelBreakdowns(result)}
      ${warnings}
    </article>
  `;
}

function deleteButton(symbol) {
  return `
    <button
      type="button"
      class="icon-button delete-button"
      data-delete-symbol="${escapeHtml(symbol)}"
      aria-label="Delete ${escapeHtml(symbol)} from scanner"
      title="Delete ${escapeHtml(symbol)} from scanner"
    >
      🗑
    </button>
  `;
}

async function deleteScreenedSymbol(symbol) {
  if (!symbol) return;

  try {
    const payload = await fetchJson(`/api/scans/${encodeURIComponent(symbol)}`, {
      method: "DELETE",
    });
    screenedResults.delete(payload.symbol);
    renderScreenedResults();
    clearErrors();
    statusEl.textContent = payload.deleted
      ? `Deleted ${payload.symbol} from the scanner and local cache.`
      : `${payload.symbol} was not present in the local cache. Removed it from this page.`;
    statusEl.className = "status success";
  } catch (error) {
    statusEl.textContent = `Could not delete ${symbol}: ${error.message}`;
    statusEl.className = "status error";
  }
}

function modelScoreTile(result, modelKey) {
  const definition = modelDefinition(modelKey);
  const value = modelScore(result, modelKey);
  const tooltip = modelTooltip(definition);
  return `
    <div
      class="model-score-tile ${signalClass(value, scoreMax())} has-tooltip"
      data-tooltip="${escapeHtml(tooltip)}"
      title="${escapeHtml(tooltip)}"
      tabindex="0"
    >
      <span>${escapeHtml(definition.category)}</span>
      <strong>${formatNumber(value)}</strong>
      <small>${escapeHtml(definition.label)}</small>
    </div>
  `;
}

function renderModelBreakdowns(result) {
  return `
    <div class="model-breakdowns">
      ${activeModelKeys(result).map((key) => renderModelBreakdown(result, key)).join("")}
    </div>
  `;
}

function renderModelBreakdown(result, modelKey) {
  const definition = modelDefinition(modelKey);
  const isPrimary = result.primary_model === modelKey ? " open" : "";
  return `
    <details class="model-breakdown"${isPrimary}>
      <summary>
        <span>${escapeHtml(definition.category)}: ${escapeHtml(definition.label)}</span>
        <strong>${formatNumber(modelScore(result, modelKey))}</strong>
      </summary>
      <p>${escapeHtml(definition.definition)}</p>
      <div class="component-grid">
        ${definition.signals.map((signal) => component(componentScore(result, modelKey, signal.key), signal)).join("")}
      </div>
      ${renderSignalRationale(result, modelKey)}
    </details>
  `;
}

function component(value, definition) {
  const tooltip = signalTooltip(definition);
  return `
    <div
      class="signal-tile ${signalClass(value, definition.max)} has-tooltip"
      data-tooltip="${escapeHtml(tooltip)}"
      title="${escapeHtml(tooltip)}"
      tabindex="0"
    >
      <span>${definition.label}</span>
      <strong>${formatNumber(value)}</strong>
      <small>${favorabilityLabel(value, definition.max)}</small>
    </div>
  `;
}

function renderSignalRationale(result, modelKey) {
  return `
    <ul class="rationale">
      ${signalRationaleItems(result, modelKey).map(renderSignalRationaleItem).join("")}
    </ul>
  `;
}

function renderSignalRationaleItem(item) {
  const definition = item.definition;
  const value = item.score;
  const tooltip = signalTooltip(definition);
  return `
    <li
      class="${signalClass(value, definition.max)} has-tooltip"
      data-tooltip="${escapeHtml(tooltip)}"
      title="${escapeHtml(tooltip)}"
      tabindex="0"
    >
      <strong>${definition.label}:</strong> ${escapeHtml(item.text)}
    </li>
  `;
}

function signalRationaleItems(result, modelKey) {
  const definition = modelDefinition(modelKey);
  const rationales = result.model_rationales?.[modelKey] || [];
  return definition.signals.map((signal, index) => ({
    definition: signal,
    score: componentScore(result, modelKey, signal.key),
    text: rationales[index] || "No rationale returned.",
  }));
}

function signalClass(value, maxValue) {
  if (value === null || value === undefined || !maxValue || value <= 0) {
    return "signal-red";
  }

  const favorability = value / maxValue;
  if (favorability >= 0.75) return "signal-green";
  if (favorability >= 0.5) return "signal-yellow";
  if (favorability >= 0.25) return "signal-orange";
  return "signal-red";
}

function favorabilityLabel(value, maxValue) {
  const className = signalClass(value, maxValue);
  const scaleItem = favorabilityScale().find((item) => item.class === className);
  return scaleItem ? scaleItem.meaning : "Not favorable";
}

function signalTooltip(definition) {
  return `${definition.means} Calculation: ${definition.calculation} Favorability: ${definition.favorable}`;
}

function modelTooltip(definition) {
  const signalNames = definition.signals.map((signal) => signal.label.toLowerCase()).join(", ");
  return `${definition.category}: ${definition.definition} Signals: ${signalNames}.`;
}

function totalScoreTooltip(result) {
  const primaryModel = modelDefinition(result.primary_model);
  return `Top category score from 0 to ${formatNumber(scoreMax())}. The highest current model is ${primaryModel.label}.`;
}

function renderSignalGuide() {
  const chart = document.querySelector("#signal-guide-chart");
  const legend = document.querySelector("#signal-guide-legend");
  if (!chart) return;
  if (!modelMetadata) {
    if (legend) {
      legend.innerHTML = "";
    }
    chart.innerHTML = `<p class="status error">Scoring model metadata is unavailable.</p>`;
    return;
  }

  if (legend) {
    legend.innerHTML = `
    <div class="signal-legend" aria-label="Signal favorability color legend">
      ${favorabilityScale().map(legendItem).join("")}
    </div>
  `;
  }
  chart.innerHTML = modelOrder.map((key) => renderModelGuideCard(modelDefinition(key))).join("");
}

function favorabilityScale() {
  return Array.isArray(modelMetadata?.favorability_scale)
    ? modelMetadata.favorability_scale
    : [
        { class: "signal-red", label: "Red", meaning: "Not favorable" },
        { class: "signal-orange", label: "Orange", meaning: "Somewhat favorable" },
        { class: "signal-yellow", label: "Yellow", meaning: "Favorable" },
        { class: "signal-green", label: "Green", meaning: "Very favorable" },
      ];
}

function legendItem(item) {
  return `
    <span class="legend-item ${item.class}">
      <span class="legend-swatch"></span>
      <strong>${escapeHtml(item.label)}</strong>
      <small>${escapeHtml(item.meaning)}</small>
    </span>
  `;
}

function renderModelGuideCard(definition) {
  return `
    <article class="model-guide-card panel">
      <h3>${escapeHtml(definition.category)}: ${escapeHtml(definition.label)}</h3>
      <p>${escapeHtml(definition.definition)}</p>
      <div class="table-wrap signal-guide-table">
        <table>
          <thead>
            <tr>
              <th>Signal</th>
              <th>Weight</th>
              <th>What it means</th>
              <th>How it is calculated</th>
              <th>Favorable direction</th>
            </tr>
          </thead>
          <tbody>
            ${definition.signals.map(renderSignalGuideRow).join("")}
          </tbody>
        </table>
      </div>
    </article>
  `;
}

function renderSignalGuideRow(definition) {
  return `
    <tr>
      <td><strong>${definition.label}</strong></td>
      <td>${definition.max}</td>
      <td>${definition.means}</td>
      <td>${definition.calculation}</td>
      <td>${definition.favorable}</td>
    </tr>
  `;
}

function activeModelKeys(result) {
  if (modelOrder.length) {
    return modelOrder;
  }
  return result?.model_scores ? Object.keys(result.model_scores) : [];
}

function modelDefinition(key) {
  return modelDefinitions[key] || {
    key,
    category: "Model",
    label: String(key || "unknown").replace(/_/g, " "),
    definition: "Scoring metadata is unavailable for this model.",
    signals: [],
  };
}

function modelScore(result, modelKey) {
  return result.model_scores?.[modelKey] ?? (result.primary_model === modelKey ? result.score : null);
}

function componentScore(result, modelKey, signalKey) {
  return result.model_components?.[modelKey]?.[signalKey] ?? null;
}

function modelShortLabel(definition) {
  return definition.label
    .replace("Classical Short Squeeze", "Classical")
    .replace("Float Compression", "Float")
    .replace("Gamma Candidate", "Gamma");
}

function scoreMax() {
  return modelMetadata?.score_range?.maximum ?? modelMetadata?.total_weight ?? 100;
}

function renderErrors(errors) {
  if (!errors || !errors.length) {
    return;
  }
  const errorText = errors.map((error) => `${error.symbol}: ${error.message}`).join(" ");
  const errorEl = document.createElement("div");
  errorEl.className = "status error scan-error";
  errorEl.textContent = errorText;
  resultsEl.prepend(errorEl);
}

function formatNumber(value) {
  return value === null || value === undefined ? "N/A" : numberFormatter.format(value);
}

function formatCurrency(value) {
  return value === null || value === undefined ? "N/A" : currencyFormatter.format(value);
}

function formatCompact(value) {
  return value === null || value === undefined ? "N/A" : compactFormatter.format(value);
}

function formatPercent(value) {
  return value === null || value === undefined ? "N/A" : `${numberFormatter.format(value)}%`;
}

function formatMultiple(value) {
  return value === null || value === undefined ? "N/A" : `${numberFormatter.format(value)}x`;
}

function formatScanAge(minutes) {
  if (minutes === null || minutes === undefined) {
    return "unknown";
  }
  if (minutes < 1) {
    return "just now";
  }
  return `${numberFormatter.format(minutes)} min ago`;
}

function badgeClass(label) {
  const normalized = label.toLowerCase();
  if (normalized.includes("high")) return "high";
  if (normalized.includes("watch")) return "watch";
  if (normalized.includes("emerging")) return "emerging";
  return "low";
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
