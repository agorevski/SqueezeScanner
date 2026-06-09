const form = document.querySelector("#scan-form");
const button = document.querySelector("#scan-button");
const statusEl = document.querySelector("#status");
const resultsEl = document.querySelector("#results");
const summaryEl = document.querySelector("#summary");

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
let signalDefinitions = {};
let signalOrder = [];

document.addEventListener("DOMContentLoaded", async () => {
  await loadModelMetadata();
  loadRecentScans();
});

document.addEventListener("click", (event) => {
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

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setLoading(true);
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
  if (!model || !Array.isArray(model.signals)) {
    return;
  }

  modelMetadata = model;
  signalDefinitions = Object.fromEntries(
    model.signals.map((signal) => [
      signal.key,
      {
        key: signal.key,
        label: signal.label,
        max: signal.weight,
        means: signal.means,
        calculation: signal.calculation,
        favorable: signal.favorable,
      },
    ]),
  );
  signalOrder = model.signals.map((signal) => signal.key);
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail || "Request failed");
  }
  return payload;
}

function setLoading(isLoading) {
  button.disabled = isLoading;
  button.textContent = isLoading ? "Scanning..." : "Scan";
  if (isLoading) {
    statusEl.textContent = "Checking cache and refreshing stale market data.";
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
  const results = sortedScreenedResults();
  renderSummary(results);
  renderResults(results);
}

function renderSummary(results) {
  if (!results.length) {
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
    ${summaryCard("Screened", results.length)}
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

function renderResults(results) {
  if (!results.length) {
    resultsEl.innerHTML = "";
    return;
  }

  resultsEl.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Score</th>
            <th>Setup</th>
            <th>Price</th>
            <th>Short % Float</th>
            <th>Days Cover</th>
            <th>Rel Vol</th>
            <th>5D</th>
            <th>Float</th>
            <th>Scanned</th>
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
  return `
    <tr>
      <td>
        <strong>${escapeHtml(result.symbol)}</strong>
        <span>${escapeHtml(result.company_name || "")}</span>
      </td>
      <td><strong>${formatNumber(result.score)}</strong></td>
      <td><span class="badge ${badgeClass(result.risk_level)}">${escapeHtml(result.risk_level)}</span></td>
      <td>${formatCurrency(metrics.price)}</td>
      <td>${formatPercent(metrics.short_percent_float)}</td>
      <td>${formatNumber(metrics.short_ratio)}</td>
      <td>${formatMultiple(metrics.relative_volume)}</td>
      <td class="${trendClass(metrics.change_5d_pct)}">${formatSignedPercent(metrics.change_5d_pct)}</td>
      <td>${formatCompact(metrics.float_shares)}</td>
      <td>${formatScanAge(result.minutes_since_scan)}</td>
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
        <div
          class="score has-tooltip"
          data-tooltip="${escapeHtml(totalScoreTooltip())}"
          title="${escapeHtml(totalScoreTooltip())}"
          tabindex="0"
        >
          <span>${formatNumber(result.score)}</span>
          <small>${escapeHtml(result.risk_level)}</small>
        </div>
      </div>
      <div class="component-grid">
        ${signalOrder.map((key) => component(result.components[key], key)).join("")}
      </div>
      ${renderSignalRationale(result)}
      ${warnings}
    </article>
  `;
}

function component(value, key) {
  const definition = signalDefinition(key);
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

function renderSignalRationale(result) {
  return `
    <ul class="rationale">
      ${signalRationaleItems(result).map(renderSignalRationaleItem).join("")}
    </ul>
  `;
}

function renderSignalRationaleItem(item) {
  const definition = signalDefinition(item.key);
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

function signalRationaleItems(result) {
  const metrics = result.metrics;
  return [
    {
      key: "short_interest",
      score: result.components.short_interest,
      text: metrics.short_percent_float === null || metrics.short_percent_float === undefined
        ? "short interest unavailable"
        : `${formatPercent(metrics.short_percent_float)} of float sold short`,
    },
    {
      key: "days_to_cover",
      score: result.components.days_to_cover,
      text: metrics.short_ratio === null || metrics.short_ratio === undefined
        ? "days to cover unavailable"
        : `${formatNumber(metrics.short_ratio)} days to cover`,
    },
    {
      key: "relative_volume",
      score: result.components.relative_volume,
      text: metrics.relative_volume === null || metrics.relative_volume === undefined
        ? "relative volume unavailable"
        : `${formatMultiple(metrics.relative_volume)} relative volume`,
    },
    {
      key: "momentum",
      score: result.components.momentum,
      text: `1D ${formatSignedPercent(metrics.change_1d_pct)}, 5D ${formatSignedPercent(metrics.change_5d_pct)}, 20D ${formatSignedPercent(metrics.change_20d_pct)}`,
    },
    {
      key: "float_pressure",
      score: result.components.float_pressure,
      text: floatPressureText(metrics),
    },
    {
      key: "short_interest_trend",
      score: result.components.short_interest_trend,
      text: shortTrendText(metrics.short_interest_change_pct),
    },
  ];
}

function floatPressureText(metrics) {
  if (metrics.float_shares !== null && metrics.float_shares !== undefined) {
    return `${formatCompact(metrics.float_shares)} float shares`;
  }
  if (metrics.market_cap !== null && metrics.market_cap !== undefined) {
    return `${formatCompact(metrics.market_cap)} market cap fallback`;
  }
  return "float shares and market cap unavailable";
}

function shortTrendText(change) {
  if (change === null || change === undefined) {
    return "short-interest trend unavailable";
  }
  const direction = change >= 0 ? "increased" : "decreased";
  return `short interest ${direction} ${formatPercent(Math.abs(change))} vs prior month`;
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

function totalScoreTooltip() {
  const totalWeight = modelMetadata?.total_weight ?? 100;
  const signalNames = signalOrder.map((key) => signalDefinition(key).label.toLowerCase()).join(", ");
  return `Total squeeze setup score from 0 to ${formatNumber(totalWeight)}. It is recomputed from raw market data using ${signalNames}.`;
}

function renderSignalGuide() {
  const chart = document.querySelector("#signal-guide-chart");
  if (!chart) return;
  if (!modelMetadata) {
    chart.innerHTML = `<p class="status error">Scoring model metadata is unavailable.</p>`;
    return;
  }

  chart.innerHTML = `
    <div class="signal-legend" aria-label="Signal favorability color legend">
      ${favorabilityScale().map(legendItem).join("")}
    </div>
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
          ${signalOrder.map((key) => renderSignalGuideRow(signalDefinition(key))).join("")}
        </tbody>
      </table>
    </div>
  `;
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

function signalDefinition(key) {
  return signalDefinitions[key] || {
    key,
    label: key.replace(/_/g, " "),
    max: 1,
    means: "Scoring metadata is unavailable for this signal.",
    calculation: "Unavailable.",
    favorable: "Unavailable.",
  };
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

function formatSignedPercent(value) {
  if (value === null || value === undefined) {
    return "N/A";
  }
  const sign = value > 0 ? "+" : "";
  return `${sign}${numberFormatter.format(value)}%`;
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

function trendClass(value) {
  if (value > 0) return "positive";
  if (value < 0) return "negative";
  return "";
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
