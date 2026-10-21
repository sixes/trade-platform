(function () {
  "use strict";

  const state = {
    config: null,
    market: null,
    range: 252,
    charts: {},
    status: null,
    result: null,
    lastJobId: null,
    waitDeadline: null,
    pollTimer: null,
  };

  const FILTER_FIELDS = ["dte_min", "dte_max", "delta_min", "delta_max", "min_open_interest", "max_spread_pct", "min_annualized_yield_pct", "target_delta", "target_strike", "strike_mode", "max_results"];
  const OPTIONAL_FIELDS = new Set(["min_annualized_yield_pct", "target_delta", "target_strike"]);
  const STORAGE_KEY = "trade-plat.filters";

  const $ = (sel) => document.querySelector(sel);
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 2, suffix = "") => (v == null || Number.isNaN(v) ? "-" : Number(v).toFixed(d) + suffix);
  const signed = (v, d = 2) => (v == null ? "-" : (v > 0 ? "+" : "") + Number(v).toFixed(d));
  const badge = (level, text) => `<span class="badge badge-${esc(String(level || "muted").replace(/\s+/g, "_"))}">${esc(text || level || "-")}</span>`;
  const ticker = (symbol) => String(symbol || "").replace(/\.US$/i, "");

  async function api(path, options) {
    const res = await fetch(path, Object.assign({ headers: { "content-type": "application/json" } }, options || {}));
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = body.detail;
      const message = Array.isArray(detail) ? detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ") : detail;
      throw new Error(message || `${res.status} ${res.statusText}`);
    }
    return body;
  }

  // ------------------------------------------------------------------ market / charts
  async function loadMarket() {
    try {
      state.market = await api("/api/market");
      renderIndexStats();
      renderFearGreed(state.market.fear_greed, state.market.sources && state.market.sources.fear_greed);
      renderMiniRow("mini-charts", state.market.indices || {}, "index");
      renderRowSummaries();
      renderCharts();
      renderRegime(state.market.regime);
      const src = state.market.sources || {};
      $("#skew-source").textContent = src.skew ? `source: ${src.skew.source}, through ${src.skew.last_date}` : "";
      if (!$("#scenario-select").value) loadFilters();
    } catch (err) {
      Object.keys(state.charts).filter((key) => key.startsWith("mini-charts:")).forEach(clearSeriesChart);
      $("#mini-charts").querySelectorAll(".mini-stats").forEach((el) => { el.textContent = "unavailable · update failed"; });
      $("#regime-body").innerHTML = `<div class="error-text">Could not load index data: ${esc(err.message)}</div>`;
    }
  }

  // Unit-aware formatting for the mini cards: yields in % with bp changes, ETF prices in $, indices plain.
  const fmtValue = (v, unit) => (v == null ? "-" : unit === "pct" ? `${num(v, 2)}%` : unit === "bp" ? `${num(v, 0)} bp` : unit === "usd" ? `$${num(v, 2)}` : unit === "jpy" ? `¥${num(v, 0)}` : num(v, unit === "fx" ? 3 : 2));
  const fmtPrecise = (v, unit) => (v == null ? "-" : unit === "pct" ? `${num(v, 3)}%` : unit === "bp" ? `${num(v, 1)} bp` : unit === "usd" ? `$${num(v, 2)}` : unit === "jpy" ? `¥${num(v, 1)}` : unit === "vol" ? num(v, 2) : num(v, 3));
  const fmtChange = (chg, unit, base) => {
    if (chg == null) return "-";
    if (unit === "pct") return `${signed(chg * 100, 0)} bp`;
    if (unit === "bp") return `${signed(chg, 1)} bp`;
    if (unit === "usd" || unit === "jpy") return `${signed(chg, unit === "jpy" ? 0 : 2)}${base ? ` (${signed((chg / (base - chg)) * 100, 2)}%)` : ""}`;
    return signed(chg, unit === "fx" ? 3 : 2);
  };
  const fmtRange = (lo, hi, unit) => (lo == null || hi == null ? "-" : unit === "pct" ? `${num(lo, 2)}-${num(hi, 2)}%` : unit === "bp" ? `${num(lo, 0)} - ${num(hi, 0)} bp` : unit === "jpy" ? `¥${num(lo, 0)} - ¥${num(hi, 0)}` : `${num(lo, unit === "usd" ? 2 : unit === "fx" ? 3 : 1)} - ${num(hi, unit === "usd" ? 2 : unit === "fx" ? 3 : 1)}`);

  function renderMiniRow(containerId, entries, defaultUnit) {
    const container = document.getElementById(containerId);
    const names = Object.keys(entries);
    if (container.dataset.built !== names.join(",")) {
      container.innerHTML = names.map((name) => `
        <div class="mini" data-index="${esc(name)}">
          <div class="mini-head"><b>${esc(name)}</b><span class="desc">${esc(entries[name].label || "")}</span></div>
          <div class="mini-stats" id="${containerId}-stats-${esc(name)}"></div>
          <div class="mini-chart"><canvas id="${containerId}-chart-${esc(name)}"></canvas></div>
        </div>`).join("");
      container.dataset.built = names.join(",");
      Object.keys(state.charts).filter((k) => k.startsWith(`${containerId}:`)).forEach((k) => { state.charts[k].destroy(); delete state.charts[k]; });
    }
    names.forEach((name) => {
      const entry = entries[name];
      const m = entry.metrics || {};
      const unit = entry.unit || defaultUnit || "index";
      const el = document.getElementById(`${containerId}-stats-${name}`);
      if (entry.error || !entry.series?.length) {
        clearSeriesChart(`${containerId}:${name}`);
        el.innerHTML = `<span class="muted small">unavailable</span>`;
        return;
      }
      const chg = m.change_1d;
      const source = entry.source ? `<span class="sub" title="${esc(entry.source)}">${esc(entry.source)} · ${esc(m.date || entry.series.at(-1)?.d || "date unavailable")}</span>` : "";
      el.innerHTML = `
        <div class="main"><span class="value">${fmtValue(m.value, unit)}</span><span class="sub ${chg > 0 ? "up" : chg < 0 ? "down" : ""}">${fmtChange(chg, unit, m.value)} 1d</span></div>
        <div class="row"><span class="sub">${fmtChange(m.change_5d, unit, m.value)} 5d</span><span class="sub">pctl ${num(m.percentile, 0)}</span><span class="sub">52w ${fmtRange(m.low_52w, m.high_52w, unit)}</span>${source}</div>`;
    });
  }

  function renderRowSummaries() {
    const ts = state.market.term_structure || {};
    const tsParts = Object.values(ts).map((t) => `${t.numerator}/${t.denominator} ${num(t.value, 3)} (${t.state})`);
    $("#term-structure").textContent = tsParts.length ? `term structure: ${tsParts.join(" | ")}` : "";

    const vix = state.market.vix.metrics, skew = state.market.skew.metrics, fg = state.market.fear_greed;
    setSummary("card-vix", `${num(vix.value)} ${vix.level || ""} · IV rank ${num(vix.iv_rank, 0)}`);
    setSummary("card-skew", `${num(skew.value)} ${skew.level || ""}`);
    setSummary("card-fg", fg && fg.score != null ? `${num(fg.score, 0)} ${fg.rating}` : "unavailable");
    setSummary("card-volcomplex", Object.entries(state.market.indices || {}).filter(([, d]) => !d.error).map(([k, d]) => `${k} ${num(d.metrics.value, 2)}`).join(" · "));
    setSummary("regime-card", state.market.regime ? state.market.regime.title : "");
  }

  // ------------------------------------------------------------------ live chart sections (rates/credit/dollar, commodities)
  const liveSections = new Map();  // section id -> controller

  const timeLabel = (t, range) => {
    if (!t || t.length <= 10) return t || "";
    const day = t.slice(5, 10), hm = t.slice(11, 16);
    return range === "1d" ? hm : `${day} ${hm}`;
  };

  const DAILY_POINTS = { "3m": 63, "6m": 126, "1y": 252, "3y": 756, "5y": 1260 };
  const SYMBOL_RE = /^[A-Z0-9][A-Z0-9.\-]{0,14}$/;
  // Series keys can hold dots (e.g. 700.HK); DOM ids and selectors need something safer.
  const domKey = (name) => String(name).replace(/[^A-Za-z0-9_-]/g, "_");
  const isSessionStock = (ctl, name, range) => Array.isArray(ctl.symbols) && range === "1d" && (!name.includes(".") || name.endsWith(".US"));

  function clearSeriesChart(key) {
    if (state.charts[key]) { state.charts[key].destroy(); delete state.charts[key]; }
  }

  function buildLiveSections(sections, defaultRange) {
    const host = $("#live-sections");
    const template = $("#live-section-template");
    (sections || []).forEach((section) => {
      let node;
      if (section.hidden) {
        // Hidden sections feed an existing card (the VIX card) instead of a generic live-section card.
        node = document.getElementById(`card-${section.id}`);
        if (!node) return;
      } else {
        node = template.content.firstElementChild.cloneNode(true);
        node.id = `card-${section.id}`;
        node.querySelector("h2").textContent = section.title;
        host.appendChild(node);
      }
      const ctl = {
        id: section.id, card: node, curve: !!section.curve, chartMode: section.chart_mode, range: null, timer: null, data: null, inflight: false,
        groups: section.groups || [], ranges: section.ranges,
        storageKey: `trade-plat.liveRange.${section.id}`, defaultRange: section.hidden ? "1y" : (section.default_range || defaultRange || "1d"),
        variantsKey: `trade-plat.liveVariants.${section.id}`, variants: {},
        symbolsKey: `trade-plat.liveSymbols.${section.id}`, symbols: null, defaultSymbols: null,
        render: section.id === "vix" ? renderVixLive : section.groups?.length ? renderComparisons : null,
      };
      if (ctl.groups.length) {
        node.classList.add("comparison-section");
        node.querySelector(".range-buttons").innerHTML = ctl.ranges.map((range) => `<button data-range="${esc(range)}">${esc(range.toUpperCase())}</button>`).join("");
        const breadth = document.getElementById("card-breadth");
        if (breadth) node.after(breadth);
      }
      try { ctl.variants = JSON.parse(localStorage.getItem(ctl.variantsKey) || "{}"); } catch (e) { ctl.variants = {}; }
      if (Array.isArray(section.symbols)) initSymbolEditor(ctl, section.symbols);
      node.querySelectorAll(".range-buttons button").forEach((btn) => {
        btn.addEventListener("click", () => {
          ctl.range = btn.dataset.range;
          try { localStorage.setItem(ctl.storageKey, ctl.range); } catch (e) { /* ignore */ }
          if (ctl.id === "vix" && DAILY_POINTS[ctl.range]) {
            // The VIX card's daily ranges also drive the other daily charts, as before.
            state.range = DAILY_POINTS[ctl.range];
            if (state.market) renderCharts();
          }
          loadLiveSection(ctl);
        });
      });
      liveSections.set(section.id, ctl);
    });
    const breadth = document.getElementById("breadth-chart");
    if (breadth) {
      const configuration = {
        symbols: [["Above 50-day SMA", "INDEX:S5FI|1D"]],
        compareSymbol: {symbol: "INDEX:S5TH", lineColor: "#f5a524", lineWidth: 2},
        chartOnly: false, autosize: true, width: "100%", height: "100%", locale: "en",
        colorTheme: "dark", showVolume: false, showMA: false,
        hideDateRanges: false, hideMarketStatus: true, hideSymbolLogo: true,
        scalePosition: "right", scaleMode: "Normal", valuesTracking: "1",
        changeMode: "price-only", chartType: "line", lineWidth: 2, lineType: 0,
        dateRanges: ["12m|1D", "3m|1D", "60m|1D"],
      };
      breadth.src = "https://www.tradingview-widget.com/embed-widget/symbol-overview/?locale=en#" + encodeURIComponent(JSON.stringify(configuration));
    }
  }

  // Symbol sections (e.g. stocks) chart a ticker list the user can edit; the list is kept per browser and
  // sent as `symbols=` so the server never stores per-user state. Tickers are shown without the ".US" suffix.
  function initSymbolEditor(ctl, defaults) {
    ctl.defaultSymbols = defaults.map(ticker);
    ctl.symbols = ctl.defaultSymbols.slice();
    try {
      const saved = JSON.parse(localStorage.getItem(ctl.symbolsKey) || "null");
      if (Array.isArray(saved)) ctl.symbols = saved.map((s) => ticker(String(s).toUpperCase())).filter((s) => SYMBOL_RE.test(s));
    } catch (e) { /* keep defaults */ }
    const form = ctl.card.querySelector(".symbol-editor");
    const input = form.querySelector("input");
    const hint = form.querySelector(".symbol-hint");
    form.classList.remove("hidden");
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const max = (state.config && state.config.live_max_symbols) || 12;
      const wanted = input.value.toUpperCase().split(/[\s,;]+/).map((s) => ticker(s.trim())).filter(Boolean);
      const bad = wanted.filter((s) => !SYMBOL_RE.test(s));
      if (bad.length) { hint.textContent = `invalid ticker: ${bad.join(", ")}`; return; }
      const next = ctl.symbols.slice();
      wanted.forEach((s) => { if (!next.includes(s)) next.push(s); });
      if (next.length > max) { hint.textContent = `at most ${max} tickers`; return; }
      if (next.length === ctl.symbols.length) { hint.textContent = wanted.length ? "already charted" : ""; return; }
      hint.textContent = "";
      input.value = "";
      setSymbols(ctl, next);
    });
    form.querySelector(".symbol-reset").addEventListener("click", () => { hint.textContent = ""; setSymbols(ctl, ctl.defaultSymbols.slice()); });
  }

  function setSymbols(ctl, symbols) {
    ctl.symbols = symbols;
    try { localStorage.setItem(ctl.symbolsKey, JSON.stringify(symbols)); } catch (e) { /* ignore */ }
    loadLiveSection(ctl);
  }

  function sectionRange(ctl) {
    if (!ctl.range) {
      let saved = null;
      try { saved = localStorage.getItem(ctl.storageKey); } catch (e) { /* ignore */ }
      ctl.range = saved && (!ctl.ranges || ctl.ranges.includes(saved)) ? saved : ctl.defaultRange;
    }
    return ctl.range;
  }

  async function loadLiveSection(ctl) {
    if (ctl.inflight) { ctl.rerun = true; return; }
    ctl.inflight = true;
    const requestedRange = sectionRange(ctl);
    ctl.card.querySelectorAll(".range-buttons button").forEach((b) => b.classList.toggle("active", b.dataset.range === requestedRange));
    try {
      const variantParam = Object.entries(ctl.variants).map(([k, v]) => `${k}:${v}`).join(",");
      const symbolParam = Array.isArray(ctl.symbols) ? `&symbols=${encodeURIComponent(ctl.symbols.join(","))}` : "";
      const data = await api(`/api/macro?section=${encodeURIComponent(ctl.id)}&range=${encodeURIComponent(requestedRange)}${variantParam ? `&variants=${encodeURIComponent(variantParam)}` : ""}${symbolParam}`);
      ctl.data = data;
      (ctl.render || renderLiveSection)(ctl, data);
      scheduleLiveRefresh(ctl, ((data.refresh_seconds || data.live_ttl_seconds || 55) * 1000) + 300);
    } catch (err) {
      if (ctl.groups.length) {
        ctl.data = null;
        renderComparisons(ctl, {items: {}, error: `Update failed: ${err.message}`});
        scheduleLiveRefresh(ctl, 30000);
        return;
      }
      // A failed poll must not leave percentage or spread charts looking current.
      const percentChart = ctl.chartMode === "percent_change";
      ctl.card.querySelectorAll(".chart-baseline").forEach((el) => { el.textContent = ""; });
      Object.keys(state.charts).filter((key) => key.startsWith(`live:${ctl.id}:`)).forEach((key) => {
        const name = key.slice(`live:${ctl.id}:`.length);
        if (!percentChart && ctl.data?.items?.[name]?.unit !== "bp" && !state.charts[key].$stockSession && !isSessionStock(ctl, name, requestedRange)) return;
        clearSeriesChart(key);
        const stats = document.getElementById(`live-${ctl.id}-stats-${domKey(name)}`);
        if (stats) stats.innerHTML = '<span class="sub">chart unavailable - update failed</span>';
        const badge = document.getElementById(`live-${ctl.id}-badge-${domKey(name)}`);
        if (badge) { badge.className = "live-badge off"; badge.textContent = "UNAVAILABLE"; }
      });
      ctl.card.querySelector(".live-status").textContent = `update failed: ${err.message}`;
      scheduleLiveRefresh(ctl, 30000);
    } finally {
      ctl.inflight = false;
      // A range/variant/ticker change made while a poll was in flight is applied right away instead of next poll.
      if (ctl.rerun) { ctl.rerun = false; loadLiveSection(ctl); }
    }
  }

  function scheduleLiveRefresh(ctl, ms) {
    clearTimeout(ctl.timer);
    ctl.timer = setTimeout(() => {
      const folded = ctl.card.classList.contains("collapsed");
      if (document.visibilityState === "visible" && !folded) loadLiveSection(ctl); else scheduleLiveRefresh(ctl, 15000);
    }, ms);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") liveSections.forEach((ctl) => { if (ctl.data) loadLiveSection(ctl); });
  });

  const LIVE_PALETTE = ["#38bdf8", "#22d3ee", "#2dd4bf", "#4ade80", "#fb7185", "#fbbf24", "#f0abfc", "#a3e635"];

  function renderLiveSection(ctl, data) {
    const items = data.items || {};
    const sid = ctl.id;
    const container = ctl.card.querySelector(".macro-grid");
    const names = Object.keys(items);
    const editable = Array.isArray(ctl.symbols);
    const percentChart = ctl.chartMode === "percent_change";
    if (container.dataset.built !== names.join(",")) {
      container.innerHTML = names.map((name) => `
        <div class="macro-card" data-key="${esc(name)}">
          <div class="mini-head">
            <span><b>${esc(name)}</b> <span class="live-badge off" id="live-${sid}-badge-${domKey(name)}">LIVE</span> <span class="variant-buttons" id="live-${sid}-variants-${domKey(name)}"></span></span>
            <span class="desc" id="live-${sid}-label-${domKey(name)}"></span>
            ${editable ? `<button type="button" class="card-remove" data-key="${esc(name)}" title="Remove ${esc(name)} from this section">×</button>` : ""}
          </div>
          <div class="macro-stats" id="live-${sid}-stats-${domKey(name)}"></div>
          <div class="macro-chart"><canvas id="live-${sid}-chart-${domKey(name)}"></canvas></div>
          ${percentChart ? `<div class="muted small chart-baseline" id="live-${sid}-baseline-${domKey(name)}"></div>` : ""}
        </div>`).join("");
      if (!names.length) {
        container.innerHTML = `<div class="empty">${editable ? "No tickers charted - add one above or press Reset for the defaults." : "No series configured for this section."}</div>`;
      }
      container.dataset.built = names.join(",");
      Object.keys(state.charts).filter((k) => k.startsWith(`live:${sid}:`)).forEach((k) => { state.charts[k].destroy(); delete state.charts[k]; });
      container.querySelectorAll(".card-remove").forEach((btn) => btn.addEventListener("click", () => setSymbols(ctl, ctl.symbols.filter((s) => s !== btn.dataset.key))));
    }
    names.forEach((name, i) => {
      const entry = items[name];
      const m = entry.metrics || {};
      const unit = entry.unit || "index";
      const id = domKey(name);
      const chartKey = `live:${sid}:${name}`;
      const scopedStock = isSessionStock(ctl, name, data.range) || (editable && data.range === "1d" && "session" in entry);
      const clearStale = percentChart || unit === "bp" || scopedStock || !!state.charts[chartKey]?.$stockSession;
      const baseline = document.getElementById(`live-${sid}-baseline-${id}`);
      if (baseline) baseline.textContent = "";
      $(`#live-${sid}-label-${id}`).textContent = entry.label || "";
      const variantHost = $(`#live-${sid}-variants-${id}`);
      const variants = entry.variants || [];
      if (variants.length > 1) {
        variantHost.innerHTML = variants.map((v) => `<button type="button" class="${v.name === entry.variant ? "active" : ""}" data-variant="${esc(v.name)}" title="${esc(v.label)}">${esc(v.name)}</button>`).join("");
        variantHost.querySelectorAll("button").forEach((btn) => btn.addEventListener("click", () => {
          ctl.variants[name] = btn.dataset.variant;
          try { localStorage.setItem(ctl.variantsKey, JSON.stringify(ctl.variants)); } catch (e) { /* ignore */ }
          loadLiveSection(ctl);
        }));
      } else {
        variantHost.innerHTML = "";
      }
      const badge = $(`#live-${sid}-badge-${id}`);
      const historyOnly = scopedStock && entry.intraday && entry.session?.date && !m.live;
      badge.className = `live-badge ${m.live ? "" : "off"}`;
      badge.innerHTML = m.live ? `<span class="dot"></span>LIVE` : historyOnly ? "HISTORY" : "DAILY";
      badge.title = m.live ? `${entry.live_source} - as of ${m.as_of}`
        : historyOnly ? "Intraday session history; live quote unavailable" : (entry.live_note || "");
      if (entry.error) {
        if (clearStale) clearSeriesChart(chartKey);
        $(`#live-${sid}-stats-${id}`).innerHTML = `<span class="sub">unavailable - no daily history or live quote from ${esc((entry.live_source || entry.daily_source || "any source"))}${editable ? "; check the ticker" : ""}</span>`;
        return;
      }
      const chg = m.change;
      const rangeNote = entry.fallback ? `<span class="sub">no intraday feed - showing 1M daily</span>` : "";
      const note = entry.note ? `<span class="sub note" title="${esc(entry.note)}">Note: ${esc(entry.note)}</span>` : "";
      const sessionNote = scopedStock && entry.intraday && entry.session?.date
        ? `<span class="sub">Session ${esc(entry.session.date)} · ET</span>${entry.chart_source && entry.chart_source !== entry.live_source ? `<span class="sub">chart: ${esc(entry.chart_source)}</span>` : ""}` : "";
      $(`#live-${sid}-stats-${id}`).innerHTML = `
        <span class="value">${fmtValue(m.value, unit)}</span>
        <span class="chg ${chg > 0 ? "gain" : chg < 0 ? "loss" : ""}">${fmtChange(chg, unit, m.value)}${unit !== "usd" && unit !== "bp" && m.change_pct != null ? ` (${signed(m.change_pct, 2)}%)` : ""}</span>
        ${m.day_low != null ? `<span class="sub">day ${fmtRange(m.day_low, m.day_high, unit)}</span>` : ""}
        <span class="sub">52w ${fmtRange(m.low_52w, m.high_52w, unit)}</span>
        <span class="sub">pctl ${num(m.percentile, 0)}</span>
        <span class="sub" title="${esc(entry.daily_source || "")}">${esc(String(entry.live_source || entry.daily_source || "").split(":")[0])}</span>
        ${sessionNote}${rangeNote}${note}`;
      if (clearStale && (typeof Chart === "undefined" || !Array.isArray(entry.series) || !entry.series.length)) {
        clearSeriesChart(chartKey);
        $(`#live-${sid}-stats-${id}`).insertAdjacentHTML("beforeend", `<span class="sub">chart unavailable - ${typeof Chart === "undefined" ? "chart library not loaded" : "no bars"}</span>`);
      } else if (typeof Chart !== "undefined" && entry.series && entry.series.length) {
        const session = scopedStock && entry.intraday ? (entry.session || null) : undefined;
        const percentConfig = percentChart ? { reference: entry.chart_reference, timezone: entry.timezone } : undefined;
        const drawn = drawSeriesChart(chartKey, `#live-${sid}-chart-${id}`, entry.series, LIVE_PALETTE[i % LIVE_PALETTE.length], entry.intraday ? data.range : "daily", unit, name, undefined, session, percentConfig);
        if (drawn === true && baseline) baseline.textContent = state.charts[chartKey].$baselineLabel;
        if (drawn === false && clearStale) {
          const reason = scopedStock && entry.session_note ? entry.session_note : "no usable bars or chart reference";
          $(`#live-${sid}-stats-${id}`).insertAdjacentHTML("beforeend", `<span class="sub">chart unavailable - ${esc(reason)}</span>`);
        }
      }
    });
    const curve = data.curve || {};
    const curveParts = Object.entries(curve).map(([k, c]) => `${k} ${signed(c.bp, 0)} bp${c.state === "inverted" ? " (inverted)" : ""}`);
    ctl.card.querySelector(".curve-summary").textContent = curveParts.length ? `yield curve: ${curveParts.join(" | ")}` : "";
    const stamp = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const liveCount = names.filter((n) => items[n].metrics && items[n].metrics.live).length;
    ctl.card.querySelector(".live-status").innerHTML = `<span class="live-badge"><span class="dot"></span>LIVE</span> ${liveCount}/${names.length} series live · refreshed ${esc(stamp)} · every ${Math.round(data.refresh_seconds || data.live_ttl_seconds || 55)}s`;
    setSummary(ctl.card.id, names.filter((n) => !items[n].error).map((n) => `${n} ${fmtValue(items[n].metrics.value, items[n].unit)}`).join(" · "));
  }

  function renderComparisons(ctl, data) {
    const container = ctl.card.querySelector(".macro-grid");
    if (!container.dataset.built) {
      container.innerHTML = ctl.groups.map((group) => `
        <div class="macro-card comparison-card" data-group="${esc(group.id)}">
          <div class="mini-head"><h3>${esc(group.title)}</h3><span class="live-badge off">DAILY</span></div>
          <div class="comparison-message muted small" role="status"></div>
          <div class="macro-chart"><canvas id="comparison-${domKey(ctl.id)}-${domKey(group.id)}"></canvas></div>
          <div class="comparison-sources small"></div>
          <p class="comparison-note muted small">${esc(group.note || "")} Gaps indicate no observation; no interpolation.</p>
        </div>`).join("");
      container.dataset.built = "true";
    }
    ctl.groups.forEach((group, groupIndex) => {
      const card = container.children[groupIndex];
      const chartKey = `comparison:${ctl.id}:${group.id}`;
      clearSeriesChart(chartKey);
      const priceChange = group.mode === "price_change";
      const entries = group.series.map((name) => data.items?.[name] || {});
      const values = entries.map((entry) => new Map((entry.error ? [] : entry.series || []).filter((point) =>
        point && typeof point.t === "string" && /^\d{4}-\d{2}-\d{2}$/.test(point.t)
      ).map((point) => [point.t, Number.isFinite(point.v) && (!priceChange || point.v > 0) ? point.v : null])));
      const missing = group.series.filter((_, i) => ![...values[i].values()].some(Number.isFinite));
      let dates = [...new Set(values.flatMap((series) => [...series.keys()]))].sort();
      const baseline = priceChange ? dates.find((day) => values.every((series) => Number.isFinite(series.get(day)))) : null;
      if (priceChange && baseline) dates = dates.filter((day) => day >= baseline);
      card.querySelector(".comparison-sources").innerHTML = entries.map((entry, i) => {
        const metrics = entry.metrics || {};
        const isMissing = missing.includes(group.series[i]);
        const lastPrice = priceChange ? [...values[i]].filter(([, value]) => Number.isFinite(value)).at(-1) : null;
        const observation = lastPrice?.[0] || metrics.as_of || entry.daily_last_date || metrics.daily_last_date;
        return `<div><b>${esc(entry.label || group.series[i])}</b>: ${isMissing ? "unavailable" : fmtValue(priceChange ? lastPrice?.[1] : metrics.value, entry.unit)}${!priceChange && !isMissing && metrics.change != null ? ` · ${fmtChange(metrics.change, "pct")} vs previous observation` : ""}<br><span class="muted">${esc(entry.daily_source || "source unavailable")} · as of ${esc(observation || "unavailable")}</span></div>`;
      }).join("");
      let error = data.error || (typeof Chart === "undefined" ? "Chart library unavailable" : "");
      if (!error && priceChange && (missing.length || !baseline)) error = missing.length ? `Missing series: ${missing.join(", ")}` : "No common date with valid positive prices";
      if (!error && missing.length === group.series.length) error = "All series unavailable";
      card.querySelector(".comparison-message").textContent = error ? `Comparison unavailable · ${error}` : priceChange
        ? `Price change (%) · shared baseline ${baseline}`
        : `Yield / policy rate (%)${missing.length ? ` · Missing series: ${missing.join(", ")}` : ""}`;
      if (error) return;
      const bounds = group.target_bounds || [];
      const colors = ["#38bdf8", "#f5a524", "#a78bfa", "#5eead4", "#5eead4"];
      const datasets = group.series.map((name, i) => ({
        label: entries[i].label || name,
        data: dates.map((day) => {
          const value = values[i].get(day);
          return Number.isFinite(value) ? priceChange ? (value / values[i].get(baseline) - 1) * 100 : value : null;
        }),
        borderColor: colors[i % colors.length], borderWidth: 1.7,
        pointBackgroundColor: colors[i % colors.length],
        pointRadius: (context) => {
          const points = context.dataset.data, index = context.dataIndex;
          return Number.isFinite(points[index]) && !Number.isFinite(points[index - 1]) && !Number.isFinite(points[index + 1]) ? 3 : 0;
        },
        pointHitRadius: 10,
        tension: 0, spanGaps: false, fill: false,
        stepped: bounds.includes(name) ? "before" : false,
        borderDash: name === bounds[0] ? [5, 4] : name === bounds[1] ? [2, 3] : [],
      }));
      const band = {
        id: "targetBand",
        beforeDatasetsDraw(chart) {
          const lower = datasets[group.series.indexOf(bounds[0])]?.data;
          const upper = datasets[group.series.indexOf(bounds[1])]?.data;
          if (!lower || !upper || !bounds.every((name) => chart.isDatasetVisible(group.series.indexOf(name)))) return;
          const {ctx, scales: {x, y}, chartArea: area} = chart;
          ctx.save();
          ctx.beginPath(); ctx.rect(area.left, area.top, area.width, area.height); ctx.clip();
          ctx.fillStyle = "rgba(94,234,212,0.10)";
          for (let i = 0; i < dates.length - 1; i++) {
            if (![lower[i], upper[i], lower[i + 1], upper[i + 1]].every(Number.isFinite) || lower[i] > upper[i]) continue;
            const left = x.getPixelForValue(i), top = y.getPixelForValue(upper[i]);
            ctx.fillRect(left, top, x.getPixelForValue(i + 1) - left, y.getPixelForValue(lower[i]) - top);
          }
          ctx.restore();
        },
      };
      state.charts[chartKey] = new Chart(card.querySelector("canvas"), {
        type: "line", plugins: [band, crosshairPlugin], data: {labels: dates, datasets},
        options: {
          animation: false, responsive: true, maintainAspectRatio: false,
          interaction: {mode: "index", intersect: false},
          plugins: {
            legend: {display: true, position: "bottom", labels: {color: "#e6edf3", boxWidth: 20, boxHeight: 2, font: {size: 11}}},
            crosshair: {format: (value) => `${num(value, 2)}%`},
            tooltip: {callbacks: {
              title: (items) => items[0]?.label || "",
              label: (item) => `${item.dataset.label}: ${priceChange ? signed(item.parsed.y, 2) : num(item.parsed.y, 3)}%${priceChange ? ` (${fmtPrecise(values[item.datasetIndex].get(item.label), entries[item.datasetIndex].unit)})` : ""}`,
              afterBody: (items) => {
                const day = items[0]?.label;
                return group.series.filter((_, i) => !Number.isFinite(values[i].get(day))).map((name) => `${name}: no observation`);
              },
            }},
          },
          scales: {
            x: {ticks: {color: "#8b98a8", maxTicksLimit: (context) => context.chart.width < 400 ? 2 : 5, maxRotation: 0, font: {size: 10}}, grid: {color: "#263241"}},
            y: {title: {display: true, text: priceChange ? "Price change (%)" : "Rate (%)", color: "#8b98a8"}, ticks: {color: "#8b98a8", maxTicksLimit: 6, callback: (value) => `${num(value, 2)}%`}, grid: {color: "#263241"}},
          },
        },
      });
    });
    ctl.card.querySelector(".live-status").textContent = data.error || `DAILY · ${data.window_start || ""} – ${data.window_end || ""} · native chart ranges`;
    ctl.card.querySelector(".curve-summary").textContent = "";
    setSummary(ctl.card.id, data.error ? "Comparison update unavailable" : "SMH / KOSPI · Treasury / Fed · daily observations");
  }

  // VIX card: live Longbridge quote + intraday/daily series from the hidden "vix" live section.
  function renderVixLive(ctl, data) {
    const entry = (data.items || {}).VIX;
    if (!entry) return;
    const m = entry.metrics || {};
    state.vixLive = m;
    const stamp = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    $("#vix-live").innerHTML = m.live
      ? `<span class="live-badge"><span class="dot"></span>LIVE</span> ${esc(String(entry.live_source || "").split(" (")[0])} · as of ${esc(String(m.as_of || "").slice(11, 16))} ET · refreshed ${esc(stamp)} · every ${Math.round(data.refresh_seconds || 5)}s`
      : `<span class="live-badge off">DAILY</span> ${esc(entry.live_note || "no live feed")}`;
    if (state.market) renderIndexStats();
    if (typeof Chart === "undefined" || !entry.series || !entry.series.length) return;
    const t = (state.market && state.market.thresholds && state.market.thresholds.vix) || {};
    const thresholds = [[t.low, "low"], [t.medium, "medium"], [t.high, "high"]].filter(([v]) => v != null);
    drawSeriesChart("vix", "#vix-chart", entry.series, "#f5a524", entry.intraday ? data.range : "daily", "vol", "VIX", thresholds);
  }

  // Crosshair for the live macro charts: vertical line snapped to the hovered point, horizontal line at the
  // cursor with its value on the axis, and a marker on the series. Options: plugins.crosshair.format(value).
  const crosshairPlugin = {
    id: "crosshair",
    afterEvent(chart, args) {
      const e = args.event;
      const a = chart.chartArea;
      const inside = e.type !== "mouseout" && e.x >= a.left && e.x <= a.right && e.y >= a.top && e.y <= a.bottom;
      const next = inside ? { x: e.x, y: e.y } : null;
      if (JSON.stringify(next) !== JSON.stringify(chart.$crosshair || null)) {
        chart.$crosshair = next;
        args.changed = true;
      }
    },
    afterDraw(chart, _args, options) {
      const c = chart.$crosshair;
      if (!c) return;
      const { ctx, chartArea: a } = chart;
      const active = chart.getActiveElements();
      const x = active.length ? active[0].element.x : c.x;
      const yScale = chart.scales.y;
      const value = yScale.getValueForPixel(c.y);
      const label = options && options.format ? options.format(value) : Number(value).toFixed(2);

      ctx.save();
      ctx.lineWidth = 1;
      ctx.strokeStyle = "rgba(230, 237, 243, 0.45)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(x, a.top); ctx.lineTo(x, a.bottom); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(a.left, c.y); ctx.lineTo(a.right, c.y); ctx.stroke();
      ctx.setLineDash([]);

      if (active.length) {
        const el = active[0].element;
        ctx.fillStyle = chart.data.datasets[active[0].datasetIndex].borderColor;
        ctx.beginPath(); ctx.arc(el.x, el.y, 3.5, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = "#0f1419"; ctx.stroke();
      }

      ctx.font = "11px -apple-system, Segoe UI, Roboto, sans-serif";
      const pad = 4, w = ctx.measureText(label).width + pad * 2, h = 16;
      const boxX = Math.max(0, a.left - w - 2), boxY = Math.min(Math.max(c.y - h / 2, a.top), a.bottom - h);
      ctx.fillStyle = "#263241";
      ctx.fillRect(boxX, boxY, w, h);
      ctx.fillStyle = "#e6edf3";
      ctx.textBaseline = "middle";
      ctx.fillText(label, boxX + pad, boxY + h / 2);
      ctx.restore();
    },
  };

  // Undefined session preserves category charts; null means the US session is unavailable.
  function drawSeriesChart(key, selector, series, color, range, unit, name, thresholds, session, percentConfig) {
    const fixedSession = range === "1d" && session !== undefined;
    const percent = !!percentConfig;
    const timezone = percentConfig?.timezone || session?.timezone || percentConfig?.reference?.timezone;
    const rawValue = (v) => `${fmtPrecise(v, unit)}${unit === "index" ? " pts" : ""}`;
    // Never let Date.parse interpret a naive intraday timestamp in the browser's timezone.
    const timestamp = (t) => typeof t === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/.test(t) ? Date.parse(t) : NaN;
    const dateInZone = (formatter, x) => {
      const parts = Object.fromEntries(formatter.formatToParts(x).map((p) => [p.type, p.value]));
      return `${parts.year}-${parts.month}-${parts.day}`;
    };
    let points = series, start, end, sessionTime, sessionStamp, zoneDate, zoneTime, zoneStamp, referenceValue;
    let baselineLabel = "";
    try {
      if (fixedSession || percent || unit === "bp") {
        points = (Array.isArray(series) ? series : []).filter((p) => p && Number.isFinite(p.v)).map((p) => {
          const dateOnly = !fixedSession && typeof p.t === "string" && /^\d{4}-\d{2}-\d{2}$/.test(p.t);
          return { ...p, dateOnly, x: dateOnly ? Date.parse(p.t) : timestamp(p.t) };
        }).filter((p) => Number.isFinite(p.x) && (!p.dateOnly || new Date(p.x).toISOString().slice(0, 10) === p.t)).sort((a, b) => a.x - b.x);
      }
      if (fixedSession) {
        if (!session || session.timezone !== "America/New_York") throw new Error("unknown session timezone");
        start = timestamp(session.start);
        end = timestamp(session.end);
        sessionTime = new Intl.DateTimeFormat("en-GB", { timeZone: session.timezone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
        sessionStamp = new Intl.DateTimeFormat("en-US", { timeZone: session.timezone, year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short" });
        if (!Number.isFinite(start) || !Number.isFinite(end) || end - start !== 390 * 60000 || sessionTime.format(start) !== "09:30" || sessionTime.format(end) !== "16:00") throw new Error("invalid session bounds");
        // Keep the supplied historical session, including weekend views, and only its real bars.
        points = points.filter((p) => p.x >= start && p.x <= end);
      }
      if ((fixedSession || percent || unit === "bp") && !points.length) throw new Error("no usable bars");
      if (percent) {
        if (timezone) {
          zoneDate = new Intl.DateTimeFormat("en-US", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit" });
          zoneTime = new Intl.DateTimeFormat("en-GB", { timeZone: timezone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
          zoneStamp = new Intl.DateTimeFormat("en-US", { timeZone: timezone, year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short" });
        }
        const reference = percentConfig.reference || {};
        let kind = reference.kind;
        let fallbackReason = kind === "first_point_fallback" ? "previous close unavailable" : "";
        if (kind === "previous_close") {
          if (!Number.isFinite(reference.value) || reference.value <= 0) throw new Error("invalid previous close");
          let matchesSession = false;
          try {
            if (range === "1d" && typeof reference.timezone === "string" && /^\d{4}-\d{2}-\d{2}$/.test(reference.session_date)) {
              const referenceDate = new Intl.DateTimeFormat("en-US", { timeZone: reference.timezone, year: "numeric", month: "2-digit", day: "2-digit" });
              matchesSession = points.every((p) => !p.dateOnly && dateInZone(referenceDate, p.x) === reference.session_date);
            }
          } catch (err) { /* An uncheckable session cannot justify a previous-close baseline. */ }
          if (!matchesSession) { kind = "first_point_fallback"; fallbackReason = "reference/session mismatch"; }
        } else if (kind !== "first_point" && kind !== "first_point_fallback") {
          throw new Error("unknown chart reference");
        }
        // Rebase only after sorting and filtering the points that will actually be drawn.
        referenceValue = kind === "previous_close" ? reference.value : points[0].v;
        if (!Number.isFinite(referenceValue) || referenceValue <= 0) throw new Error("invalid chart reference");
        points = points.map((p) => ({ ...p, y: ((p.v - referenceValue) / referenceValue) * 100 }));
        if (points.some((p) => !Number.isFinite(p.y))) throw new Error("nonfinite percentage");
        baselineLabel = kind === "previous_close"
          ? `% vs previous close · ${rawValue(referenceValue)} · ${reference.session_date}`
          : `% vs first plotted point${fallbackReason ? ` (fallback: ${fallbackReason})` : ""} · ${rawValue(referenceValue)}`;
      }
    } catch (err) {
      clearSeriesChart(key);
      return false;
    }
    const labels = fixedSession ? [] : points.map((p) => {
      // Daily dates are exchange calendar labels, not UTC instants to shift into yesterday.
      if (!percent || !zoneTime || p.dateOnly) return timeLabel(p.t, range);
      const date = dateInZone(zoneDate, p.x);
      return range === "daily" ? date : range === "1d" ? zoneTime.format(p.x) : `${date.slice(5)} ${zoneTime.format(p.x)}`;
    });
    const values = fixedSession ? points.map((p) => ({ x: p.x, y: percent ? p.y : p.v })) : points.map((p) => percent ? p.y : p.v);
    const guideLevels = percent ? (thresholds || []).filter(([v]) => Number.isFinite(v)).map(([v, label]) => [((v - referenceValue) / referenceValue) * 100, label]).filter(([v]) => Number.isFinite(v)) : (thresholds || []);
    const guides = guideLevels.map(([value, label]) => ({ label, data: fixedSession ? points.map((p) => ({ x: p.x, y: value })) : labels.map(() => value), borderColor: "#5b6b7e", borderDash: [4, 4], borderWidth: 1, pointRadius: 0, pointHitRadius: 0, fill: false, order: 2 }));
    const xScale = { type: "category", ticks: { color: "#8b98a8", maxTicksLimit: 6, maxRotation: 0, font: { size: 10 } }, grid: { color: "#1f2a37" } };
    if (unit === "bp") xScale.ticks.autoSkipPadding = 12;
    if (percent && timezone) xScale.title = { display: true, text: timezone, color: "#8b98a8", font: { size: 10 } };
    if (fixedSession) {
      Object.assign(xScale, {
        type: "linear", min: start, max: end, bounds: "ticks", offset: false,
        title: { display: true, text: "ET · America/New_York", color: "#8b98a8", font: { size: 10 } },
        afterBuildTicks: (scale) => {
          // Keep both endpoints even on a 320px phone; add interior ticks only when they fit.
          const minutes = scale.width >= 420 ? [90, 210, 330] : scale.width >= 260 ? [180] : [];
          scale.ticks = [start, ...minutes.map((m) => start + m * 60000), end].map((value) => ({ value }));
        },
      });
      Object.assign(xScale.ticks, { autoSkip: false, includeBounds: true, align: "inner", callback: (v) => sessionTime.format(v) });
    }
    const data = { labels, datasets: [{ label: name || key, data: values, borderColor: color, backgroundColor: color + "22", borderWidth: 1.5, pointRadius: (fixedSession || percent || unit === "bp") && points.length === 1 ? 3 : 0, pointHitRadius: 12, tension: 0.1, fill: true, order: 1 }, ...guides] };
    const options = {
      animation: false, responsive: true, maintainAspectRatio: false, parsing: !fixedSession,
      interaction: fixedSession ? { mode: "nearest", axis: "x", intersect: false } : { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        crosshair: { format: (v) => percent ? `${signed(v, 3)}%` : fmtPrecise(v, unit) },
        tooltip: {
          filter: (item) => item.datasetIndex === 0,
          callbacks: {
            title: (items) => {
              const item = items[0];
              if (!item) return "";
              if (fixedSession) return sessionStamp.format(item.parsed.x);
              const point = item.chart.$series && item.chart.$series[item.dataIndex];
              if (percent && zoneStamp && point && !point.dateOnly) return zoneStamp.format(point.x);
              return point ? String(point.t).replace("T", " ").replace(/[-+]\d\d:\d\d$/, "") : "";
            },
            label: (ctx) => {
              if (!percent) return `${ctx.dataset.label}: ${fmtPrecise(ctx.parsed.y, unit)}`;
              const point = ctx.chart.$series && ctx.chart.$series[ctx.dataIndex];
              return `${ctx.dataset.label}: ${signed(ctx.parsed.y, 2)}% (${rawValue(point?.v)})`;
            },
          },
        },
      },
      scales: {
        x: xScale,
        y: { ticks: { color: "#8b98a8", maxTicksLimit: 5, font: { size: 10 }, ...(percent ? { callback: (v) => `${signed(v, 2)}%` } : unit === "bp" ? { callback: (v) => fmtPrecise(v, unit) } : {}) }, grid: { color: "#1f2a37" } },
      },
    };
    const existing = state.charts[key];
    if (existing) {
      // Replace representation AND options: no old bounds, parsing, callbacks or dot survive a switch.
      existing.data = data;
      existing.options = options;
      existing.$series = points;
      existing.$baselineLabel = baselineLabel;
      if (percent || unit === "bp" || fixedSession || existing.$stockSession) {
        existing.$crosshair = null;
        existing.setActiveElements([]);
        if (existing.tooltip) existing.tooltip.setActiveElements([], { x: 0, y: 0 });
      }
      existing.$stockSession = fixedSession;
      existing.update("none");
      return true;
    }
    const canvas = $(selector);
    if (!canvas) { clearSeriesChart(key); return false; }
    const chart = new Chart(canvas, { type: "line", plugins: [crosshairPlugin], data, options });
    chart.$series = points;
    chart.$baselineLabel = baselineLabel;
    chart.$stockSession = fixedSession;
    state.charts[key] = chart;
    return true;
  }

  function renderIndexStats() {
    const m = state.market;
    const vix = m.vix.metrics;
    const skew = m.skew.metrics;
    $("#vix-stats").innerHTML = [
      stat("VIX", num(state.vixLive && state.vixLive.live ? state.vixLive.value : vix.value),
           state.vixLive && state.vixLive.live ? `live · prev close ${num(state.vixLive.prev_close)} (${signed(state.vixLive.change)})` : vix.date, "big"),
      stat("Level", badge(vix.level)),
      stat("IV Rank", num(vix.iv_rank, 0), `52w ${num(vix.low_52w, 1)} - ${num(vix.high_52w, 1)}`),
      stat("IV Rank level", badge(m.iv_rank_level), `5d ${signed(m.iv_rank_change_5d, 1)} pts`),
      stat("Percentile", num(vix.percentile, 0)),
      stat("5d change", signed(vix.change_5d), `1d ${signed(vix.change_1d)}`),
      stat("SMA 5 / 20", `${num(vix.sma5, 1)} / ${num(vix.sma20, 1)}`),
    ].join("");
    $("#skew-stats").innerHTML = [
      stat("SKEW", num(skew.value), skew.date, "big"),
      stat("Level", badge(skew.level)),
      stat("Percentile", num(skew.percentile, 0), `52w ${num(skew.low_52w, 0)} - ${num(skew.high_52w, 0)}`),
      stat("5d change", signed(skew.change_5d), `1d ${signed(skew.change_1d)}`),
      stat("SMA 5 / 20", `${num(skew.sma5, 1)} / ${num(skew.sma20, 1)}`),
      stat("Range rank", num(skew.iv_rank, 0), "position in 52w range"),
    ].join("");
  }

  function renderFearGreed(fg, source) {
    if (!fg || fg.score == null) {
      const reason = source && source.last_error ? `: ${source.last_error}` : "";
      $("#fg-stats").innerHTML = `<div class="muted">CNN Fear &amp; Greed unavailable${esc(reason)}</div>`;
      $("#fg-components").innerHTML = "";
      $("#fg-source").textContent = "";
      return;
    }
    const day = fg.timestamp ? String(fg.timestamp).slice(0, 10) : "";
    $("#fg-stats").innerHTML = [
      stat("Score", num(fg.score, 0), day, "big"),
      stat("Rating", badge(fg.rating)),
      stat("Prev close", num(fg.previous_close, 0)),
      stat("1 week ago", num(fg.previous_1_week, 0), fg.previous_1_week != null ? `${signed(fg.score - fg.previous_1_week, 0)} since` : ""),
      stat("1 month ago", num(fg.previous_1_month, 0), fg.previous_1_month != null ? `${signed(fg.score - fg.previous_1_month, 0)} since` : ""),
      stat("1 year ago", num(fg.previous_1_year, 0)),
    ].join("");
    $("#fg-components").innerHTML = (fg.components || []).map((c) => `
      <span class="label">${esc(c.label)}</span><span class="score">${num(c.score, 1)}</span><span>${badge(c.rating)}</span>`).join("");
    $("#fg-source").textContent = source && source.source ? `source: ${source.source}` : "";
  }

  function stat(label, value, sub, cls) {
    return `<div class="stat"><span class="label">${esc(label)}</span><span class="value ${cls || ""}">${value}</span>${sub ? `<span class="sub">${esc(sub)}</span>` : ""}</div>`;
  }

  function renderCharts() {
    if (typeof Chart === "undefined") { setTimeout(renderCharts, 300); return; }
    const t = state.market.thresholds || {};
    const vixCtl = liveSections.get("vix");
    if (vixCtl && vixCtl.data) renderVixLive(vixCtl, vixCtl.data);
    else drawChart("vix", "#vix-chart", state.market.vix.series, "#f5a524", [[t.vix?.low, "low"], [t.vix?.medium, "medium"], [t.vix?.high, "high"]]);
    drawChart("skew", "#skew-chart", state.market.skew.series, "#7c9cff", [
      [t.skew?.low, "low"], [t.skew?.normal, "normal"], [t.skew?.high, "high"],
    ]);
    const fg = state.market.fear_greed;
    if (fg && fg.history && fg.history.length) {
      drawChart("fg", "#fg-chart", fg.history, "#2dd4bf", [[25, "extreme fear"], [45, "fear"], [55, "neutral"], [75, "greed"]], { min: 0, max: 100 });
    }
    const palette = ["#f97316", "#a78bfa", "#f472b6", "#34d399", "#60a5fa", "#facc15"];
    Object.entries(state.market.indices || {}).forEach(([name, data], i) => {
      if (!data.error && data.series?.length) drawChart(`mini-charts:${name}`, `#mini-charts-chart-${name}`, data.series, palette[i % palette.length], [], null, true);
      else clearSeriesChart(`mini-charts:${name}`);
    });
  }

  function drawChart(key, selector, series, color, thresholds, yBounds, compact) {
    const points = series.slice(-state.range);
    const labels = points.map((p) => p.d);
    const datasets = [{
      label: key.replace(/^[a-z-]+:/, "").toUpperCase(), data: points.map((p) => p.v), borderColor: color, backgroundColor: color + "22",
      borderWidth: compact ? 1.3 : 1.6, pointRadius: 0, tension: 0.15, fill: true, order: 1,
    }];
    thresholds.forEach(([value, label]) => {
      if (value == null) return;
      datasets.push({ label, data: labels.map(() => value), borderColor: "#5b6b7e", borderDash: [4, 4], borderWidth: 1, pointRadius: 0, fill: false, order: 2 });
    });
    if (state.charts[key]) {
      state.charts[key].data.labels = labels;
      state.charts[key].data.datasets = datasets;
      state.charts[key].update();
      return;
    }
    const yScale = { ticks: { color: "#8b98a8", maxTicksLimit: compact ? 4 : undefined, font: compact ? { size: 10 } : undefined }, grid: { color: "#1f2a37" } };
    if (yBounds) Object.assign(yScale, yBounds);
    state.charts[key] = new Chart($(selector), {
      type: "line",
      data: { labels, datasets },
      options: {
        animation: false, responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${Number(ctx.parsed.y).toFixed(2)}` } },
        },
        scales: {
          x: { ticks: { color: "#8b98a8", maxTicksLimit: compact ? 3 : 8, maxRotation: 0, font: compact ? { size: 10 } : undefined }, grid: { color: "#1f2a37" } },
          y: yScale,
        },
      },
    });
  }

  // ------------------------------------------------------------------ regime
  function renderRegime(regime) {
    if (!regime) return;
    $("#regime-confidence").textContent = regime.exact_match
      ? "all three conditions match"
      : `nearest match - confidence ${Math.round((regime.confidence || 0) * 100)}%`;
    const conditions = (regime.conditions || []).map((c) => `
      <tr>
        <td class="left">${esc(c.name)}</td>
        <td>${c.value == null ? "-" : num(c.value, c.name === "IV Rank" ? 0 : 2)}</td>
        <td>${badge(c.level)}</td>
        <td>${badge(c.expected)}</td>
        <td class="${c.matched ? "cond-ok" : "cond-bad"}">${c.matched ? "match" : "differs"}</td>
        <td class="left muted small wrap">${esc(c.detail || "")}</td>
      </tr>`).join("");
    const notes = (regime.notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
    const alts = (regime.alternatives || []).map((a) => `${esc(a.title)} (${Math.round(a.confidence * 100)}%)`).join(", ");
    const panic = regime.panic && regime.panic.peak != null
      ? `Panic check: 15-day VIX peak ${num(regime.panic.peak)} on ${esc(regime.panic.peak_date)}, ${num(regime.panic.drop_from_peak_pct, 0)}% below peak now${regime.panic.detected ? " - reversal detected" : ""}.`
      : "";
    const fg = state.market && state.market.fear_greed;
    const sentiment = fg && fg.score != null ? `CNN Fear &amp; Greed ${num(fg.score, 0)} (${esc(fg.rating)}) - sentiment context only, not part of the regime rules.` : "";
    $("#regime-body").innerHTML = `
      <div>
        <div class="regime-title"><h3>${esc(regime.title)}</h3>${badge(regime.stance, regime.stance.replace("_", " "))}</div>
        <div class="regime-desc">${esc(regime.description)}</div>
        <div class="regime-action">${esc(regime.action)}</div>
        ${notes ? `<ul class="notes">${notes}</ul>` : ""}
        ${alts ? `<div class="alts muted small">Next closest: ${alts}</div>` : ""}
        ${panic ? `<div class="muted small" style="margin-top:6px">${panic}</div>` : ""}
        ${sentiment ? `<div class="muted small" style="margin-top:4px">${sentiment}</div>` : ""}
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th class="left">Input</th><th>Value</th><th>Level</th><th>Scenario needs</th><th></th><th class="left">Detail</th></tr></thead>
          <tbody>${conditions}</tbody>
        </table>
      </div>`;
  }

  // ------------------------------------------------------------------ screener filters
  function activeScenarioKey() {
    return $("#scenario-select").value || (state.market && state.market.regime && state.market.regime.scenario) || null;
  }

  function scenarioMeta(key) {
    return ((state.config && state.config.scenarios) || []).find((s) => s.key === key) || null;
  }

  function defaultFilters(key) {
    const f = (scenarioMeta(key) || {}).filters || {};
    return {
      dte_min: f.dte ? f.dte[0] : 30,
      dte_max: f.dte ? f.dte[1] : 45,
      delta_min: f.delta ? f.delta[0] : 0.1,
      delta_max: f.delta ? f.delta[1] : 0.3,
      min_open_interest: f.min_open_interest ?? 0,
      max_spread_pct: f.max_spread_pct ?? 100,
      min_annualized_yield_pct: f.min_annualized_yield_pct ?? null,
      target_delta: f.target_delta ?? null,
      target_strike: f.target_strike ?? null,
      strike_mode: strikeMode(f),
      max_results: f.max_results ?? 12,
    };
  }

  function storedFilters() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); } catch (e) { return {}; }
  }

  function saveStoredFilters(all) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(all)); } catch (e) { /* storage unavailable */ }
  }

  function strikeMode(filters) {
    return filters.target_strike != null && ["exact", "lt", "gt"].includes(filters.strike_mode) ? filters.strike_mode : "exact";
  }

  function updateStrikeInput() {
    const input = $("#scan-form [name=target_strike]");
    const mode = $("#scan-form [name=strike_mode]");
    const blank = input.value.trim() === "" && !input.validity.badInput;
    const value = input.valueAsNumber;
    input.setCustomValidity(blank || (Number.isFinite(value) && value > 0) ? "" : "Enter a finite target strike greater than 0.");
    mode.disabled = blank;
    if (blank) {
      mode.value = "exact";
      mode.classList.remove("changed");
    }
    return input.validity.valid;
  }

  function readFilterForm() {
    const out = {};
    FILTER_FIELDS.forEach((name) => {
      const raw = $(`#scan-form [name="${name}"]`).value.trim();
      out[name] = name === "strike_mode" ? raw : raw === "" ? null : Number(raw);
    });
    out.strike_mode = strikeMode(out);
    return out;
  }

  function writeFilterForm(values, defaults) {
    FILTER_FIELDS.forEach((name) => {
      const input = $(`#scan-form [name="${name}"]`);
      const value = name === "strike_mode" ? strikeMode(values) : values[name];
      const baseline = name === "strike_mode" ? strikeMode(defaults) : defaults[name];
      input.value = value == null ? "" : value;
      input.classList.toggle("changed", !sameValue(value, baseline, name));
    });
    updateStrikeInput();
  }

  function sameValue(a, b, name) {
    if (a == null && b == null) return true;
    if (a == null || b == null) return false;
    if (name === "strike_mode" || name === "target_strike") return a === b;
    return Math.abs(Number(a) - Number(b)) < 1e-9;
  }

  function filtersEqual(a, b) {
    return FILTER_FIELDS.every((name) => name === "strike_mode"
      ? sameValue(strikeMode(a), strikeMode(b), name)
      : sameValue(a[name], b[name], name));
  }

  function loadFilters() {
    const key = activeScenarioKey();
    const meta = scenarioMeta(key);
    const defaults = defaultFilters(key);
    const stored = storedFilters()[key];
    const values = stored ? Object.assign({}, defaults, stored) : defaults;
    writeFilterForm(values, defaults);
    const auto = !$("#scenario-select").value;
    $("#filters-scenario").textContent = meta ? `- ${auto ? "auto: " : ""}${meta.title}${meta.option_side === "call" ? " · delta guide: OTM .15-.35, ATM .35-.65, ITM .65-.85" : ""}` : "";
    updateFilterState(values, defaults);
  }

  function updateFilterState(values, defaults) {
    const custom = !filtersEqual(values, defaults);
    $("#filters-state").textContent = custom ? "Custom values (highlighted) will be used for the next scan." : "Using scenario defaults from config.yaml.";
  }

  function onFilterInput() {
    if (!updateStrikeInput()) return;
    const key = activeScenarioKey();
    if (!key) return;
    const values = readFilterForm();
    const defaults = defaultFilters(key);
    const all = storedFilters();
    if (filtersEqual(values, defaults)) delete all[key]; else all[key] = values;
    saveStoredFilters(all);
    writeFilterForm(values, defaults);
    updateFilterState(values, defaults);
  }

  document.querySelectorAll("#scan-form .filters input, #scan-form .filters select").forEach((input) => input.addEventListener("change", onFilterInput));
  $("#scan-form [name=target_strike]").addEventListener("input", updateStrikeInput);
  $("#filters-reset").addEventListener("click", () => {
    const key = activeScenarioKey();
    const all = storedFilters();
    delete all[key];
    saveStoredFilters(all);
    loadFilters();
  });
  $("#scenario-select").addEventListener("change", loadFilters);

  // ------------------------------------------------------------------ status polling
  async function pollStatus() {
    try {
      const status = await api("/api/status");
      state.status = status;
      renderStatus(status);
      const job = status.job;
      if (job && job.state === "done" && job.id !== state.lastJobId) {
        state.lastJobId = job.id;
        if (job.kind === "iv_refresh") await loadWatchlist(); else await loadRecommendations();
      }
      const active = job && (job.state === "running" || job.state === "queued");
      schedulePoll(active ? 1000 : 4000);
    } catch (err) {
      setPill("pill-error", "Backend unreachable");
      schedulePoll(5000);
    }
  }

  function schedulePoll(ms) {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(pollStatus, ms);
  }

  function setPill(cls, text, spinning) {
    const pill = $("#status-pill");
    pill.className = `pill ${cls}`;
    pill.innerHTML = `${spinning ? '<span class="spinner"></span>' : ""}${esc(text)}<span class="clock" id="pill-clock">${clockText()}</span>`;
    if (state.status) pill.title = `server time ${state.status.server_time} · Longbridge ${state.status.quota && state.status.quota.connected ? "connected" : "not connected"}`;
  }

  const pad2 = (n) => String(n).padStart(2, "0");
  function clockText() {
    const now = new Date();
    const local = `${pad2(now.getHours())}:${pad2(now.getMinutes())}:${pad2(now.getSeconds())}`;
    let et = "";
    try { et = now.toLocaleTimeString("en-US", { timeZone: "America/New_York", hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" }); } catch (e) { /* no tz support */ }
    return `${local}${et ? `<span class="et">${et} ET</span>` : ""}`;
  }
  setInterval(() => { const el = $("#pill-clock"); if (el) el.innerHTML = clockText(); }, 1000);

  function renderStatus(status) {
    const job = status.job;
    const quota = status.quota || {};
    const banner = $("#status-banner");
    const active = job && (job.state === "running" || job.state === "queued");
    const waiting = quota.waiting && active;
    state.waitDeadline = waiting ? Date.now() + quota.waiting_seconds * 1000 : null;
    $("#scan-btn").disabled = !!active;
    $("#watchlist-btn").disabled = !!active;
    $("#cancel-btn").classList.toggle("hidden", !active);

    if (quota.auth_error) {
      setPill("pill-error", "Longbridge auth error");
    } else if (waiting) {
      setPill("pill-waiting", `Waiting for API quota (${Math.ceil(quota.waiting_seconds)}s)`, true);
    } else if (active) {
      setPill("pill-working", `Working: ${job.phase}`, true);
    } else if (job && job.state === "error") {
      setPill("pill-error", "Last scan failed");
    } else if (!status.credentials_present) {
      setPill("pill-waiting", "Longbridge credentials missing");
    } else {
      setPill("pill-idle", quota.connected ? "Ready - Longbridge connected" : "Ready");
    }

    if (active) {
      banner.className = `banner ${waiting ? "waiting" : ""}`;
      $("#banner-spinner").classList.remove("hidden");
      $("#banner-phase").textContent = waiting
        ? `Waiting for Longbridge API quota - resuming in ${Math.ceil(quota.waiting_seconds)}s`
        : ticker(job.phase);
      $("#banner-message").textContent = waiting
        ? `${quota.waiting_reason}. Still working on: ${ticker(job.phase)}${job.message ? " - " + ticker(job.message) : ""}`
        : `${ticker(job.message || "")} | ${quota.total_calls} API calls, ${quota.rate_limited_events} quota hits | option contracts quoted this minute: ${quota.option_contracts_last_minute ?? "-"}/${quota.option_contracts_per_minute ?? "-"} | ${job.elapsed_seconds}s elapsed`;
      $("#banner-progress").style.width = `${Math.round((job.progress || 0) * 100)}%`;
    } else if (job && job.state === "error") {
      banner.className = "banner error";
      $("#banner-spinner").classList.add("hidden");
      $("#banner-phase").textContent = "Scan failed";
      $("#banner-message").textContent = job.error || "unknown error";
      $("#banner-progress").style.width = "100%";
    } else if (quota.auth_error) {
      banner.className = "banner error";
      $("#banner-spinner").classList.add("hidden");
      $("#banner-phase").textContent = "Longbridge authentication problem";
      $("#banner-message").textContent = `${quota.auth_error}. Update the LONGPORT_* credential exports in ~/.bashrc (environment/.env is used only when no profile exports exist), then restart with ./run.sh restart.`;
      $("#banner-progress").style.width = "0%";
    } else {
      banner.className = "banner hidden";
    }
  }

  // Tick the countdown between polls so the wait never looks frozen.
  setInterval(() => {
    if (!state.waitDeadline) return;
    const secs = Math.max(0, Math.ceil((state.waitDeadline - Date.now()) / 1000));
    setPill("pill-waiting", `Waiting for API quota (${secs}s)`, true);
    $("#banner-phase").textContent = secs > 0
      ? `Waiting for Longbridge API quota - resuming in ${secs}s`
      : "Quota window passed - retrying request";
  }, 500);

  // ------------------------------------------------------------------ scanning
  $("#scan-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    updateStrikeInput();
    if (!ev.currentTarget.reportValidity()) return;
    const symbols = $("#symbols-input").value.split(/[,\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
    const scenario = $("#scenario-select").value || null;
    const key = activeScenarioKey();
    const values = readFilterForm();
    const payload = { symbols, scenario };
    if (key && !filtersEqual(values, defaultFilters(key))) payload.filters = values;
    $("#scan-hint").textContent = "";
    try {
      await api("/api/scan", { method: "POST", body: JSON.stringify(payload) });
      schedulePoll(200);
    } catch (err) {
      $("#scan-hint").innerHTML = `<span class="error-text">${esc(err.message)}</span>`;
    }
  });

  $("#cancel-btn").addEventListener("click", async () => {
    try { await api("/api/scan/cancel", { method: "POST" }); } catch (err) { /* status poll will surface it */ }
  });

  async function loadRecommendations() {
    try {
      state.result = await api("/api/recommendations");
      renderResults(state.result);
    } catch (err) {
      if (!/no scan/i.test(err.message)) $("#scan-hint").innerHTML = `<span class="error-text">${esc(err.message)}</span>`;
    }
  }

  function filterSummary(filters) {
    const parts = [
      `${filters.dte?.[0]}-${filters.dte?.[1]} DTE`,
      `|delta| ${filters.delta?.[0]}-${filters.delta?.[1]}`,
      `OI >= ${filters.min_open_interest}`,
      `spread <= ${filters.max_spread_pct}%`,
    ];
    if (filters.min_annualized_yield_pct != null) parts.push(`annualized yield >= ${filters.min_annualized_yield_pct}%`);
    if (filters.target_delta != null) parts.push(`target |delta| ${filters.target_delta}`);
    if (filters.target_strike != null) {
      const operator = { exact: "=", lt: "<", gt: ">" }[strikeMode(filters)];
      parts.push(`strike ${operator} ${filters.target_strike}`);
    }
    parts.push(`top ${filters.max_results}`);
    return parts.join(", ");
  }

  function renderResults(result) {
    if (!result) return;
    const sc = result.scenario || {};
    const tickers = (result.symbols || []).map(ticker).join(", ");
    $("#scan-meta").textContent = `last scan ${result.finished_at} - ${tickers} - ${result.api_calls} API calls in ${result.duration_seconds}s - ${sc.title} (${result.scenario_source})`;
    const longVol = sc.stance === "long_vol";
    const optionSide = sc.option_side || "put";
    const custom = sc.filters_source === "custom";
    const head = `
      <div class="regime-action" style="margin-top:8px">
        <b>${esc(sc.title)}</b> - ${esc(sc.action)}
        <div class="muted small" style="margin-top:4px">${custom ? "custom filters" : "default filters"}: ${esc(filterSummary(sc.filters || {}))}</div>
      </div>
      ${(result.warnings || []).length ? `<div class="warnings">${result.warnings.map((w) => esc(ticker(w))).join("<br>")}</div>` : ""}`;
    const blocks = (result.underlyings || []).map((u) => renderUnderlying(u, longVol, optionSide)).join("");
    $("#results").innerHTML = head + blocks;
    setSummary("card-screener", `${sc.title} · ${tickers} · ${(result.underlyings || []).reduce((n, u) => n + (u.candidates || []).length, 0)} candidates`);
  }

  function renderUnderlying(u, longVol, optionSide) {
    if (u.error) {
      return `<div class="underlying"><div class="underlying-head"><h3>${esc(ticker(u.underlying))}</h3><span class="error-text">${esc(u.error)}</span></div></div>`;
    }
    const ref = u.reference || {};
    const ivr = u.iv_rank || {};
    const spot = u.spot || {};
    const head = `
      <div class="underlying-head">
        <h3>${esc(ticker(u.underlying))}</h3>
        <span class="metric">spot <b>${num(spot.last)}</b> (${signed(spot.change_pct)}%)</span>
        <span class="metric">ATM IV (${esc(ref.dte)}d) <b>${ref.atm_iv == null ? "-" : num(ref.atm_iv * 100, 1) + "%"}</b></span>
        <span class="metric">25d put IV <b>${ref.otm_put_iv == null ? "-" : num(ref.otm_put_iv * 100, 1) + "%"}</b></span>
        <span class="metric">chain skew <b>${num(ref.skew_ratio, 3)}</b> ${badge(ref.skew_level)}</span>
        <span class="metric">IV rank <b>${ivr.value == null ? "n/a" : num(ivr.value, 0)}</b> ${ivr.level ? badge(ivr.level) : ""} <span class="muted" title="${esc(ivr.note || "")}">${ivr.source ? `(${esc(ivr.source)})` : ""}</span></span>
        <span class="metric muted">${u.stats?.contracts_quoted ?? u.stats?.puts_quoted} contracts quoted, ${u.stats?.after_filters} passed filters</span>
      </div>`;
    const warnings = [...(u.warnings || []), ivr.value == null ? ivr.note : null].filter(Boolean);
    if (!u.candidates || !u.candidates.length) {
      return `<div class="underlying">${head}<div class="empty">No ${esc(optionSide)}s matched the scenario filters.</div>${warnings.length ? `<div class="warnings">${warnings.map(esc).join("<br>")}</div>` : ""}</div>`;
    }
    const valueCols = optionSide === "call"
      ? `<th>Contract $</th><th>Intrinsic</th><th>Extrinsic</th><th>Extrinsic / delta</th><th>Breakeven</th><th>Theta/day</th>`
      : longVol
        ? `<th>Cost % spot</th><th>Cost / delta</th><th>Theta/day</th>`
        : `<th>Yield %</th><th>Annualized %</th><th>Cushion %</th>`;
    const rows = u.candidates.map((c) => `
      <tr>
        <td>${c.rank}</td>
        <td class="left symbol-cell">${esc(ticker(c.symbol))}</td>
        <td>${esc(c.expiry)}</td>
        <td>${c.dte}</td>
        <td>${esc(c.strike)}</td>
        <td>${optionSide === "call" ? `${esc(c.moneyness)} ${num(c.moneyness_pct, 1)}%` : `${num(c.moneyness_pct, 1)}%`}</td>
        <td>${num(c.delta, 3)}</td>
        <td>${num(c.iv * 100, 1)}%</td>
        <td>${num(c.iv_ratio, 2)}</td>
        <td>${num(c.bid)}</td>
        <td>${num(c.ask)}</td>
        <td><b>${num(c.price)}</b></td>
        ${optionSide === "call"
          ? `<td>$${num(c.contract_premium, 0)}</td><td>${num(c.intrinsic_value)}</td><td>${num(c.extrinsic_value)}</td><td>${num(c.extrinsic_cost_per_delta_pct)}</td><td>${num(c.breakeven)} (${signed(c.breakeven_upside_pct, 1)}%)</td><td>${num(c.theta_per_day, 3)}</td>`
          : longVol
            ? `<td>${num(c.cost_pct_of_spot)}%</td><td>${num(c.cost_per_delta_pct)}</td><td>${num(c.theta_per_day, 3)}</td>`
            : `<td>${num(c.premium_yield_pct)}%</td><td>${num(c.annualized_yield_pct, 1)}%</td><td>${num(c.cushion_pct, 1)}%</td>`}
        <td>${c.open_interest}</td>
        <td>${c.volume}</td>
        <td>${num(c.spread_pct, 1)}%</td>
        <td><b>${num(c.score, 0)}</b></td>
        <td class="flags">${(c.flags || []).map(flagHtml).join("")}</td>
      </tr>`).join("");
    return `
      <div class="underlying">${head}
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>#</th><th class="left">Contract</th><th>Expiry</th><th>DTE</th><th>Strike</th><th>Moneyness</th><th>Delta</th><th>IV</th><th>IV/ATM</th>
              <th>Bid</th><th>Ask</th><th>Price</th>${valueCols}<th>OI</th><th>Vol</th><th>Spread</th><th>Score</th><th class="left">Notes</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        ${warnings.length ? `<div class="warnings">${warnings.map(esc).join("<br>")}</div>` : ""}
      </div>`;
  }

  function flagHtml(flag) {
    const cls = flag === "wide_spread" || flag === "no_depth" || flag === "no_volume_today" ? "warn" : flag === "cheap_iv" || flag === "rich_skew" ? "good" : "";
    return `<span class="flag ${cls}">${esc(flag.replace(/_/g, " "))}</span>`;
  }

  // ------------------------------------------------------------------ watchlist IV
  function watchlistSymbols() {
    return $("#watchlist-input").value.split(/[,\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
  }

  $("#watchlist-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    $("#watchlist-hint").textContent = "";
    try {
      await api("/api/watchlist/refresh", { method: "POST", body: JSON.stringify({ symbols: watchlistSymbols() }) });
      schedulePoll(200);
    } catch (err) {
      $("#watchlist-hint").innerHTML = `<span class="error-text">${esc(err.message)}</span>`;
    }
  });
  $("#watchlist-show").addEventListener("click", loadWatchlist);

  async function loadWatchlist() {
    try {
      const symbols = watchlistSymbols();
      const data = await api(`/api/watchlist${symbols.length ? `?symbols=${encodeURIComponent(symbols.join(","))}` : ""}`);
      renderWatchlist(data);
    } catch (err) {
      $("#watchlist-hint").innerHTML = `<span class="error-text">${esc(err.message)}</span>`;
    }
  }

  const pct = (v, d = 1) => (v == null ? "-" : num(v * 100, d) + "%");

  function renderWatchlist(data) {
    const items = data.items || [];
    const sched = data.scheduler || {};
    const last = data.last_refresh;
    $("#watchlist-meta").textContent = `${sched.enabled ? `auto-refresh weekdays ${sched.daily_at_et} ET` : "auto-refresh off"}${last ? ` - last refresh ${last.finished_at} (${last.api_calls} API calls, ${last.duration_seconds}s)` : ""}`;
    if (!items.length) { $("#watchlist-results").innerHTML = `<div class="empty">No tickers.</div>`; return; }
    const rows = items.map((it) => {
      if (it.missing) {
        return `<tr><td class="left"><b>${esc(it.ticker)}</b></td><td colspan="10" class="left muted">not refreshed yet (${it.snapshots || 0} daily snapshots stored) - click Refresh IV</td></tr>`;
      }
      if (it.error) {
        return `<tr><td class="left"><b>${esc(it.ticker)}</b></td><td colspan="10" class="left error-text">${esc(it.error)}</td></tr>`;
      }
      const r = it.iv_rank || {};
      const spot = it.spot || {};
      const range = r.low != null && r.high != null ? `${pct(r.low)} - ${pct(r.high)}` : "-";
      const source = r.source ? `<span title="${esc(r.note || "")}">${esc(r.source)}</span>` : "-";
      return `
        <tr>
          <td class="left"><b>${esc(it.ticker)}</b></td>
          <td>${num(spot.last)}</td>
          <td class="${spot.change_pct > 0 ? "gain" : spot.change_pct < 0 ? "loss" : ""}">${signed(spot.change_pct)}%</td>
          <td>${pct(it.atm_iv)}</td>
          <td>${r.value == null ? "-" : `<b>${num(r.value, 0)}</b> ${badge(r.level)}`}</td>
          <td>${num(r.percentile, 0)}</td>
          <td>${r.current_iv == null ? "-" : pct(r.current_iv)}</td>
          <td>${range}</td>
          <td class="left small">${source}</td>
          <td>${num(it.skew_ratio, 3)} ${badge(it.skew_level)}</td>
          <td class="muted small">${esc(it.snapshots)} / ${esc(String(it.updated_at || "").replace("T", " ").slice(0, 16))}</td>
        </tr>`;
    }).join("");
    $("#watchlist-results").innerHTML = `
      <table>
        <thead><tr>
          <th class="left">Ticker</th><th>Spot</th><th>Chg</th><th>ATM IV 30d</th><th>IV Rank</th><th>IV Pctl</th>
          <th>Series IV</th><th>52w IV range</th><th class="left">Rank source</th><th>25d skew</th><th>Snapshots / updated</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="muted small" style="margin-top:6px">IV Rank = (series IV - 52w low) / (52w high - 52w low). ETFs with a CBOE volatility index use that index; other names use our stored ATM-IV snapshots or a proxy rebuilt from a long-dated expiry's option history (hover the source for details).</div>`;
    // One line per distinct note, listing the tickers it applies to, instead of repeating it per ticker.
    const grouped = new Map();
    items.forEach((it) => {
      const note = it.iv_rank && it.iv_rank.note;
      if (!note || (it.iv_rank.source_kind === "cboe_index")) return;
      if (!grouped.has(note)) grouped.set(note, []);
      grouped.get(note).push(it.ticker);
    });
    $("#watchlist-hint").innerHTML = [...grouped.entries()].map(([note, tickers]) => `<div><b>${esc(tickers.join(", "))}</b>: ${esc(note)}</div>`).join("");
    setSummary("watchlist-card", items.filter((it) => it.iv_rank && it.iv_rank.value != null).map((it) => `${it.ticker} ${num(it.iv_rank.value, 0)}`).join(" · ") || `${items.length} tickers`);
  }

  const chinaEtfsState = { data: null, inflight: false, timer: null, refreshSeconds: 300, nextRefreshAt: 0, summary: "Loading fund reference data..." };
  const chinaEtfNumber = (value, decimals, suffix = "") => Number.isFinite(value) ? num(value, decimals, suffix) : "-";
  const chinaEtfDetail = (text, warning = false) => text ? `<div class="small ${warning ? "china-etfs-warning" : "muted"}">${esc(text)}</div>` : "";

  function chinaEtfLink(url, label) {
    try {
      const parsed = new URL(url);
      if (parsed.protocol === "https:" && !parsed.username && !parsed.password) {
        return `<a href="${esc(parsed.href)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;
      }
    } catch (err) { return esc(label); }
    return esc(label);
  }

  function chinaEtfDetails(part, missing, sourceLabel = "Source") {
    const status = [part.status === "stale" ? "Stale" : "", part.status === "unavailable" || missing ? "Unavailable" : ""].filter(Boolean).join(" · ");
    const source = part.source_url ? chinaEtfLink(part.source_url, sourceLabel) : `${esc(sourceLabel)} -`;
    return `${chinaEtfDetail(status, true)}${chinaEtfDetail(part.note)}
      <div class="muted small">${source} · retrieved ${esc(part.retrieved_at || "-")}</div>`;
  }

  function chinaEtfRow(item) {
    const close = item.close, nav = item.nav, profile = item.profile;
    const management = profile.management_fee_pct, custody = profile.custody_fee_pct;
    const feesKnown = Number.isFinite(management) && Number.isFinite(custody);
    const feeder = item.code === "161130";
    const feederNote = "Feeder headline rates; not all-in expenses.";
    const warnings = [...new Set(item.warnings || [])].filter((note) => !feeder || note !== feederNote);
    const dateMismatch = close.date && nav.date && close.date !== nav.date;
    const premiumKnown = Number.isFinite(item.premium_pct);
    return `<tr>
      <td class="left">
        <b lang="zh-CN">${chinaEtfLink(item.fund_url, item.name || "-")}</b>
        ${chinaEtfDetail(`${item.code || "-"} · ${item.exchange || "-"} · ${item.kind || "-"}`)}
        ${warnings.map((note) => chinaEtfDetail(note, true)).join("")}
      </td>
      <td><b>${chinaEtfNumber(close.value, 3)}</b>
        ${chinaEtfDetail(`Close date ${close.date || "-"}`)}
        ${chinaEtfDetails(close, !Number.isFinite(close.value))}
      </td>
      <td><b>${chinaEtfNumber(nav.value, 4)}</b>
        ${chinaEtfDetail(`Unit NAV date ${nav.date || "-"}`)}
        ${chinaEtfDetails(nav, !Number.isFinite(nav.value))}
      </td>
      <td><b class="${premiumKnown && item.premium_pct > 0 ? "china-etfs-premium" : ""}">${premiumKnown ? `${signed(item.premium_pct, 2)}%` : "-"}</b>
        ${dateMismatch ? chinaEtfDetail(`Dates differ: close ${close.date}; NAV ${nav.date}.`, true) : ""}
        ${premiumKnown && (close.status === "stale" || nav.status === "stale") ? chinaEtfDetail("Uses stale reference data", true) : ""}
        ${chinaEtfDetail(dateMismatch && premiumKnown ? "Reference only, not live fair value." : item.premium_note || (premiumKnown ? "" : "Reference unavailable"))}
      </td>
      <td><b>${feesKnown ? chinaEtfNumber(management + custody, 2, "%") : "-"}</b>
        ${chinaEtfDetail(`Management ${chinaEtfNumber(management, 2, "%")} + custody ${chinaEtfNumber(custody, 2, "%")}`)}
        ${feeder ? chinaEtfDetail(feederNote, true) : ""}
        ${chinaEtfDetail(profile.fee_note)}
        ${profile.fee_source_url ? `<div class="small">${chinaEtfLink(profile.fee_source_url, "Fee-base disclosure")}</div>` : ""}
        ${chinaEtfDetails(profile, !feesKnown, "Profile source")}
      </td>
      <td><b>${Number.isFinite(profile.aum_cny) ? chinaEtfNumber(profile.aum_cny / 1e9, 3) : "-"}</b>
        ${chinaEtfDetail(`Reported ${profile.aum_date || "-"}`)}
        ${chinaEtfDetail(`Scope: ${profile.aum_scope || "-"}`)}
        ${chinaEtfDetails(profile, !Number.isFinite(profile.aum_cny), "Profile source")}
      </td>
    </tr>`;
  }

  function setChinaEtfsStatus(message) {
    chinaEtfsState.summary = message;
    $("#china-etfs-meta").textContent = message;
    setSummary("card-china-etfs", message);
  }

  function renderChinaEtfs(data) {
    const items = data.items || [];
    const rows = ["Nasdaq-100", "S&P 500"].map((index) => {
      const group = items.filter((item) => item.index === index);
      return group.length ? `<tbody><tr class="china-etfs-group"><th colspan="6" scope="rowgroup">${esc(index)}</th></tr>${group.map(chinaEtfRow).join("")}</tbody>` : "";
    }).join("");
    $("#china-etfs-table").innerHTML = `<table aria-label="Mainland China US-index ETFs and LOFs">
      <thead><tr><th scope="col">Fund name</th><th scope="col">Previous close (CNY)</th><th scope="col">NAV (CNY)</th><th scope="col">Premium (reference)</th><th scope="col">Fees (annual)</th><th scope="col">AUM (CNY bn)</th></tr></thead>
      ${rows || `<tbody><tr><td colspan="6" class="left empty">${data.refreshing ? "Loading fund reference data; backend refreshing..." : "Fund reference data unavailable."}</td></tr></tbody>`}
    </table>`;
    const partial = items.some((item) => [item.close.value, item.nav.value, item.premium_pct, item.profile.management_fee_pct, item.profile.custody_fee_pct, item.profile.aum_cny].some((value) => !Number.isFinite(value)) || [item.close, item.nav, item.profile].some((part) => part.status === "unavailable"));
    const stale = items.filter((item) => [item.close, item.nav, item.profile].some((part) => part.status === "stale")).length;
    const available = items.some((item) => [item.close.value, item.nav.value, item.profile.management_fee_pct, item.profile.custody_fee_pct, item.profile.aum_cny].some(Number.isFinite));
    setChinaEtfsStatus([
      `${items.length} funds`,
      !available ? "Data unavailable" : partial ? "Partial data; missing fields shown as -" : "Reference data loaded",
      stale ? `${stale} funds with stale fields` : "",
      data.refreshing ? "Backend refreshing; checking every 3s while visible" : "Checks every 300s while visible; Refresh uses server cache",
      `Snapshot ${data.generated_at || "-"}`,
    ].filter(Boolean).join(" · "));
  }

  function scheduleChinaEtfsRefresh() {
    clearTimeout(chinaEtfsState.timer);
    chinaEtfsState.timer = null;
    if (chinaEtfsState.inflight || document.visibilityState !== "visible" || $("#card-china-etfs").classList.contains("collapsed")) return;
    chinaEtfsState.timer = setTimeout(loadChinaEtfs, Math.max(0, chinaEtfsState.nextRefreshAt - Date.now()));
  }

  async function loadChinaEtfs() {
    if (chinaEtfsState.inflight || document.visibilityState !== "visible") return;
    clearTimeout(chinaEtfsState.timer);
    chinaEtfsState.inflight = true;
    $("#china-etfs-refresh").disabled = true;
    $("#china-etfs-table").setAttribute("aria-busy", "true");
    setChinaEtfsStatus(chinaEtfsState.data ? "Checking fund reference data; displayed snapshot may be stale..." : "Loading fund reference data...");
    try {
      const data = await api("/api/china-etfs");
      renderChinaEtfs(data);
      chinaEtfsState.data = data;
      chinaEtfsState.refreshSeconds = data.refreshing ? 3 : 300;
    } catch (err) {
      if (!chinaEtfsState.data) $("#china-etfs-table").innerHTML = `<div class="empty">Fund reference data unavailable. Use Refresh to retry.</div>`;
      setChinaEtfsStatus(`Update unavailable; ${chinaEtfsState.data ? `showing last loaded snapshot ${chinaEtfsState.data.generated_at || "-"} (may be stale)` : "no reference data loaded"}. ${err.message}`);
      chinaEtfsState.refreshSeconds = 300;
    } finally {
      chinaEtfsState.inflight = false;
      $("#china-etfs-refresh").disabled = false;
      $("#china-etfs-table").setAttribute("aria-busy", "false");
      chinaEtfsState.nextRefreshAt = Date.now() + Math.max(3, chinaEtfsState.refreshSeconds) * 1000;
      scheduleChinaEtfsRefresh();
    }
  }

  $("#china-etfs-refresh").addEventListener("click", loadChinaEtfs);
  document.addEventListener("visibilitychange", scheduleChinaEtfsRefresh);

  // ------------------------------------------------------------------ fold / unfold
  const FOLD_KEY = "trade-plat.folded";

  function foldedState() {
    try { return JSON.parse(localStorage.getItem(FOLD_KEY) || "{}"); } catch (e) { return {}; }
  }

  function saveFolded(all) {
    try { localStorage.setItem(FOLD_KEY, JSON.stringify(all)); } catch (e) { /* storage unavailable */ }
  }

  function setCollapsed(card, collapsed) {
    card.classList.toggle("collapsed", collapsed);
    const btn = card.querySelector(".fold-btn");
    if (btn) { btn.textContent = collapsed ? "▸" : "▾"; btn.title = collapsed ? "Unfold" : "Fold"; }
    if (!collapsed) {
      // Charts drawn while hidden have no size; give them one now that the card is visible.
      requestAnimationFrame(() => Object.values(state.charts).forEach((ch) => { if (card.contains(ch.canvas)) ch.resize(); }));
      liveSections.forEach((ctl) => { if (ctl.card === card && ctl.data) loadLiveSection(ctl); });
    }
    if (card.id === "card-china-etfs") scheduleChinaEtfsRefresh();
  }

  function setSummary(cardId, text) {
    const el = document.querySelector(`#${cardId} .fold-summary`);
    if (el) el.textContent = text || "";
  }

  function updateFoldAllLabel() {
    const anyOpen = [...document.querySelectorAll(".chart-card")].some((c) => !c.classList.contains("collapsed"));
    $("#fold-charts").textContent = anyOpen ? "Fold charts" : "Unfold charts";
  }

  function initFolding() {
    const saved = foldedState();
    document.querySelectorAll(".card").forEach((card) => {
      const head = card.querySelector(".card-head");
      if (!head || !card.id) return;
      const summary = document.createElement("span");
      summary.className = "fold-summary";
      head.appendChild(summary);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "fold-btn";
      btn.addEventListener("click", () => {
        const collapsed = !card.classList.contains("collapsed");
        setCollapsed(card, collapsed);
        const all = foldedState();
        all[card.id] = collapsed;
        saveFolded(all);
        updateFoldAllLabel();
      });
      head.appendChild(btn);
      setCollapsed(card, !!saved[card.id]);
    });
    $("#fold-charts").addEventListener("click", () => {
      const charts = [...document.querySelectorAll(".chart-card")];
      const fold = charts.some((c) => !c.classList.contains("collapsed"));
      const all = foldedState();
      charts.forEach((c) => { setCollapsed(c, fold); all[c.id] = fold; });
      saveFolded(all);
      updateFoldAllLabel();
    });
    updateFoldAllLabel();
  }

  // ------------------------------------------------------------------ Kalshi: next Fed decision odds
  const kalshiStates = new Map();
  const KALSHI_COLORS = ["#4ade80", "#f87171", "#60a5fa", "#fbbf24", "#a78bfa", "#f472b6", "#2dd4bf", "#fb923c"];

  function kalshiSelector(ctl, suffix) {
    return ctl.id === "next" ? `#kalshi-${suffix}` : `#kalshi-${ctl.id}-${suffix}`;
  }

  function initKalshi(configs) {
    (configs || []).forEach((cfg) => {
      const id = cfg.id || "next";
      const ctl = {
        id, enabled: cfg.enabled !== false, range: null, timer: null, data: null, inflight: false,
        card: $(id === "next" ? "#card-kalshi" : `#card-kalshi-${id}`),
        storageKey: `trade-plat.kalshiRange.${id}`,
      };
      if (!ctl.card) return;
      if (!ctl.enabled) { ctl.card.classList.add("hidden"); return; }
      $(kalshiSelector(ctl, "title")).textContent = cfg.title;
      try {
        const legacy = id === "next" ? localStorage.getItem("trade-plat.kalshiRange") : null;
        ctl.range = localStorage.getItem(ctl.storageKey) || legacy || "1m";
      } catch (e) { ctl.range = "1m"; }
      ctl.card.querySelectorAll(".range-buttons button").forEach((btn) => btn.addEventListener("click", () => {
        ctl.range = btn.dataset.range;
        try { localStorage.setItem(ctl.storageKey, ctl.range); } catch (e) { /* ignore */ }
        loadKalshi(ctl);
      }));
      kalshiStates.set(id, ctl);
    });
  }

  async function loadKalshi(ctl) {
    if (!ctl.enabled || ctl.inflight) return;
    ctl.inflight = true;
    ctl.card.querySelectorAll(".range-buttons button").forEach((b) => b.classList.toggle("active", b.dataset.range === ctl.range));
    try {
      const chartParam = ctl.id === "next" ? "" : `&chart=${encodeURIComponent(ctl.id)}`;
      const data = await api(`/api/kalshi?range=${encodeURIComponent(ctl.range)}${chartParam}`);
      ctl.data = data;
      renderKalshi(ctl, data);
      scheduleKalshiRefresh(ctl, (data.refresh_seconds || 30) * 1000);
    } catch (err) {
      $(kalshiSelector(ctl, "live")).textContent = `update failed: ${err.message}`;
      scheduleKalshiRefresh(ctl, 60000);
    } finally {
      ctl.inflight = false;
    }
  }

  function scheduleKalshiRefresh(ctl, ms) {
    clearTimeout(ctl.timer);
    ctl.timer = setTimeout(() => {
      if (document.visibilityState === "visible" && !ctl.card.classList.contains("collapsed")) loadKalshi(ctl); else scheduleKalshiRefresh(ctl, 15000);
    }, ms);
  }

  const kalshiTimeLabel = (ms, range) => {
    const d = new Date(ms);
    const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    const md = `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
    return range === "1d" ? hm : range === "5d" ? `${md} ${hm}` : md;
  };

  function renderKalshi(ctl, data) {
    const event = data.event;
    if (!event) {
      $(kalshiSelector(ctl, "event")).textContent = data.error || "no open market";
      $(kalshiSelector(ctl, "table")).innerHTML = "";
      return;
    }
    const when = event.strike_date ? new Date(event.strike_date) : null;
    const days = when ? Math.max(0, Math.round((when - Date.now()) / 86400000)) : null;
    $(kalshiSelector(ctl, "event")).textContent = `${event.title} · ${event.sub_title || ""}${days != null ? ` · in ${days} days` : ""} · ${event.ticker}`;
    const stamp = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    $(kalshiSelector(ctl, "live")).innerHTML = `<span class="live-badge"><span class="dot"></span>LIVE</span> refreshed ${esc(stamp)} · every ${Math.round(data.refresh_seconds || 30)}s`;

    const markets = data.markets || [];
    const rows = markets.map((m, i) => `
      <tr>
        <td><span class="swatch" style="background:${KALSHI_COLORS[i % KALSHI_COLORS.length]}"></span>${esc(m.label)}</td>
        <td><b>${num(m.last_pct, 0)}%</b></td>
        <td>${num(m.yes_bid_pct, 0)} / ${num(m.yes_ask_pct, 0)}</td>
        <td class="${m.previous_pct != null && m.last_pct != null ? (m.last_pct > m.previous_pct ? "gain" : m.last_pct < m.previous_pct ? "loss" : "") : ""}">${m.previous_pct != null && m.last_pct != null ? signed(m.last_pct - m.previous_pct, 0) : "-"}</td>
        <td>${m.volume_24h != null ? num(m.volume_24h, 0) : "-"}</td>
        <td>${m.open_interest != null ? num(m.open_interest, 0) : "-"}</td>
      </tr>`).join("");
    $(kalshiSelector(ctl, "table")).innerHTML = `
      <table class="kalshi-table">
        <thead><tr><th>Outcome</th><th>Yes</th><th>Bid / ask</th><th>Chg</th><th>Vol 24h</th><th>Open int.</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    $(kalshiSelector(ctl, "note")).textContent = `Prices are Kalshi yes-contract prices in cents = implied probability; ${data.config && data.config.markets && data.config.markets.length ? `showing ${data.config.markets.join(", ")}` : "showing all outcomes"}.`;
    setSummary(ctl.card.id, markets.slice(0, 3).map((m) => `${m.label} ${num(m.last_pct, 0)}%`).join(" · "));

    if (typeof Chart === "undefined") return;
    const datasets = markets.map((m, i) => ({
      label: m.label,
      data: m.series.map((p) => ({ x: Date.parse(p.t), y: p.v })),
      borderColor: KALSHI_COLORS[i % KALSHI_COLORS.length],
      backgroundColor: "transparent",
      borderWidth: 1.6, pointRadius: 0, pointHitRadius: 8, tension: 0.05, spanGaps: true, stepped: false,
    }));
    const range = data.range;
    const chartKey = `kalshi:${ctl.id}`;
    const existing = state.charts[chartKey];
    if (existing) {
      existing.data.datasets.forEach((ds, i) => { if (datasets[i]) { ds.data = datasets[i].data; ds.label = datasets[i].label; } });
      while (existing.data.datasets.length > datasets.length) existing.data.datasets.pop();
      for (let i = existing.data.datasets.length; i < datasets.length; i++) existing.data.datasets.push(datasets[i]);
      existing.options.scales.x.ticks.callback = (v) => kalshiTimeLabel(v, range);
      existing.update("none");
      return;
    }
    state.charts[chartKey] = new Chart($(kalshiSelector(ctl, "chart")), {
      type: "line",
      data: { datasets },
      options: {
        animation: false, responsive: true, maintainAspectRatio: false, parsing: false,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        plugins: {
          legend: { display: true, labels: { color: "#e6edf3", boxWidth: 10, font: { size: 11 } } },
          tooltip: { callbacks: { title: (items) => (items[0] ? new Date(items[0].parsed.x).toLocaleString() : ""), label: (ctx) => `${ctx.dataset.label}: ${Number(ctx.parsed.y).toFixed(0)}%` } },
        },
        scales: {
          x: { type: "linear", ticks: { color: "#8b98a8", maxTicksLimit: 8, maxRotation: 0, font: { size: 10 }, callback: (v) => kalshiTimeLabel(v, range) }, grid: { color: "#1f2a37" } },
          y: { min: 0, max: 100, ticks: { color: "#8b98a8", callback: (v) => `${v}%` }, grid: { color: "#1f2a37" } },
        },
      },
    });
  }

  // ------------------------------------------------------------------ section navigation
  const NAV_LABELS = { "card-vix": "VIX", "card-skew": "SKEW", "card-fg": "Fear & Greed", "card-volcomplex": "Vol complex", "card-stocks": "Stocks", "card-comparisons": "Comparisons", "card-china-etfs": "China ETFs / LOFs", "card-breadth": "S&P 500 breadth", "card-global_equities": "Global equities", "card-macro": "Rates", "card-commodities": "Commodities", "card-kalshi": "Fed odds", "card-kalshi-december_hikes": "December hikes", "regime-card": "Regime", "watchlist-card": "Watchlist", "card-screener": "Screener" };

  function initNav() {
    const nav = $("#section-nav");
    nav.innerHTML = "";
    document.querySelectorAll("main .card[id]").forEach((card) => {
      if (card.classList.contains("hidden")) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = NAV_LABELS[card.id] || (card.querySelector("h2") || {}).textContent || card.id;
      btn.addEventListener("click", () => {
        if (card.classList.contains("collapsed")) {
          setCollapsed(card, false);
          const all = foldedState(); all[card.id] = false; saveFolded(all); updateFoldAllLabel();
        }
        const header = document.querySelector(".topbar").getBoundingClientRect().height;
        window.scrollTo({ top: card.getBoundingClientRect().top + window.scrollY - header - 10, behavior: "smooth" });
      });
      nav.appendChild(btn);
    });
  }

  // ------------------------------------------------------------------ init
  async function init() {
    loadChinaEtfs();
    try {
      state.config = await api("/api/config");
      buildLiveSections(state.config.live_sections, state.config.live_default_range);
      $("#symbols-input").value = (state.config.watchlist || []).map(ticker).join(", ");
      $("#watchlist-input").value = (state.config.watchlist || []).map(ticker).join(", ");
      const select = $("#scenario-select");
      (state.config.scenarios || []).forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s.key;
        opt.textContent = s.title;
        select.appendChild(opt);
      });
    } catch (err) {
      $("#scan-hint").innerHTML = `<span class="error-text">${esc(err.message)}</span>`;
    }
    initFolding();
    setSummary("card-china-etfs", chinaEtfsState.summary);
    initKalshi(state.config && state.config.kalshi_charts);
    initNav();
    await loadMarket();
    await Promise.all([...liveSections.values()].map(loadLiveSection));
    kalshiStates.forEach(loadKalshi);
    await loadRecommendations();
    await loadWatchlist();
    pollStatus();
    setInterval(loadMarket, 60 * 1000);
  }

  init();
})();
