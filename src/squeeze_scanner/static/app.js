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
const rankingModeEl = document.querySelector("#ranking-mode");
const rankingModelEl = document.querySelector("#ranking-model");
const savedScreenSelectEl = document.querySelector("#saved-screen-select");
const savedScreenNameEl = document.querySelector("#saved-screen-name");
const saveScreenButton = document.querySelector("#save-screen");
const updateScreenButton = document.querySelector("#update-screen");
const deleteScreenButton = document.querySelector("#delete-screen");
const savedScreenStatusEl = document.querySelector("#saved-screen-status");
const watchlistSelectEl = document.querySelector("#watchlist-select");
const watchlistNameEl = document.querySelector("#watchlist-name");
const watchlistSymbolsEl = document.querySelector("#watchlist-symbols");
const saveWatchlistButton = document.querySelector("#save-watchlist");
const scanWatchlistButton = document.querySelector("#scan-watchlist");
const deleteWatchlistButton = document.querySelector("#delete-watchlist");
const watchlistStatusEl = document.querySelector("#watchlist-status");
const alertsPanelBodyEl = document.querySelector("#alerts-panel-body");
const schedulerPanelBodyEl = document.querySelector("#scheduler-panel-body");
const reportsPanelBodyEl = document.querySelector("#reports-panel-body");
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
let savedScreens = [];
let savedScreensAvailable = false;
let watchlists = [];
let watchlistsAvailable = false;

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
  loadSavedScreens();
  loadWatchlists();
  loadRoadmapPanels();
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

[rankingModeEl, rankingModelEl].filter(Boolean).forEach((control) => {
  control.addEventListener("change", renderScreenedResults);
});

savedScreenSelectEl?.addEventListener("change", applySelectedSavedScreen);
saveScreenButton?.addEventListener("click", saveCurrentScreen);
updateScreenButton?.addEventListener("click", updateCurrentScreen);
deleteScreenButton?.addEventListener("click", deleteCurrentScreen);

watchlistSelectEl?.addEventListener("change", applySelectedWatchlist);
saveWatchlistButton?.addEventListener("click", saveCurrentWatchlist);
scanWatchlistButton?.addEventListener("click", scanCurrentWatchlist);
deleteWatchlistButton?.addEventListener("click", deleteCurrentWatchlist);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setLoading(true, "Scanning...", "Checking cache and refreshing stale market data.");
  clearErrors();

  const symbols = new FormData(form).get("symbols");

  try {
    const payload = await fetchJson("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols, ...apiRankingPayload() }),
    });

    applyScanPayload(payload);

    const count = payload.count ?? payload.results?.length ?? 0;
    statusEl.textContent = count
      ? `Scan complete. Added or refreshed ${count} screened stock${count === 1 ? "" : "s"}.`
      : "No ticker data was returned.";
    statusEl.className = count ? "status success" : "status error";
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
    const payload = await fetchJson(withRankingQuery("/api/scan/most-shorted?count=100"), {
      method: "POST",
    });

    applyScanPayload(payload);

    const errorCount = payload.errors?.length || 0;
    const count = payload.count ?? payload.results?.length ?? 0;
    statusEl.textContent = `Loaded Yahoo most-shorted stocks and analyzed ${count} ticker${
      count === 1 ? "" : "s"
    }${errorCount ? ` (${errorCount} unavailable)` : ""}.`;
    statusEl.className = count ? "status success" : "status error";
  } catch (error) {
    statusEl.textContent = `Could not load Yahoo most-shorted stocks: ${error.message}`;
    statusEl.className = "status error";
  } finally {
    setLoading(false);
  }
});

async function loadRecentScans() {
  try {
    const payload = await fetchJson(withRankingQuery("/api/scans/recent"));
    applyScanPayload(payload);
    statusEl.textContent = payload.count
      ? `Loaded ${payload.count} stock${payload.count === 1 ? "" : "s"} screened within the last hour.`
      : "No stocks have been screened within the last hour.";
    statusEl.className = payload.count ? "status success" : "status";
  } catch (error) {
    statusEl.textContent = `Could not load recent screened stocks: ${error.message}`;
    statusEl.className = "status error";
  }
}

function applyScanPayload(payload) {
  applyModelMetadata(payload?.model);
  renderSignalGuide();
  mergeResults(payload?.results);
  renderScreenedResults();
  renderErrors(payload?.errors);
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
  if (!modelOrder.length) {
    return;
  }

  populateModelSelect(filterModelEl, "Any top model");
  populateModelSelect(rankingModelEl, "Top/current model");
}

function populateModelSelect(selectEl, emptyLabel) {
  if (!selectEl) {
    return;
  }

  const currentValue = selectEl.value;
  selectEl.innerHTML = `
    <option value="">${escapeHtml(emptyLabel)}</option>
    ${modelOrder
      .map((key) => {
        const definition = modelDefinition(key);
        return `<option value="${escapeHtml(key)}">${escapeHtml(definition.label)}</option>`;
      })
      .join("")}
  `;

  if ([...selectEl.options].some((option) => option.value === currentValue)) {
    selectEl.value = currentValue;
  }
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let payload = {};
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text };
    }
  }
  if (!response.ok) {
    const message = payload.detail || payload.message || `${response.status} ${response.statusText}` || "Request failed";
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

async function loadSavedScreens() {
  setInlineStatus(savedScreenStatusEl, "Loading saved screens...");
  try {
    const payload = await fetchJson("/api/screens");
    savedScreens = extractCollection(payload, ["screens", "saved_screens", "items", "results"]);
    savedScreensAvailable = true;
    renderSavedScreens();
    setInlineStatus(
      savedScreenStatusEl,
      savedScreens.length
        ? `Loaded ${savedScreens.length} saved screen${savedScreens.length === 1 ? "" : "s"}.`
        : "No saved screens yet.",
      "success",
    );
  } catch (error) {
    savedScreens = [];
    savedScreensAvailable = false;
    renderSavedScreens();
    setInlineStatus(savedScreenStatusEl, `Saved screens unavailable: ${shortError(error)}`, "error");
  }
}

function renderSavedScreens() {
  if (!savedScreenSelectEl) {
    return;
  }

  const currentValue = savedScreenSelectEl.value;
  if (!savedScreensAvailable) {
    savedScreenSelectEl.innerHTML = `<option value="">Saved screens unavailable</option>`;
    savedScreenSelectEl.disabled = true;
    if (saveScreenButton) saveScreenButton.disabled = true;
    if (updateScreenButton) updateScreenButton.disabled = true;
    if (deleteScreenButton) deleteScreenButton.disabled = true;
    return;
  }

  savedScreenSelectEl.disabled = !savedScreens.length;
  savedScreenSelectEl.innerHTML = `
    <option value="">${savedScreens.length ? "Choose saved screen" : "No saved screens"}</option>
    ${savedScreens
      .map((screen) => `<option value="${escapeHtml(screenId(screen))}">${escapeHtml(screenName(screen))}</option>`)
      .join("")}
  `;
  if ([...savedScreenSelectEl.options].some((option) => option.value === currentValue)) {
    savedScreenSelectEl.value = currentValue;
  }
  if (saveScreenButton) saveScreenButton.disabled = false;
  updateSavedScreenButtons();
}

function updateSavedScreenButtons() {
  const hasSelection = Boolean(selectedSavedScreen());
  if (updateScreenButton) updateScreenButton.disabled = !savedScreensAvailable || !hasSelection;
  if (deleteScreenButton) deleteScreenButton.disabled = !savedScreensAvailable || !hasSelection;
}

function selectedSavedScreen() {
  const selectedId = savedScreenSelectEl?.value;
  return savedScreens.find((screen) => String(screenId(screen)) === String(selectedId));
}

function applySelectedSavedScreen() {
  const screen = selectedSavedScreen();
  updateSavedScreenButtons();
  if (!screen) {
    return;
  }

  if (savedScreenNameEl) {
    savedScreenNameEl.value = screenName(screen);
  }
  applyScreenDefinition(screenDefinition(screen));
  renderScreenedResults();
  setInlineStatus(savedScreenStatusEl, `Applied "${screenName(screen)}" without removing current results.`, "success");
}

async function saveCurrentScreen() {
  if (!savedScreensAvailable) {
    setInlineStatus(savedScreenStatusEl, "Saved-screen API is unavailable.", "error");
    return;
  }

  const name = savedScreenNameEl?.value.trim();
  if (!name) {
    setInlineStatus(savedScreenStatusEl, "Enter a saved-screen name first.", "error");
    return;
  }

  try {
    const definition = currentScreenDefinition();
    const payload = await sendJsonWithFallback("/api/screens", "POST", screenPayloads(name, definition));
    const created = firstObject(payload, ["screen", "saved_screen"]) || payload;
    await loadSavedScreens();
    const createdId = screenId(created);
    if (createdId && savedScreenSelectEl) {
      savedScreenSelectEl.value = String(createdId);
      updateSavedScreenButtons();
    }
    setInlineStatus(savedScreenStatusEl, `Saved "${name}".`, "success");
  } catch (error) {
    setInlineStatus(savedScreenStatusEl, `Could not save screen: ${shortError(error)}`, "error");
  }
}

async function updateCurrentScreen() {
  const screen = selectedSavedScreen();
  if (!screen) {
    setInlineStatus(savedScreenStatusEl, "Choose a saved screen to update.", "error");
    return;
  }

  const name = savedScreenNameEl?.value.trim() || screenName(screen);
  try {
    const definition = currentScreenDefinition();
    await sendJsonWithFallback(
      `/api/screens/${encodeURIComponent(screenId(screen))}`,
      "PUT",
      screenPayloads(name, definition),
    );
    await loadSavedScreens();
    if (savedScreenSelectEl) savedScreenSelectEl.value = String(screenId(screen));
    updateSavedScreenButtons();
    setInlineStatus(savedScreenStatusEl, `Updated "${name}".`, "success");
  } catch (error) {
    setInlineStatus(savedScreenStatusEl, `Could not update screen: ${shortError(error)}`, "error");
  }
}

async function deleteCurrentScreen() {
  const screen = selectedSavedScreen();
  if (!screen) {
    setInlineStatus(savedScreenStatusEl, "Choose a saved screen to delete.", "error");
    return;
  }

  try {
    await fetchJson(`/api/screens/${encodeURIComponent(screenId(screen))}`, { method: "DELETE" });
    if (savedScreenNameEl) savedScreenNameEl.value = "";
    await loadSavedScreens();
    setInlineStatus(savedScreenStatusEl, `Deleted "${screenName(screen)}".`, "success");
  } catch (error) {
    setInlineStatus(savedScreenStatusEl, `Could not delete screen: ${shortError(error)}`, "error");
  }
}

function currentScreenDefinition() {
  return {
    filters: {
      search: filterSearchEl?.value || "",
      model: filterModelEl?.value || "",
      risk: filterRiskEl?.value || "",
      minScore: numberFilterValue(filterMinScoreEl),
      minShortFloat: numberFilterValue(filterMinShortFloatEl),
      minRelativeVolume: numberFilterValue(filterMinRelativeVolumeEl),
      maxFloatMillions: numberFilterValue(filterMaxFloatEl),
    },
    ranking: currentRanking(),
    symbols: new FormData(form).get("symbols") || "",
  };
}

function applyScreenDefinition(definition) {
  const filters = definition?.filters || definition?.criteria || definition || {};
  const ranking = definition?.ranking || {};
  setValue(filterSearchEl, firstDefined(filters.search, filters.query, filters.symbol_search));
  setValue(filterModelEl, firstDefined(filters.model, filters.primary_model, filters.filter_model));
  setValue(filterRiskEl, firstDefined(filters.risk, filters.risk_level, filters.setup));
  setValue(filterMinScoreEl, firstDefined(filters.minScore, filters.min_score, filters.score_min));
  setValue(
    filterMinShortFloatEl,
    firstDefined(filters.minShortFloat, filters.min_short_float, filters.short_percent_float_min),
  );
  setValue(
    filterMinRelativeVolumeEl,
    firstDefined(filters.minRelativeVolume, filters.min_relative_volume, filters.relative_volume_min),
  );

  const maxFloatMillions = firstDefined(filters.maxFloatMillions, filters.max_float_millions);
  const maxFloatShares = firstDefined(filters.maxFloatShares, filters.max_float_shares, filters.max_float);
  setValue(filterMaxFloatEl, maxFloatMillions ?? (typeof maxFloatShares === "number" ? maxFloatShares / 1_000_000 : maxFloatShares));
  setValue(rankingModeEl, normalizeUiRankingMode(firstDefined(ranking.mode, definition?.ranking_mode, filters.ranking_mode)));
  setValue(rankingModelEl, firstDefined(ranking.model, definition?.ranking_model, filters.ranking_model));
}

function screenPayloads(name, definition) {
  return [
    { name, filters: definition },
    { name, filters_json: definition },
    { name, definition },
  ];
}

function screenId(screen) {
  return firstDefined(screen?.id, screen?.screen_id, screen?.uuid, screen?.name) || "";
}

function screenName(screen) {
  return firstDefined(screen?.name, screen?.label, screen?.title, screenId(screen), "Untitled screen");
}

function screenDefinition(screen) {
  return parseMaybeJson(
    firstDefined(screen?.filters, screen?.filters_json, screen?.definition, screen?.criteria, screen?.settings, {}),
  );
}

async function loadWatchlists() {
  setInlineStatus(watchlistStatusEl, "Loading watchlists...");
  try {
    const payload = await fetchJson("/api/watchlists");
    watchlists = extractCollection(payload, ["watchlists", "items", "results"]);
    watchlistsAvailable = true;
    renderWatchlists();
    setInlineStatus(
      watchlistStatusEl,
      watchlists.length
        ? `Loaded ${watchlists.length} watchlist${watchlists.length === 1 ? "" : "s"}.`
        : "No watchlists yet.",
      "success",
    );
  } catch (error) {
    watchlists = [];
    watchlistsAvailable = false;
    renderWatchlists();
    setInlineStatus(
      watchlistStatusEl,
      `Watchlist API unavailable: ${shortError(error)}. You can still scan typed symbols.`,
      "error",
    );
  }
}

function renderWatchlists() {
  if (!watchlistSelectEl) {
    return;
  }

  const currentValue = watchlistSelectEl.value;
  if (!watchlistsAvailable) {
    watchlistSelectEl.innerHTML = `<option value="">Watchlists unavailable</option>`;
    watchlistSelectEl.disabled = true;
    if (saveWatchlistButton) saveWatchlistButton.disabled = true;
    if (deleteWatchlistButton) deleteWatchlistButton.disabled = true;
    if (scanWatchlistButton) scanWatchlistButton.disabled = false;
    return;
  }

  watchlistSelectEl.disabled = !watchlists.length;
  watchlistSelectEl.innerHTML = `
    <option value="">${watchlists.length ? "Choose watchlist" : "No watchlists"}</option>
    ${watchlists
      .map((watchlist) => `<option value="${escapeHtml(watchlistId(watchlist))}">${escapeHtml(watchlistName(watchlist))}</option>`)
      .join("")}
  `;
  if ([...watchlistSelectEl.options].some((option) => option.value === currentValue)) {
    watchlistSelectEl.value = currentValue;
  }
  if (saveWatchlistButton) saveWatchlistButton.disabled = false;
  if (scanWatchlistButton) scanWatchlistButton.disabled = false;
  updateWatchlistButtons();
}

function updateWatchlistButtons() {
  const hasSelection = Boolean(selectedWatchlist());
  if (deleteWatchlistButton) deleteWatchlistButton.disabled = !watchlistsAvailable || !hasSelection;
}

function selectedWatchlist() {
  const selectedId = watchlistSelectEl?.value;
  return watchlists.find((watchlist) => String(watchlistId(watchlist)) === String(selectedId));
}

function applySelectedWatchlist() {
  const watchlist = selectedWatchlist();
  updateWatchlistButtons();
  if (!watchlist) {
    return;
  }

  if (watchlistNameEl) watchlistNameEl.value = watchlistName(watchlist);
  if (watchlistSymbolsEl) watchlistSymbolsEl.value = watchlistSymbols(watchlist).join(", ");
  setInlineStatus(watchlistStatusEl, `Loaded "${watchlistName(watchlist)}".`, "success");
}

async function saveCurrentWatchlist() {
  if (!watchlistsAvailable) {
    setInlineStatus(watchlistStatusEl, "Watchlist API is unavailable; typed symbols can still be scanned.", "error");
    return;
  }

  const name = watchlistNameEl?.value.trim();
  const symbols = normalizeSymbolsClient(watchlistSymbolsEl?.value || "");
  if (!name) {
    setInlineStatus(watchlistStatusEl, "Enter a watchlist name first.", "error");
    return;
  }
  if (!symbols.length) {
    setInlineStatus(watchlistStatusEl, "Enter at least one watchlist symbol.", "error");
    return;
  }

  const selected = selectedWatchlist();
  const payloads = watchlistPayloads(name, symbols);
  try {
    if (selected) {
      try {
        await sendJsonWithFallback(`/api/watchlists/${encodeURIComponent(watchlistId(selected))}`, "PUT", payloads);
        await syncWatchlistSymbols(watchlistId(selected), watchlistSymbols(selected), symbols);
      } catch (error) {
        if (![404, 405].includes(error.status)) {
          throw error;
        }
        await sendJsonWithFallback("/api/watchlists", "POST", payloads);
      }
    } else {
      await sendJsonWithFallback("/api/watchlists", "POST", payloads);
    }
    await loadWatchlists();
    setInlineStatus(watchlistStatusEl, `Saved "${name}".`, "success");
  } catch (error) {
    setInlineStatus(watchlistStatusEl, `Could not save watchlist: ${shortError(error)}`, "error");
  }
}

async function scanCurrentWatchlist() {
  const selected = selectedWatchlist();
  const selectedId = selected ? watchlistId(selected) : "";
  const symbols = watchlistSymbolsEl?.value || watchlistSymbols(selected).join(", ");

  setLoading(true, "Scanning...", "Scanning watchlist symbols.", scanWatchlistButton, "Scanning...");
  clearErrors();
  try {
    if (selectedId && watchlistsAvailable) {
      try {
        const payload = await fetchJson(`/api/watchlists/${encodeURIComponent(selectedId)}/scan`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(apiRankingPayload()),
        });
        applyScanPayload(payload);
        const count = payload.count ?? payload.results?.length ?? 0;
        setInlineStatus(watchlistStatusEl, `Scanned "${watchlistName(selected)}" (${count} result${count === 1 ? "" : "s"}).`, "success");
        statusEl.textContent = `Watchlist scan complete for "${watchlistName(selected)}".`;
        statusEl.className = count ? "status success" : "status";
        return;
      } catch (error) {
        if (!symbols || ![404, 405, 501].includes(error.status)) {
          throw error;
        }
        setInlineStatus(
          watchlistStatusEl,
          `Watchlist scan endpoint unavailable; scanning listed symbols instead.`,
          "error",
        );
      }
    }

    if (!symbols) {
      setInlineStatus(watchlistStatusEl, "Choose a watchlist or enter symbols to scan.", "error");
      return;
    }

    const payload = await fetchJson("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols, ...apiRankingPayload() }),
    });
    applyScanPayload(payload);
    const count = payload.count ?? payload.results?.length ?? 0;
    statusEl.textContent = `Scanned ${count} watchlist symbol${count === 1 ? "" : "s"}.`;
    statusEl.className = count ? "status success" : "status error";
  } catch (error) {
    statusEl.textContent = `Could not scan watchlist: ${shortError(error)}`;
    statusEl.className = "status error";
    setInlineStatus(watchlistStatusEl, `Could not scan watchlist: ${shortError(error)}`, "error");
  } finally {
    setLoading(false);
  }
}

async function deleteCurrentWatchlist() {
  const watchlist = selectedWatchlist();
  if (!watchlist) {
    setInlineStatus(watchlistStatusEl, "Choose a watchlist to delete.", "error");
    return;
  }

  try {
    await fetchJson(`/api/watchlists/${encodeURIComponent(watchlistId(watchlist))}`, { method: "DELETE" });
    if (watchlistNameEl) watchlistNameEl.value = "";
    if (watchlistSymbolsEl) watchlistSymbolsEl.value = "";
    await loadWatchlists();
    setInlineStatus(watchlistStatusEl, `Deleted "${watchlistName(watchlist)}".`, "success");
  } catch (error) {
    setInlineStatus(watchlistStatusEl, `Could not delete watchlist: ${shortError(error)}`, "error");
  }
}

async function syncWatchlistSymbols(watchlistIdValue, existingSymbols, desiredSymbols) {
  const existing = new Set(existingSymbols.map((symbol) => symbol.toUpperCase()));
  const desired = new Set(desiredSymbols.map((symbol) => symbol.toUpperCase()));
  const toAdd = [...desired].filter((symbol) => !existing.has(symbol));
  const toRemove = [...existing].filter((symbol) => !desired.has(symbol));

  if (toAdd.length) {
    await fetchJson(`/api/watchlists/${encodeURIComponent(watchlistIdValue)}/symbols`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols: toAdd }),
    });
  }

  for (const symbol of toRemove) {
    await fetchJson(`/api/watchlists/${encodeURIComponent(watchlistIdValue)}/symbols/${encodeURIComponent(symbol)}`, {
      method: "DELETE",
    });
  }
}

function watchlistPayloads(name, symbols) {
  const symbolText = symbols.join(", ");
  return [
    { name, symbols },
    { name, symbol_list: symbols },
    { name, symbols: symbolText },
  ];
}

function watchlistId(watchlist) {
  return firstDefined(watchlist?.id, watchlist?.watchlist_id, watchlist?.uuid, watchlist?.name) || "";
}

function watchlistName(watchlist) {
  return firstDefined(watchlist?.name, watchlist?.label, watchlist?.title, watchlistId(watchlist), "Untitled watchlist");
}

function watchlistSymbols(watchlist) {
  const rawSymbols = firstDefined(watchlist?.symbols, watchlist?.tickers, watchlist?.items, watchlist?.watchlist_symbols, []);
  if (Array.isArray(rawSymbols)) {
    return rawSymbols
      .map((item) => (typeof item === "string" ? item : firstDefined(item.symbol, item.ticker, item.value, "")))
      .filter(Boolean);
  }
  return normalizeSymbolsClient(rawSymbols);
}

function loadRoadmapPanels() {
  loadRoadmapPanel(alertsPanelBodyEl, "Alerts", ["/api/alerts", "/api/alert-events", "/api/alert-delivery-attempts"], [
    "alerts",
    "alert_events",
    "events",
    "items",
    "results",
  ]);
  loadRoadmapPanel(schedulerPanelBodyEl, "Scheduler", ["/api/scheduler", "/api/schedules", "/api/scheduled-scans"], [
    "schedules",
    "scheduled_scans",
    "runs",
    "items",
    "results",
  ]);
  loadRoadmapPanel(reportsPanelBodyEl, "Reports/history", ["/api/reports", "/api/scans/history", "/api/scheduled-scan-runs"], [
    "reports",
    "history",
    "rows",
    "items",
    "results",
  ]);
}

async function loadRoadmapPanel(element, label, urls, collectionKeys) {
  if (!element) {
    return;
  }

  try {
    const { payload, url } = await fetchFirstAvailable(urls);
    renderRoadmapPanel(element, label, payload, collectionKeys, url);
  } catch (error) {
    element.innerHTML = `<p>${escapeHtml(label)} endpoint unavailable. ${escapeHtml(shortError(error))}</p>`;
  }
}

async function fetchFirstAvailable(urls) {
  let lastError = null;
  for (const url of urls) {
    try {
      return { payload: await fetchJson(url), url };
    } catch (error) {
      lastError = error;
      if (error.status && ![404, 405, 501].includes(error.status)) {
        throw error;
      }
    }
  }
  throw lastError || new Error("No endpoint configured");
}

function renderRoadmapPanel(element, label, payload, collectionKeys, url) {
  let items = extractCollection(payload, collectionKeys);
  if (!items.length && payload && !Array.isArray(payload) && Object.keys(payload).length) {
    items = [payload];
  }

  if (!items.length) {
    element.innerHTML = `<p>No ${escapeHtml(label.toLowerCase())} returned from ${escapeHtml(url)}.</p>`;
    return;
  }

  element.innerHTML = `
    <ul class="roadmap-list">
      ${items.slice(0, 5).map(renderRoadmapItem).join("")}
    </ul>
  `;
}

function renderRoadmapItem(item) {
  const title = firstDefined(item.title, item.name, item.symbol, item.rule_name, item.report_name, item.status, "Untitled");
  const detail = firstDefined(
    item.message,
    item.description,
    item.detail,
    item.summary,
    item.target,
    item.schedule,
    item.last_run_at,
    item.created_at,
    "",
  );
  const meta = firstDefined(item.created_at, item.updated_at, item.run_at, item.next_run_at, item.generated_at, "");
  return `
    <li>
      <strong>${escapeHtml(title)}</strong>
      ${detail ? `<span>${escapeHtml(detail)}</span>` : ""}
      ${meta ? `<small>${escapeHtml(meta)}</small>` : ""}
    </li>
  `;
}

async function sendJsonWithFallback(url, method, payloads) {
  let lastError = null;
  for (const payload of payloads) {
    try {
      return await fetchJson(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (error) {
      lastError = error;
      if (![400, 422].includes(error.status)) {
        throw error;
      }
    }
  }
  throw lastError || new Error("Request failed");
}

function extractCollection(payload, keys) {
  if (Array.isArray(payload)) {
    return payload;
  }
  for (const key of keys) {
    const value = payload?.[key];
    if (Array.isArray(value)) {
      return value;
    }
  }
  const data = payload?.data;
  if (Array.isArray(data)) {
    return data;
  }
  return [];
}

function firstObject(payload, keys) {
  for (const key of keys) {
    if (payload?.[key] && typeof payload[key] === "object") {
      return payload[key];
    }
  }
  return null;
}

function parseMaybeJson(value) {
  if (typeof value !== "string") {
    return value || {};
  }
  try {
    return JSON.parse(value);
  } catch {
    return {};
  }
}

function setValue(element, value) {
  if (!element || value === undefined || value === null) {
    return;
  }
  element.value = String(value);
}

function setInlineStatus(element, message, tone = "") {
  if (!element) {
    return;
  }
  element.textContent = message;
  element.className = `inline-status${tone ? ` ${tone}` : ""}`;
}

function shortError(error) {
  return error?.message || "Request failed";
}

function normalizeSymbolsClient(value) {
  if (Array.isArray(value)) {
    return value.flatMap((item) => normalizeSymbolsClient(item));
  }
  return String(value || "")
    .split(/[\s,;]+/)
    .map((symbol) => symbol.trim().toUpperCase())
    .filter(Boolean);
}

function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== "");
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
  if (scanWatchlistButton) {
    scanWatchlistButton.disabled = isLoading;
  }

  button.textContent = isLoading && activeButton === button ? primaryLoadingText : "Scan";
  mostShortedButton.textContent =
    isLoading && activeButton === mostShortedButton ? activeLoadingText : "Load Yahoo most-shorted";
  if (scanWatchlistButton) {
    scanWatchlistButton.textContent =
      isLoading && activeButton === scanWatchlistButton ? activeLoadingText : "Scan watchlist";
  }

  if (isLoading) {
    statusEl.textContent = message;
    statusEl.className = "status";
  }
}

function clearErrors() {
  document.querySelectorAll(".scan-error").forEach((element) => element.remove());
}

function mergeResults(results) {
  (Array.isArray(results) ? results : []).forEach((result) => {
    if (result?.symbol) {
      screenedResults.set(result.symbol, result);
    }
  });
}

function sortedScreenedResults() {
  const ranking = currentRanking();
  return [...screenedResults.values()].sort((left, right) => {
    const rankComparison = compareRankedResults(left, right, ranking);
    if (rankComparison !== 0) return rankComparison;
    if ((right.score ?? -Infinity) !== (left.score ?? -Infinity)) {
      return (right.score ?? -Infinity) - (left.score ?? -Infinity);
    }
    if ((right.data_quality ?? -Infinity) !== (left.data_quality ?? -Infinity)) {
      return (right.data_quality ?? -Infinity) - (left.data_quality ?? -Infinity);
    }
    return String(left.symbol || "").localeCompare(String(right.symbol || ""));
  });
}

function currentRanking() {
  return {
    mode: rankingModeEl?.value || "top_score",
    model: rankingModelEl?.value || filterModelEl?.value || "",
  };
}

function apiRankingPayload() {
  const ranking = currentRanking();
  const mode = apiRankingMode(ranking.mode);
  const payload = { sort_direction: "desc" };
  if (mode) {
    payload.ranking_mode = mode;
  }
  if (ranking.model && mode === "selected_model_score") {
    payload.selected_model = ranking.model;
  }
  return payload;
}

function withRankingQuery(url) {
  const query = rankingQueryString();
  if (!query) {
    return url;
  }
  return `${url}${url.includes("?") ? "&" : "?"}${query}`;
}

function rankingQueryString() {
  const params = new URLSearchParams(apiRankingPayload());
  return params.toString();
}

function apiRankingMode(mode) {
  const modes = {
    top_score: "top_score",
    selected_model_score: "selected_model_score",
    confidence: "highest_model_confidence",
    highest_model_confidence: "highest_model_confidence",
    delta_1h: "score_increase_1h",
    score_delta_1h: "score_increase_1h",
    score_change_1h: "score_increase_1h",
    score_increase_1h: "score_increase_1h",
    delta_24h: "score_increase_24h",
    score_delta_24h: "score_increase_24h",
    score_change_24h: "score_increase_24h",
    score_increase_24h: "score_increase_24h",
    relative_volume: "relative_volume",
    short_interest: "short_interest",
    smallest_float: "smallest_float",
    hybrid_score: "hybrid_only",
    hybrid_only: "hybrid_only",
    gamma_candidate_score: "gamma_candidate_only",
    gamma_candidate_only: "gamma_candidate_only",
  };
  return modes[mode] || null;
}

function normalizeUiRankingMode(mode) {
  const modes = {
    model_confidence: "highest_model_confidence",
    highest_confidence: "highest_model_confidence",
    score_delta_1h: "score_increase_1h",
    score_change_1h: "score_increase_1h",
    delta_1h: "score_increase_1h",
    score_delta_24h: "score_increase_24h",
    score_change_24h: "score_increase_24h",
    delta_24h: "score_increase_24h",
    hybrid_score: "hybrid_only",
    gamma_score: "gamma_candidate_only",
    gamma_candidate_score: "gamma_candidate_only",
  };
  return modes[mode] || mode;
}

function compareRankedResults(left, right, ranking) {
  const direction = ranking.mode === "smallest_float" ? "asc" : "desc";
  const leftValue = rankValue(left, ranking);
  const rightValue = rankValue(right, ranking);
  const leftMissing = leftValue === null || leftValue === undefined || Number.isNaN(leftValue);
  const rightMissing = rightValue === null || rightValue === undefined || Number.isNaN(rightValue);

  if (leftMissing && rightMissing) return 0;
  if (leftMissing) return 1;
  if (rightMissing) return -1;
  if (leftValue === rightValue) return 0;
  return direction === "asc" ? leftValue - rightValue : rightValue - leftValue;
}

function rankValue(result, ranking) {
  const metrics = result?.metrics || {};
  switch (ranking.mode) {
    case "selected_model_score":
      return modelScore(result, ranking.model || result.primary_model);
    case "confidence":
    case "highest_model_confidence":
      return ranking.model ? confidenceRankValue(result, ranking.model) : bestConfidenceRankValue(result);
    case "delta_1h":
    case "score_increase_1h":
      return deltaValue(resultDelta(result, "1h"));
    case "delta_24h":
    case "score_increase_24h":
      return deltaValue(resultDelta(result, "24h"));
    case "previous_scan_delta":
      return deltaValue(resultDelta(result, "previous"));
    case "relative_volume":
      return metrics.relative_volume;
    case "short_interest":
      return metrics.short_percent_float;
    case "smallest_float":
      return metrics.float_shares;
    case "hybrid_score":
    case "hybrid_only":
      return modelScore(result, "hybrid");
    case "gamma_candidate_score":
    case "gamma_candidate_only":
      return modelScore(result, "gamma_candidate");
    case "top_score":
    default:
      return result?.score;
  }
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

  const highCount = results.filter((result) => resultRiskLevel(result).toLowerCase().includes("high")).length;
  const watchCount = results.filter((result) => resultRiskLevel(result).toLowerCase().includes("watch")).length;
  const best = results[0];
  const ranking = currentRanking();

  summaryEl.innerHTML = `
    ${summaryCard("Showing", `${results.length}/${totalCount}`)}
    ${summaryCard("High setups", highCount)}
    ${summaryCard("Watchlist", watchCount)}
    ${summaryCard(rankingSummaryLabel(ranking), best ? `${best.symbol} ${formatRankValue(best, ranking)}` : "None")}
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
  if (filters.risk && resultRiskLevel(result) !== filters.risk) {
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
            <th>Δ Prev</th>
            <th>Δ 1h</th>
            <th>Δ 24h</th>
            ${activeModelKeys(results[0]).map((key) => `<th>${escapeHtml(modelShortLabel(modelDefinition(key)))}</th>`).join("")}
            <th>Price</th>
            <th>Short % Float</th>
            <th>Rel Vol</th>
            <th>Float</th>
            <th>Risk flags</th>
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
  const metrics = result.metrics || {};
  const primaryModel = modelDefinition(result.primary_model);
  const riskLabel = resultRiskLevel(result);
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
      <td><span class="badge ${badgeClass(riskLabel)}">${escapeHtml(riskLabel)}</span></td>
      <td>${deltaChip(resultDelta(result, "previous"))}</td>
      <td>${deltaChip(resultDelta(result, "1h"))}</td>
      <td>${deltaChip(resultDelta(result, "24h"))}</td>
      ${activeModelKeys(result).map((key) => `<td>${modelScoreCell(result, key)}</td>`).join("")}
      <td>${formatCurrency(metrics.price)}</td>
      <td>${formatPercent(metrics.short_percent_float)}</td>
      <td>${formatMultiple(metrics.relative_volume)}</td>
      <td>${formatCompact(metrics.float_shares)}</td>
      <td>${renderRiskBadges(result, true)}</td>
      <td>${formatScanAge(result.minutes_since_scan)}</td>
      <td>${deleteButton(result.symbol)}</td>
    </tr>
  `;
}

function renderResultCard(result) {
  const warningsList = resultWarnings(result);
  const warnings = warningsList.length
    ? `<div class="warnings"><strong>Data notes:</strong> ${escapeHtml(warningsList.join(" "))}</div>`
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
      ${renderDeltaGrid(result)}
      ${renderGammaDetails(result)}
      ${renderRiskSection(result)}
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

function renderDeltaGrid(result) {
  return `
    <div class="delta-grid" aria-label="${escapeHtml(result.symbol)} score deltas">
      ${deltaCard("Previous scan", resultDelta(result, "previous"))}
      ${deltaCard("1 hour", resultDelta(result, "1h"))}
      ${deltaCard("24 hours", resultDelta(result, "24h"))}
    </div>
  `;
}

function deltaCard(label, rawValue) {
  const reason = deltaUnavailableReason(rawValue);
  return `
    <div class="delta-card">
      <span>${escapeHtml(label)}</span>
      ${deltaChip(rawValue)}
      ${reason ? `<p class="hint">${escapeHtml(reason)}</p>` : ""}
    </div>
  `;
}

function renderRiskSection(result) {
  const badges = renderRiskBadges(result, false);
  const warnings = guardrailWarnings(result);
  return `
    <div class="risk-section">
      <span>Risk flags & guardrails</span>
      ${badges || `<p>No risk flags returned.</p>`}
      ${warnings.length ? `<p>${escapeHtml(warnings.join(" "))}</p>` : ""}
    </div>
  `;
}

function renderGammaDetails(result) {
  const metrics = result.metrics || {};
  const hasGammaDetail = [
    metrics.gamma_exposure_source_type,
    metrics.gamma_exposure_pct_market_cap,
    metrics.dealer_gamma_exposure_pct_market_cap,
    metrics.net_gamma_exposure,
    metrics.near_gamma_exposure,
    metrics.gamma_flip_distance_pct,
    metrics.call_gamma_share_pct,
    metrics.call_wall_strike,
    metrics.put_wall_strike,
    metrics.largest_gamma_expiration,
    metrics.net_open_interest_change,
  ].some((value) => value !== undefined && value !== null && value !== "");
  if (!hasGammaDetail) {
    return "";
  }

  const items = [
    ["Source", gammaSourceLabel(metrics)],
    ["GEX % cap", formatPercent(firstDefined(metrics.gamma_exposure_pct_market_cap, metrics.dealer_gamma_exposure_pct_market_cap))],
    ["Net GEX", formatCompact(metrics.net_gamma_exposure)],
    ["Near GEX", formatCompact(metrics.near_gamma_exposure)],
    ["Flip", formatPercent(metrics.gamma_flip_distance_pct)],
    ["Call gamma", formatPercent(metrics.call_gamma_share_pct)],
    ["Call wall", formatStrikeDistance(metrics.call_wall_strike, metrics.call_wall_distance_pct)],
    ["Put wall", formatStrikeDistance(metrics.put_wall_strike, metrics.put_wall_distance_pct)],
    ["Largest expiry", formatGammaExpiration(metrics)],
    ["OI Δ", formatSignedCompact(metrics.net_open_interest_change)],
  ].filter(([, value]) => value && value !== "N/A");

  if (!items.length) {
    return "";
  }

  return `
    <div class="risk-section gamma-details">
      <span>Gamma details</span>
      <div class="risk-flags">
        ${items
          .map(([label, value]) => `<span class="risk-badge info" title="${escapeHtml(label)}">${escapeHtml(label)}: ${escapeHtml(value)}</span>`)
          .join("")}
      </div>
    </div>
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

function modelScoreCell(result, modelKey) {
  return `
    <strong>${formatNumber(modelScore(result, modelKey))}</strong>
    ${confidenceBadge(modelConfidence(result, modelKey), true)}
  `;
}

function modelScoreTile(result, modelKey) {
  const definition = modelDefinition(modelKey);
  const value = modelScore(result, modelKey);
  const confidence = modelConfidence(result, modelKey);
  const tooltip = [modelTooltip(definition), confidenceTooltip(result, modelKey)].filter(Boolean).join(" ");
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
      ${confidenceBadge(confidence)}
    </div>
  `;
}

function confidenceBadge(confidence, compact = false) {
  if (confidence === null || confidence === undefined) {
    return `<span class="confidence-badge unavailable">${compact ? "Conf N/A" : "Confidence N/A"}</span>`;
  }

  const numeric = confidenceNumber(confidence);
  const label = formatConfidence(confidence);
  return `<span class="confidence-badge ${confidenceClass(numeric)}">${compact ? "Conf" : "Confidence"} ${escapeHtml(label)}</span>`;
}

function modelConfidence(result, modelKey) {
  const source = firstDefined(result?.model_confidence, result?.confidence_by_model, result?.confidence);
  if (source === undefined || source === null) {
    return null;
  }
  if (typeof source === "number" || typeof source === "string") {
    return result?.primary_model === modelKey ? source : null;
  }
  const value = firstDefined(source[modelKey], source[modelDefinition(modelKey).label], source[modelDefinition(modelKey).category]);
  if (value && typeof value === "object") {
    return firstDefined(value.value, value.confidence, value.score, value.level, value.label);
  }
  return value ?? null;
}

function confidenceTooltip(result, modelKey) {
  const source = firstDefined(result?.confidence_rationales, result?.model_confidence_rationales);
  const rationale = source?.[modelKey];
  if (Array.isArray(rationale)) {
    return `Confidence rationale: ${rationale.join(" ")}`;
  }
  if (rationale) {
    return `Confidence rationale: ${rationale}`;
  }
  return modelConfidence(result, modelKey) === null ? "Confidence data is unavailable for this model." : "";
}

function confidenceRankValue(result, modelKey) {
  return confidenceNumber(modelConfidence(result, modelKey));
}

function bestConfidenceRankValue(result) {
  const values = activeModelKeys(result).map((key) => confidenceRankValue(result, key)).filter((value) => value !== null);
  return values.length ? Math.max(...values) : null;
}

function confidenceNumber(confidence) {
  if (confidence === null || confidence === undefined) {
    return null;
  }
  if (typeof confidence === "number") {
    return confidence <= 1 ? confidence * 100 : confidence;
  }
  const parsed = Number(String(confidence).replace("%", ""));
  if (Number.isFinite(parsed)) {
    return parsed <= 1 && !String(confidence).includes("%") ? parsed * 100 : parsed;
  }
  const normalized = String(confidence).toLowerCase();
  if (normalized.includes("high")) return 90;
  if (normalized.includes("medium") || normalized.includes("moderate")) return 60;
  if (normalized.includes("low")) return 30;
  return null;
}

function formatConfidence(confidence) {
  const numeric = confidenceNumber(confidence);
  if (numeric !== null) {
    return `${numberFormatter.format(numeric)}%`;
  }
  return String(confidence);
}

function confidenceClass(numeric) {
  if (numeric === null) return "unavailable";
  if (numeric >= 70) return "high";
  if (numeric < 40) return "low";
  return "medium";
}

function resultDelta(result, bucket) {
  if (bucket === "previous") {
    return firstDefined(
      result?.previous_scan_delta,
      result?.previous_delta,
      result?.score_delta_previous,
      result?.score_deltas?.previous,
      result?.score_changes?.previous,
    );
  }
  if (bucket === "1h") {
    return firstDefined(
      result?.delta_1h,
      result?.score_delta_1h,
      result?.score_change_1h,
      result?.score_increase_1h,
      result?.score_deltas?.["1h"],
      result?.score_deltas?.one_hour,
      result?.score_changes?.["1h"],
      result?.score_increases?.["1h"],
    );
  }
  if (bucket === "24h") {
    return firstDefined(
      result?.delta_24h,
      result?.score_delta_24h,
      result?.score_change_24h,
      result?.score_increase_24h,
      result?.score_deltas?.["24h"],
      result?.score_deltas?.["1d"],
      result?.score_changes?.["24h"],
      result?.score_increases?.["24h"],
    );
  }
  return null;
}

function deltaChip(rawValue) {
  const value = deltaValue(rawValue);
  const reason = deltaUnavailableReason(rawValue);
  return `
    <span class="delta-chip ${deltaClass(value)}" title="${escapeHtml(reason || "Score delta")}">
      ${formatDelta(value)}
    </span>
  `;
}

function deltaValue(rawValue) {
  if (rawValue === null || rawValue === undefined) {
    return null;
  }
  if (typeof rawValue === "number") {
    return rawValue;
  }
  if (typeof rawValue === "string") {
    const parsed = Number(rawValue);
    return Number.isFinite(parsed) ? parsed : null;
  }
  const direct = firstDefined(rawValue.score_delta, rawValue.delta, rawValue.value, rawValue.points);
  if (typeof direct === "number") {
    return direct;
  }
  if (direct !== undefined) {
    const parsed = Number(direct);
    if (Number.isFinite(parsed)) return parsed;
  }
  if (typeof rawValue.current_score === "number" && typeof rawValue.previous_score === "number") {
    return rawValue.current_score - rawValue.previous_score;
  }
  return null;
}

function formatDelta(value) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "N/A";
  }
  if (value > 0) return `+${numberFormatter.format(value)}`;
  return numberFormatter.format(value);
}

function deltaClass(value) {
  if (value === null || value === undefined || Number.isNaN(value) || value === 0) {
    return "neutral";
  }
  return value > 0 ? "positive" : "negative";
}

function deltaUnavailableReason(rawValue) {
  if (rawValue === null || rawValue === undefined) {
    return "Not enough history returned yet.";
  }
  if (typeof rawValue === "object") {
    return firstDefined(rawValue.reason, rawValue.message, rawValue.status, rawValue.detail, "");
  }
  return "";
}

function renderRiskBadges(result, compact) {
  const flags = riskFlags(result);
  if (!flags.length) {
    return compact ? `<span class="muted">N/A</span>` : "";
  }

  const badges = flags
    .map(
      (flag) =>
        `<span class="risk-badge ${escapeHtml(flag.severity)}" title="${escapeHtml(flag.description)}">${escapeHtml(
          flag.label,
        )}</span>`,
    )
    .join("");
  return compact ? badges : `<div class="risk-flags">${badges}</div>`;
}

function riskFlags(result) {
  const rawFlags = firstDefined(result?.risk_flags, result?.guardrail_flags, []);
  const flags = [];
  if (Array.isArray(rawFlags)) {
    flags.push(...rawFlags.map(normalizeRiskFlag));
  } else if (rawFlags && typeof rawFlags === "object") {
    flags.push(
      ...Object.entries(rawFlags)
        .filter(([, value]) => Boolean(value))
        .map(([key, value]) => normalizeRiskFlag(typeof value === "object" ? { key, ...value } : { key, label: key })),
    );
  } else if (rawFlags) {
    flags.push(normalizeRiskFlag(rawFlags));
  }

  return flags.filter((flag) => flag.label);
}

function normalizeRiskFlag(flag) {
  if (typeof flag === "string") {
    return { label: flag, severity: riskSeverity(flag), description: flag };
  }
  const label = firstDefined(flag.label, flag.name, flag.key, flag.code, "Risk flag");
  const description = firstDefined(flag.description, flag.message, flag.detail, label);
  return {
    label,
    description,
    severity: riskSeverity(firstDefined(flag.severity, flag.level, flag.type, label)),
  };
}

function riskSeverity(value) {
  const normalized = String(value || "").toLowerCase();
  if (normalized.includes("critical")) return "critical";
  if (normalized.includes("high") || normalized.includes("danger")) return "high";
  if (normalized.includes("warn") || normalized.includes("medium")) return "warning";
  if (normalized.includes("low")) return "low";
  return "info";
}

function guardrailWarnings(result) {
  const warnings = firstDefined(result?.guardrail_warnings, result?.guardrails, []);
  if (Array.isArray(warnings)) {
    return warnings.map((warning) => (typeof warning === "string" ? warning : firstDefined(warning.message, warning.label, ""))).filter(Boolean);
  }
  if (warnings && typeof warnings === "object") {
    return Object.values(warnings).map((warning) => String(warning)).filter(Boolean);
  }
  return warnings ? [String(warnings)] : [];
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
  return result?.model_scores?.[modelKey] ?? (result?.primary_model === modelKey ? result?.score : null);
}

function componentScore(result, modelKey, signalKey) {
  return result?.model_components?.[modelKey]?.[signalKey] ?? null;
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

function resultWarnings(result) {
  const warnings = firstDefined(result?.warnings, result?.source_warnings, []);
  if (Array.isArray(warnings)) {
    return warnings.map((warning) => String(warning)).filter(Boolean);
  }
  return warnings ? [String(warnings)] : [];
}

function resultRiskLevel(result) {
  return firstDefined(result?.risk_level, result?.setup, result?.risk, "Unavailable");
}

function rankingSummaryLabel(ranking) {
  const labels = {
    top_score: "Top score",
    selected_model_score: "Top selected model",
    confidence: "Top confidence",
    highest_model_confidence: "Top confidence",
    delta_1h: "Top 1h delta",
    score_increase_1h: "Top 1h delta",
    delta_24h: "Top 24h delta",
    score_increase_24h: "Top 24h delta",
    previous_scan_delta: "Top prev delta",
    relative_volume: "Top rel volume",
    short_interest: "Top short % float",
    smallest_float: "Smallest float",
    hybrid_score: "Top hybrid",
    hybrid_only: "Top hybrid",
    gamma_candidate_score: "Top gamma",
    gamma_candidate_only: "Top gamma",
  };
  return labels[ranking.mode] || "Top result";
}

function formatRankValue(result, ranking) {
  const value = rankValue(result, ranking);
  if (["confidence", "highest_model_confidence"].includes(ranking.mode)) {
    return value === null ? "N/A" : `${numberFormatter.format(value)}%`;
  }
  if (ranking.mode.includes("delta") || ranking.mode.includes("increase")) return formatDelta(value);
  if (ranking.mode === "relative_volume") return formatMultiple(value);
  if (ranking.mode === "short_interest") return formatPercent(value);
  if (ranking.mode === "smallest_float") return formatCompact(value);
  return formatNumber(value);
}

function gammaSourceLabel(metrics) {
  const sourceType = metrics.gamma_exposure_source_type;
  const sourceName = metrics.gamma_exposure_source ? ` (${metrics.gamma_exposure_source})` : "";
  if (sourceType === "true_greeks") return `true greeks${sourceName}`;
  if (sourceType === "proxy") return `proxy${sourceName}`;
  return "missing";
}

function formatStrikeDistance(strike, distancePct) {
  if (strike === null || strike === undefined || Number.isNaN(strike)) {
    return "N/A";
  }
  const distance = distancePct === null || distancePct === undefined || Number.isNaN(distancePct)
    ? ""
    : ` (${numberFormatter.format(Math.abs(distancePct))}% ${distancePct >= 0 ? "above" : "below"})`;
  return `${formatCurrency(strike)}${distance}`;
}

function formatGammaExpiration(metrics) {
  if (!metrics.largest_gamma_expiration) {
    return "N/A";
  }
  const dte = metrics.largest_gamma_expiration_days_to_expiration;
  return dte === null || dte === undefined || Number.isNaN(dte)
    ? String(metrics.largest_gamma_expiration)
    : `${metrics.largest_gamma_expiration} (${numberFormatter.format(dte)} DTE)`;
}

function formatSignedCompact(value) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "N/A";
  }
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${formatCompact(value)}`;
}

function formatNumber(value) {
  return value === null || value === undefined || Number.isNaN(value) ? "N/A" : numberFormatter.format(value);
}

function formatCurrency(value) {
  return value === null || value === undefined || Number.isNaN(value) ? "N/A" : currencyFormatter.format(value);
}

function formatCompact(value) {
  return value === null || value === undefined || Number.isNaN(value) ? "N/A" : compactFormatter.format(value);
}

function formatPercent(value) {
  return value === null || value === undefined || Number.isNaN(value) ? "N/A" : `${numberFormatter.format(value)}%`;
}

function formatMultiple(value) {
  return value === null || value === undefined || Number.isNaN(value) ? "N/A" : `${numberFormatter.format(value)}x`;
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
  const normalized = String(label || "").toLowerCase();
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
