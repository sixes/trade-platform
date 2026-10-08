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

  const FILTER_FIELDS = ["dte_min", "dte_max", "delta_min", "delta_max", "min_open_interest", "max_spread_pct", "min_annualized_yield_pct", "target_delta", "max_results"];
  const OPTIONAL_FIELDS = new Set(["min_annualized_yield_pct", "target_delta"]);
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
      $("#regime-body").innerHTML = `<div class="error-text">Could not load index data: ${esc(err.message)}</div>`;
    }
  }

  // Unit-aware formatting for the mini cards: yields in % with bp changes, ETF prices in $, indices plain.
  const fmtValue = (v, unit) => (v == null ? "-" : unit === "pct" ? `${num(v, 2)}%` : unit === "usd" ? `$${num(v, 2)}` : num(v, 2));
  const fmtPrecise = (v, unit) => (v == null ? "-" : unit === "pct" ? `${num(v, 3)}%` : unit === "usd" ? `$${num(v, 2)}` : unit === "vol" ? num(v, 2) : num(v, 3));
  const fmtChange = (chg, unit, base) => {
    if (chg == null) return "-";
    if (unit === "pct") return `${signed(chg * 100, 0)} bp`;
    if (unit === "usd") return `${signed(chg, 2)}${base ? ` (${signed((chg / (base - chg)) * 100, 2)}%)` : ""}`;
    return signed(chg, 2);
  };
  const fmtRange = (lo, hi, unit) => (lo == null || hi == null ? "-" : unit === "pct" ? `${num(lo, 2)}-${num(hi, 2)}%` : `${num(lo, unit === "usd" ? 2 : 1)} - ${num(hi, unit === "usd" ? 2 : 1)}`);

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
      if (entry.error) { el.innerHTML = `<span class="muted small">unavailable</span>`; return; }
      const chg = m.change_1d;
      const source = entry.source ? `<span class="sub" title="${esc(entry.source)}">${esc(String(entry.source).split(":")[0])}</span>` : "";
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
        id: section.id, card: node, curve: !!section.curve, range: null, timer: null, data: null, inflight: false,
        storageKey: `trade-plat.liveRange.${section.id}`, defaultRange: section.hidden ? "1y" : (defaultRange || "1d"),
        variantsKey: `trade-plat.liveVariants.${section.id}`, variants: {},
        symbolsKey: `trade-plat.liveSymbols.${section.id}`, symbols: null, defaultSymbols: null,
        render: section.id === "vix" ? renderVixLive : null,
      };
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
      ctl.range = saved || ctl.defaultRange;
    }
    return ctl.range;
  }

  async function loadLiveSection(ctl) {
    if (ctl.inflight) { ctl.rerun = true; return; }
    ctl.inflight = true;
    ctl.card.querySelectorAll(".range-buttons button").forEach((b) => b.classList.toggle("active", b.dataset.range === sectionRange(ctl)));
    try {
      const variantParam = Object.entries(ctl.variants).map(([k, v]) => `${k}:${v}`).join(",");
      const symbolParam = Array.isArray(ctl.symbols) ? `&symbols=${encodeURIComponent(ctl.symbols.join(","))}` : "";
      const data = await api(`/api/macro?section=${encodeURIComponent(ctl.id)}&range=${encodeURIComponent(sectionRange(ctl))}${variantParam ? `&variants=${encodeURIComponent(variantParam)}` : ""}${symbolParam}`);
      ctl.data = data;
      (ctl.render || renderLiveSection)(ctl, data);
      scheduleLiveRefresh(ctl, ((data.refresh_seconds || data.live_ttl_seconds || 55) * 1000) + 300);
    } catch (err) {
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
      badge.className = `live-badge ${m.live ? "" : "off"}`;
      badge.innerHTML = m.live ? `<span class="dot"></span>LIVE` : "DAILY";
      badge.title = m.live ? `${entry.live_source} - as of ${m.as_of}` : (entry.live_note || "");
      if (entry.error) {
        $(`#live-${sid}-stats-${id}`).innerHTML = `<span class="sub">unavailable - no daily history or live quote from ${esc((entry.live_source || entry.daily_source || "any source"))}${editable ? "; check the ticker" : ""}</span>`;
        return;
      }
      const chg = m.change;
      const rangeNote = entry.fallback ? `<span class="sub">no intraday feed - showing 1M daily</span>` : "";
      const note = entry.note ? `<span class="sub note" title="${esc(entry.note)}">Note: ${esc(entry.note)}</span>` : "";
      $(`#live-${sid}-stats-${id}`).innerHTML = `
        <span class="value">${fmtValue(m.value, unit)}</span>
        <span class="chg ${chg > 0 ? "gain" : chg < 0 ? "loss" : ""}">${fmtChange(chg, unit, m.value)}${unit !== "usd" && m.change_pct != null ? ` (${signed(m.change_pct, 2)}%)` : ""}</span>
        ${m.day_low != null ? `<span class="sub">day ${fmtRange(m.day_low, m.day_high, unit)}</span>` : ""}
        <span class="sub">52w ${fmtRange(m.low_52w, m.high_52w, unit)}</span>
        <span class="sub">pctl ${num(m.percentile, 0)}</span>
        <span class="sub" title="${esc(entry.daily_source || "")}">${esc(String(entry.live_source || entry.daily_source || "").split(":")[0])}</span>
        ${rangeNote}${note}`;
      if (typeof Chart !== "undefined" && entry.series && entry.series.length) {
        drawSeriesChart(`live:${sid}:${name}`, `#live-${sid}-chart-${id}`, entry.series, LIVE_PALETTE[i % LIVE_PALETTE.length], entry.intraday ? data.range : "daily", unit, name);
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
        ctx.fillStyle = chart.data.datasets[0].borderColor;
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

  // Line chart for a series of {t, v} points; updates in place so live refreshes do not flicker.
  function drawSeriesChart(key, selector, series, color, range, unit, name, thresholds) {
    const labels = series.map((p) => timeLabel(p.t, range));
    const values = series.map((p) => p.v);
    const guides = (thresholds || []).map(([value, label]) => ({ label, data: labels.map(() => value), borderColor: "#5b6b7e", borderDash: [4, 4], borderWidth: 1, pointRadius: 0, pointHitRadius: 0, fill: false, order: 2 }));
    const existing = state.charts[key];
    if (existing) {
      existing.data.labels = labels;
      existing.data.datasets[0].data = values;
      guides.forEach((g, i) => { if (existing.data.datasets[i + 1]) existing.data.datasets[i + 1].data = g.data; });
      existing.$series = series;
      existing.update("none");
      return;
    }
    const canvas = $(selector);
    if (!canvas) return;
    const chart = new Chart(canvas, {
      type: "line",
      plugins: [crosshairPlugin],
      data: { labels, datasets: [{ label: name || key, data: values, borderColor: color, backgroundColor: color + "22", borderWidth: 1.5, pointRadius: 0, pointHitRadius: 12, tension: 0.1, fill: true, order: 1 }, ...guides] },
      options: {
        animation: false, responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          crosshair: { format: (v) => fmtPrecise(v, unit) },
          tooltip: {
            filter: (item) => item.datasetIndex === 0,
            callbacks: {
              title: (items) => (items[0] && chart.$series ? String(chart.$series[items[0].dataIndex].t).replace("T", " ").replace(/[-+]\d\d:\d\d$/, "") : ""),
              label: (ctx) => `${ctx.dataset.label}: ${fmtPrecise(ctx.parsed.y, unit)}`,
            },
          },
        },
        scales: {
          x: { ticks: { color: "#8b98a8", maxTicksLimit: 6, maxRotation: 0, font: { size: 10 } }, grid: { color: "#1f2a37" } },
          y: { ticks: { color: "#8b98a8", maxTicksLimit: 5, font: { size: 10 } }, grid: { color: "#1f2a37" } },
        },
      },
    });
    chart.$series = series;
    state.charts[key] = chart;
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
      if (data.series && data.series.length) drawChart(`mini-charts:${name}`, `#mini-charts-chart-${name}`, data.series, palette[i % palette.length], [], null, true);
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
      max_results: f.max_results ?? 12,
    };
  }

  function storedFilters() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); } catch (e) { return {}; }
  }

  function saveStoredFilters(all) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(all)); } catch (e) { /* storage unavailable */ }
  }

  function readFilterForm() {
    const out = {};
    FILTER_FIELDS.forEach((name) => {
      const raw = $(`#scan-form [name="${name}"]`).value.trim();
      out[name] = raw === "" ? null : Number(raw);
    });
    return out;
  }

  function writeFilterForm(values, defaults) {
    FILTER_FIELDS.forEach((name) => {
      const input = $(`#scan-form [name="${name}"]`);
      const value = values[name];
      input.value = value == null ? "" : value;
      input.classList.toggle("changed", !sameValue(value, defaults[name]));
    });
  }

  function sameValue(a, b) {
    if (a == null && b == null) return true;
    if (a == null || b == null) return false;
    return Math.abs(Number(a) - Number(b)) < 1e-9;
  }

  function filtersEqual(a, b) {
    return FILTER_FIELDS.every((name) => sameValue(a[name], b[name]));
  }

  function loadFilters() {
    const key = activeScenarioKey();
    const meta = scenarioMeta(key);
    const defaults = defaultFilters(key);
    const stored = storedFilters()[key];
    const values = stored ? Object.assign({}, defaults, stored) : defaults;
    writeFilterForm(values, defaults);
    const auto = !$("#scenario-select").value;
    $("#filters-scenario").textContent = meta ? `- ${auto ? "auto: " : ""}${meta.title}` : "";
    updateFilterState(values, defaults);
  }

  function updateFilterState(values, defaults) {
    const custom = !filtersEqual(values, defaults);
    $("#filters-state").textContent = custom ? "Custom values (highlighted) will be used for the next scan." : "Using scenario defaults from config.yaml.";
  }

  function onFilterInput() {
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

  document.querySelectorAll("#scan-form .filters input").forEach((input) => input.addEventListener("change", onFilterInput));
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
      $("#banner-message").textContent = `${quota.auth_error}. Update LONGPORT_ACCESS_TOKEN in .env and restart with ./run.sh restart.`;
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
    parts.push(`top ${filters.max_results}`);
    return parts.join(", ");
  }

  function renderResults(result) {
    if (!result) return;
    const sc = result.scenario || {};
    const tickers = (result.symbols || []).map(ticker).join(", ");
    $("#scan-meta").textContent = `last scan ${result.finished_at} - ${tickers} - ${result.api_calls} API calls in ${result.duration_seconds}s - ${sc.title} (${result.scenario_source})`;
    const longVol = sc.stance === "long_vol";
    const custom = sc.filters_source === "custom";
    const head = `
      <div class="regime-action" style="margin-top:8px">
        <b>${esc(sc.title)}</b> - ${esc(sc.action)}
        <div class="muted small" style="margin-top:4px">${custom ? "custom filters" : "default filters"}: ${esc(filterSummary(sc.filters || {}))}</div>
      </div>
      ${(result.warnings || []).length ? `<div class="warnings">${result.warnings.map((w) => esc(ticker(w))).join("<br>")}</div>` : ""}`;
    const blocks = (result.underlyings || []).map((u) => renderUnderlying(u, longVol)).join("");
    $("#results").innerHTML = head + blocks;
    setSummary("card-screener", `${sc.title} · ${tickers} · ${(result.underlyings || []).reduce((n, u) => n + (u.candidates || []).length, 0)} candidates`);
  }

  function renderUnderlying(u, longVol) {
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
        <span class="metric muted">${u.stats?.puts_quoted} puts quoted, ${u.stats?.after_filters} passed filters</span>
      </div>`;
    const warnings = [...(u.warnings || []), ivr.value == null ? ivr.note : null].filter(Boolean);
    if (!u.candidates || !u.candidates.length) {
      return `<div class="underlying">${head}<div class="empty">No puts matched the scenario filters.</div>${warnings.length ? `<div class="warnings">${warnings.map(esc).join("<br>")}</div>` : ""}</div>`;
    }
    const valueCols = longVol
      ? `<th>Cost % spot</th><th>Cost / delta</th><th>Theta/day</th>`
      : `<th>Yield %</th><th>Annualized %</th><th>Cushion %</th>`;
    const rows = u.candidates.map((c) => `
      <tr>
        <td>${c.rank}</td>
        <td class="left symbol-cell">${esc(ticker(c.symbol))}</td>
        <td>${esc(c.expiry)}</td>
        <td>${c.dte}</td>
        <td>${num(c.strike)}</td>
        <td>${num(c.moneyness_pct, 1)}%</td>
        <td>${num(c.delta, 3)}</td>
        <td>${num(c.iv * 100, 1)}%</td>
        <td>${num(c.iv_ratio, 2)}</td>
        <td>${num(c.bid)}</td>
        <td>${num(c.ask)}</td>
        <td><b>${num(c.price)}</b></td>
        ${longVol
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
              <th>#</th><th class="left">Contract</th><th>Expiry</th><th>DTE</th><th>Strike</th><th>OTM</th><th>Delta</th><th>IV</th><th>IV/ATM</th>
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
  const KALSHI_RANGE_KEY = "trade-plat.kalshiRange";
  const kalshiState = { enabled: false, range: null, timer: null, data: null, inflight: false };
  const KALSHI_COLORS = ["#4ade80", "#f87171", "#60a5fa", "#fbbf24", "#a78bfa", "#f472b6", "#2dd4bf", "#fb923c"];

  function initKalshi(cfg) {
    const card = $("#card-kalshi");
    kalshiState.enabled = !!(cfg && cfg.enabled !== false);
    if (!kalshiState.enabled) { card.classList.add("hidden"); return; }
    if (cfg.title) $("#kalshi-title").textContent = cfg.title;
    try { kalshiState.range = localStorage.getItem(KALSHI_RANGE_KEY) || "1m"; } catch (e) { kalshiState.range = "1m"; }
    card.querySelectorAll("#kalshi-ranges button").forEach((btn) => btn.addEventListener("click", () => {
      kalshiState.range = btn.dataset.range;
      try { localStorage.setItem(KALSHI_RANGE_KEY, kalshiState.range); } catch (e) { /* ignore */ }
      loadKalshi();
    }));
  }

  async function loadKalshi() {
    if (!kalshiState.enabled || kalshiState.inflight) return;
    kalshiState.inflight = true;
    document.querySelectorAll("#kalshi-ranges button").forEach((b) => b.classList.toggle("active", b.dataset.range === kalshiState.range));
    try {
      const data = await api(`/api/kalshi?range=${encodeURIComponent(kalshiState.range)}`);
      kalshiState.data = data;
      renderKalshi(data);
      scheduleKalshiRefresh((data.refresh_seconds || 30) * 1000);
    } catch (err) {
      $("#kalshi-live").textContent = `update failed: ${err.message}`;
      scheduleKalshiRefresh(60000);
    } finally {
      kalshiState.inflight = false;
    }
  }

  function scheduleKalshiRefresh(ms) {
    clearTimeout(kalshiState.timer);
    kalshiState.timer = setTimeout(() => {
      const folded = $("#card-kalshi").classList.contains("collapsed");
      if (document.visibilityState === "visible" && !folded) loadKalshi(); else scheduleKalshiRefresh(15000);
    }, ms);
  }

  const kalshiTimeLabel = (ms, range) => {
    const d = new Date(ms);
    const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    const md = `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
    return range === "1d" ? hm : range === "5d" ? `${md} ${hm}` : md;
  };

  function renderKalshi(data) {
    const event = data.event;
    if (!event) {
      $("#kalshi-event").textContent = data.error || "no open market";
      $("#kalshi-table").innerHTML = "";
      return;
    }
    const when = event.strike_date ? new Date(event.strike_date) : null;
    const days = when ? Math.max(0, Math.round((when - Date.now()) / 86400000)) : null;
    $("#kalshi-event").textContent = `${event.title} · ${event.sub_title || ""}${days != null ? ` · in ${days} days` : ""} · ${event.ticker}`;
    const stamp = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    $("#kalshi-live").innerHTML = `<span class="live-badge"><span class="dot"></span>LIVE</span> refreshed ${esc(stamp)} · every ${Math.round(data.refresh_seconds || 30)}s`;

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
    $("#kalshi-table").innerHTML = `
      <table class="kalshi-table">
        <thead><tr><th>Outcome</th><th>Yes</th><th>Bid / ask</th><th>Chg</th><th>Vol 24h</th><th>Open int.</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    $("#kalshi-note").textContent = `Prices are Kalshi yes-contract prices in cents = implied probability. Configure which outcomes to chart in config.yaml (kalshi.markets); ${data.config && data.config.markets && data.config.markets.length ? `showing ${data.config.markets.join(", ")}` : "showing all outcomes"}.`;
    setSummary("card-kalshi", markets.slice(0, 3).map((m) => `${m.label} ${num(m.last_pct, 0)}%`).join(" · "));

    if (typeof Chart === "undefined") return;
    const datasets = markets.map((m, i) => ({
      label: m.label,
      data: m.series.map((p) => ({ x: Date.parse(p.t), y: p.v })),
      borderColor: KALSHI_COLORS[i % KALSHI_COLORS.length],
      backgroundColor: "transparent",
      borderWidth: 1.6, pointRadius: 0, pointHitRadius: 8, tension: 0.05, spanGaps: true, stepped: false,
    }));
    const range = data.range;
    const existing = state.charts.kalshi;
    if (existing) {
      existing.data.datasets.forEach((ds, i) => { if (datasets[i]) { ds.data = datasets[i].data; ds.label = datasets[i].label; } });
      while (existing.data.datasets.length > datasets.length) existing.data.datasets.pop();
      for (let i = existing.data.datasets.length; i < datasets.length; i++) existing.data.datasets.push(datasets[i]);
      existing.options.scales.x.ticks.callback = (v) => kalshiTimeLabel(v, range);
      existing.update("none");
      return;
    }
    state.charts.kalshi = new Chart($("#kalshi-chart"), {
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
  const NAV_LABELS = { "card-vix": "VIX", "card-skew": "SKEW", "card-fg": "Fear & Greed", "card-volcomplex": "Vol complex", "card-stocks": "Stocks", "card-macro": "Rates", "card-commodities": "Commodities", "card-kalshi": "Fed odds", "regime-card": "Regime", "watchlist-card": "Watchlist", "card-screener": "Screener" };

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
    initKalshi(state.config && state.config.kalshi);
    initNav();
    await loadMarket();
    await Promise.all([...liveSections.values()].map(loadLiveSection));
    if (kalshiState.enabled) loadKalshi();
    await loadRecommendations();
    await loadWatchlist();
    pollStatus();
    setInterval(loadMarket, 60 * 1000);
  }

  init();
})();
