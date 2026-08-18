/* ---------------------------------------------------------------------------
 * Stellwerk dashboard.
 *
 * One WebSocket carries the whole payload on the feed's own 10s rhythm. Charts
 * are created once and fed with setOption, so ECharts animates between states
 * instead of tearing down and rebuilding.
 *
 * Train markers are the exception: MapLibre GeoJSON sources snap rather than
 * tween, so positions are lerped client-side across the push interval. That is
 * what makes trains glide instead of teleporting every ten seconds.
 * --------------------------------------------------------------------------- */

'use strict';

const PUSH_MS = 10_000;          // Matches the server's WebSocket heartbeat.
const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/* Mirrors :root in styles.css. Two mirrored palettes is a real cost, so keep
   them in sync: severity is monotone in lightness by construction. */
const C = {
  ink: '#eef2f8', ink2: '#a8b6ca', muted: '#8195ad',
  panel: '#131a24', raise: '#1a2331', line: '#243041',
  d0: '#007a2a', d1: '#a28500', d2: '#ff7406', d3: '#ff9c9c',
  ice: '#3987e5', ic: '#d95926', ec: '#9085e9', ece: '#c98500', oth: '#8195ad',
  ok: '#199e70', warn: '#c98500', bad: '#e66767',
};

/* The severity scale, defined once and used by the map, the legend and the
   distribution chart so they cannot drift apart. */
const SEVERITY = [
  { max: 180,       color: C.d0, label: 'On time (under 3 min)' },
  { max: 360,       color: C.d1, label: '3 to 6 min' },
  { max: 900,       color: C.d2, label: '6 to 15 min' },
  { max: Infinity,  color: C.d3, label: '15 min or more' },
];

const severityColor = (seconds) =>
  (SEVERITY.find((band) => (seconds ?? 0) < band.max) || SEVERITY[SEVERITY.length - 1]).color;

const CATEGORY_COLOR = { ICE: C.ice, IC: C.ic, EC: C.ec, ECE: C.ece };
const categoryColor = (name) => CATEGORY_COLOR[name] || C.oth;

/* --- small helpers -------------------------------------------------------- */

const $ = (id) => document.getElementById(id);
const minutes = (seconds) => (seconds ?? 0) / 60;
const fmt = (n, d = 1) => (n === null || n === undefined || Number.isNaN(n) ? '—' : n.toFixed(d));

const clockTime = (iso) =>
  iso ? new Date(iso).toLocaleTimeString('en-GB', { hour12: false }) : '--:--:--';

function relativeAge(iso) {
  if (!iso) return '—';
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

/* Writes a panel's live reading and its severity dot. This is the difference
   between a chart you look at and one you can act on. */
function setReading(id, tone, text) {
  const el = $(id);
  if (!el) return;
  el.className = `panel-read ${tone}`;
  el.querySelector('span:last-child').textContent = text;
}

/* --- ECharts scaffolding -------------------------------------------------- */

const charts = new Map();

function chart(id) {
  if (charts.has(id)) return charts.get(id);
  const el = $(id);
  if (!el) return null;
  const instance = echarts.init(el, null, { renderer: 'canvas' });
  charts.set(id, instance);
  return instance;
}

const AXIS_LABEL = { color: C.muted, fontSize: 10.5 };
const SPLIT_LINE = { lineStyle: { color: C.line, width: 1 } };   // hairline, never dashed

const baseGrid = (over = {}) => ({ left: 46, right: 16, top: 26, bottom: 26, containLabel: true, ...over });

const baseTooltip = (extra = {}) => ({
  trigger: 'axis',
  backgroundColor: C.raise,
  borderColor: C.line,
  borderWidth: 1,
  padding: [8, 11],
  textStyle: { color: C.ink, fontSize: 11.5 },
  axisPointer: { type: 'line', lineStyle: { color: C.muted, width: 1 } },
  ...extra,
});

const timeAxis = () => ({
  type: 'time',
  axisLabel: { ...AXIS_LABEL, formatter: (v) => new Date(v).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) },
  axisLine: { lineStyle: { color: C.line } },
  axisTick: { show: false },
  splitLine: { show: false },
});

const valueAxis = (name, over = {}) => ({
  type: 'value',
  name,
  nameTextStyle: { color: C.muted, fontSize: 10, padding: [0, 0, 0, -30] },
  axisLabel: AXIS_LABEL,
  axisLine: { show: false },
  axisTick: { show: false },
  splitLine: SPLIT_LINE,
  ...over,
});

function showEmpty(id, title, hint) {
  const el = $(id);
  if (!el || el.dataset.state === 'empty') return;
  const instance = charts.get(id);
  if (instance) { instance.dispose(); charts.delete(id); }
  el.dataset.state = 'empty';
  el.innerHTML = `<div class="empty"><strong>${title}</strong><span>${hint}</span></div>`;
}

function clearEmpty(id) {
  const el = $(id);
  if (el && el.dataset.state === 'empty') { el.innerHTML = ''; delete el.dataset.state; }
}

/* --- metric strip --------------------------------------------------------- */

const counters = new Map();

/* Counts toward the new value rather than snapping, so a changing number reads
   as movement. Skipped entirely under reduced motion. */
function setCounter(key, value, decimals = 0) {
  const el = document.querySelector(`[data-counter="${key}"]`);
  if (!el) return;
  if (value === null || value === undefined || Number.isNaN(value)) { el.textContent = '—'; return; }
  const from = counters.get(key) ?? value;
  counters.set(key, value);

  if (REDUCED || from === value) { el.textContent = value.toFixed(decimals); return; }

  const start = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - start) / 500);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = (from + (value - from) * eased).toFixed(decimals);
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function sparkline(id, values, color) {
  if (!values || values.length < 4) return;   // fewer points read as a bar, not a trend
  const instance = chart(id);
  if (!instance) return;
  instance.setOption({
    animation: !REDUCED,
    grid: { left: 0, right: 0, top: 2, bottom: 2 },
    xAxis: { type: 'category', show: false },
    yAxis: { type: 'value', show: true, min: 'dataMin', max: 'dataMax', axisLabel: { show: false }, splitLine: { show: false }, axisLine: { show: false }, axisTick: { show: false } },
    series: [{
      type: 'line', data: values, showSymbol: false, smooth: 0.3,
      lineStyle: { width: 1.5, color },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
        { offset: 0, color: `${color}2e` }, { offset: 1, color: `${color}00` }] } },
    }],
  });
}

/* --- charts --------------------------------------------------------------- */

function renderTimeseries(rows) {
  if (!rows || rows.length < 2) {
    showEmpty('chart-timeseries', 'Not enough history yet',
      'The first few polls are still landing. This chart needs at least two 5-minute buckets.');
    return;
  }
  clearEmpty('chart-timeseries');
  const instance = chart('chart-timeseries');
  if (!instance) return;

  const mean = rows.map((r) => [r.bucket, minutes(r.mean_delay_seconds)]);
  const p90 = rows.map((r) => [r.bucket, minutes(r.p90_delay_seconds)]);

  instance.setOption({
    animation: !REDUCED, animationDuration: 400, animationEasing: 'quarticOut',
    tooltip: baseTooltip({ valueFormatter: (v) => `${fmt(v)} min` }),
    legend: {
      data: ['Mean delay', '90th percentile'], top: 0, right: 0,
      textStyle: { color: C.ink2, fontSize: 11 }, itemWidth: 14, itemHeight: 2, icon: 'rect',
    },
    grid: baseGrid({ top: 34 }),
    xAxis: timeAxis(),
    yAxis: valueAxis('min'),
    series: [
      { name: '90th percentile', type: 'line', data: p90, showSymbol: false, smooth: 0.25,
        lineStyle: { width: 2, color: C.d2 },
        areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
          { offset: 0, color: `${C.d2}30` }, { offset: 1, color: `${C.d2}00` }] } } },
      { name: 'Mean delay', type: 'line', data: mean, showSymbol: false, smooth: 0.25,
        lineStyle: { width: 2, color: C.ice } },
    ],
  });

  const latest = rows[rows.length - 1];
  const first = rows[0];
  const drift = minutes(latest.mean_delay_seconds) - minutes(first.mean_delay_seconds);
  const spread = minutes(latest.p90_delay_seconds) - minutes(latest.mean_delay_seconds);
  const direction = Math.abs(drift) < 0.3 ? 'holding steady' : drift > 0 ? `up ${fmt(drift)} min` : `down ${fmt(-drift)} min`;
  const tone = minutes(latest.mean_delay_seconds) > 6 ? 'bad' : minutes(latest.mean_delay_seconds) > 3 ? 'warn' : 'good';
  setReading('read-timeseries', tone,
    `Mean delay is ${fmt(minutes(latest.mean_delay_seconds))} min and ${direction} across this window. ` +
    `The p90 sits ${fmt(spread)} min above the mean, so ${spread > 8 ? 'a minority of services is carrying most of the lateness' : 'lateness is spread fairly evenly'}.`);
}

function renderPunctuality(rows) {
  if (!rows || rows.length < 2) {
    showEmpty('chart-punctuality', 'Not enough history yet', 'Punctuality needs at least two buckets to plot a trend.');
    return;
  }
  clearEmpty('chart-punctuality');
  const instance = chart('chart-punctuality');
  if (!instance) return;

  const data = rows.map((r) => [r.bucket, r.punctuality_pct]);

  instance.setOption({
    animation: !REDUCED, animationDuration: 400, animationEasing: 'quarticOut',
    tooltip: baseTooltip({ valueFormatter: (v) => `${fmt(v)}%` }),
    grid: baseGrid(),
    xAxis: timeAxis(),
    yAxis: valueAxis('%', { max: 100, min: (v) => Math.max(0, Math.floor(v.min - 5)) }),
    series: [{
      name: 'Punctuality', type: 'line', data, showSymbol: false, smooth: 0.25,
      lineStyle: { width: 2, color: C.ok },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
        { offset: 0, color: `${C.ok}33` }, { offset: 1, color: `${C.ok}00` }] } },
      markLine: {
        silent: true, symbol: 'none',
        label: { formatter: 'DB target 80%', color: C.muted, fontSize: 10, position: 'insideEndTop' },
        lineStyle: { color: C.muted, width: 1, type: 'solid', opacity: 0.5 },
        data: [{ yAxis: 80 }],
      },
    }],
  });

  const latest = rows[rows.length - 1].punctuality_pct;
  const tone = latest >= 80 ? 'good' : latest >= 65 ? 'warn' : 'bad';
  setReading('read-punctuality', tone,
    `${fmt(latest)}% of calls are running under the 6-minute threshold right now, ` +
    `${latest >= 80 ? 'at or above' : 'below'} the 80% mark DB publishes as its long-distance target.`);
}

/* The API names the bands but does not carry their bounds, so severity is
   resolved from the band label. Keeping the order here also guarantees the axis
   reads as an ordinal scale rather than whatever order SQL returned. */
const BANDS = [
  { band: 'early',            lower: -60 },
  { band: 'on time (<6 min)', lower: 0 },
  { band: '6-15 min',         lower: 360 },
  { band: '15-30 min',        lower: 900 },
  { band: '30-60 min',        lower: 1800 },
  { band: '60+ min',          lower: 3600 },
];

function renderDistribution(rows) {
  if (!rows || !rows.length) { showEmpty('chart-distribution', 'No observations yet', 'Delay bands appear once the collector has stored its first poll.'); return; }
  clearEmpty('chart-distribution');
  const instance = chart('chart-distribution');
  if (!instance) return;

  const byBand = new Map(rows.map((r) => [r.band, r.stops || 0]));
  const ordered = BANDS.filter((b) => byBand.has(b.band))
    .map((b) => ({ ...b, stops: byBand.get(b.band) }));
  const total = ordered.reduce((sum, r) => sum + r.stops, 0) || 1;

  instance.setOption({
    animation: !REDUCED, animationDuration: 400,
    tooltip: baseTooltip({ trigger: 'item', formatter: (p) => `${p.name}<br/><b>${p.value.toLocaleString()}</b> calls (${fmt((p.value / total) * 100)}%)` }),
    grid: baseGrid({ left: 8, bottom: 34 }),
    xAxis: {
      type: 'category', data: ordered.map((r) => r.band),
      axisLabel: { ...AXIS_LABEL, interval: 0, rotate: 22, hideOverlap: false },
      axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false },
    },
    yAxis: valueAxis('calls'),
    series: [{
      type: 'bar',
      data: ordered.map((r) => ({ value: r.stops, itemStyle: { color: severityColor(r.lower) } })),
      barMaxWidth: 46,
      itemStyle: { borderRadius: [4, 4, 0, 0] },   // 4px rounded data-end on the baseline
    }],
  });

  const late = ordered.filter((r) => r.lower >= 360).reduce((s, r) => s + r.stops, 0);
  const pct = (late / total) * 100;
  const biggest = ordered.reduce((a, b) => (a.stops > b.stops ? a : b));
  setReading('read-distribution', pct > 20 ? 'bad' : pct > 10 ? 'warn' : 'good',
    `${fmt(pct)}% of observed calls are 6 minutes late or worse (${late.toLocaleString()} of ${total.toLocaleString()}). ` +
    `Most of the network sits in the "${biggest.band}" band.`);
}

function renderStations(rows) {
  if (!rows || !rows.length) { showEmpty('chart-stations', 'No station data yet', 'Stations rank once at least two observations exist per station.'); return; }
  clearEmpty('chart-stations');
  const instance = chart('chart-stations');
  if (!instance) return;

  const ordered = [...rows].sort((a, b) => a.mean_delay_seconds - b.mean_delay_seconds).slice(-12);

  instance.setOption({
    animation: !REDUCED, animationDuration: 400,
    tooltip: baseTooltip({ trigger: 'item', formatter: (p) => `${p.name}<br/><b>${fmt(p.value)} min</b> mean delay` }),
    grid: baseGrid({ left: 8, right: 34 }),
    xAxis: valueAxis('min'),
    yAxis: {
      type: 'category', data: ordered.map((r) => r.stop_name),
      axisLabel: { ...AXIS_LABEL, width: 116, overflow: 'truncate' },
      axisLine: { show: false }, axisTick: { show: false },
    },
    series: [{
      type: 'bar',
      // One hue for every bar: length already encodes the value, so hue stays
      // free rather than re-encoding what the reader can already see.
      data: ordered.map((r) => minutes(r.mean_delay_seconds)),
      itemStyle: { color: C.ice, borderRadius: [0, 4, 4, 0] },
      barMaxWidth: 13,
      label: { show: true, position: 'right', color: C.ink2, fontSize: 10.5, formatter: (p) => fmt(p.value) },
    }],
  });

  const worst = ordered[ordered.length - 1];
  setReading('read-stations', minutes(worst.mean_delay_seconds) > 10 ? 'bad' : 'warn',
    `${worst.stop_name} is the worst-performing station in this window at ${fmt(minutes(worst.mean_delay_seconds))} min mean delay ` +
    `over ${worst.observations} observations.`);
}

function renderCategories(rows) {
  if (!rows || !rows.length) { showEmpty('chart-categories', 'No product data yet', 'Breakdown appears once trains from more than one product are tracked.'); return; }
  clearEmpty('chart-categories');
  const instance = chart('chart-categories');
  if (!instance) return;

  instance.setOption({
    animation: !REDUCED, animationDuration: 400,
    tooltip: baseTooltip({ trigger: 'item', formatter: (p) => `${p.name}<br/><b>${fmt(p.value)} min</b> mean delay` }),
    grid: baseGrid({ left: 8 }),
    xAxis: { type: 'category', data: rows.map((r) => r.route_category), axisLabel: { ...AXIS_LABEL, fontSize: 12, fontWeight: 600 }, axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false } },
    yAxis: valueAxis('min'),
    series: [{
      type: 'bar', barMaxWidth: 52,
      data: rows.map((r) => ({ value: minutes(r.mean_delay_seconds), itemStyle: { color: categoryColor(r.route_category), borderRadius: [4, 4, 0, 0] } })),
      label: { show: true, position: 'top', color: C.ink2, fontSize: 10.5, formatter: (p) => fmt(p.value) },
    }],
  });

  const sorted = [...rows].sort((a, b) => b.mean_delay_seconds - a.mean_delay_seconds);
  setReading('read-categories', 'good',
    `${sorted[0].route_category} is running worst at ${fmt(minutes(sorted[0].mean_delay_seconds))} min mean delay; ` +
    `${sorted[sorted.length - 1].route_category} is best at ${fmt(minutes(sorted[sorted.length - 1].mean_delay_seconds))} min.`);
}

/* The API reports arrival and departure delay separately and leaves both null
   where it has no prediction (typically a trip's first and last calls). Those
   are dropped rather than plotted as zero, which would read as "on time". */
const callDelay = (row) => (row.arrival_delay ?? row.departure_delay ?? null);

function renderPropagation(rows, label) {
  rows = (rows || []).filter((r) => callDelay(r) !== null);
  if (!rows || rows.length < 2) {
    showEmpty('chart-propagation', 'Select a service', 'Choose a row from the table to trace how its delay builds along the route.');
    return;
  }
  clearEmpty('chart-propagation');
  const instance = chart('chart-propagation');
  if (!instance) return;

  instance.setOption({
    animation: !REDUCED, animationDuration: 500,
    tooltip: baseTooltip({ valueFormatter: (v) => `${fmt(v)} min` }),
    grid: baseGrid({ bottom: 52 }),
    xAxis: {
      type: 'category', data: rows.map((r) => r.stop_name),
      axisLabel: { ...AXIS_LABEL, rotate: 32, width: 84, overflow: 'truncate', hideOverlap: true },
      axisLine: { lineStyle: { color: C.line } }, axisTick: { show: false },
    },
    yAxis: valueAxis('min'),
    series: [{
      type: 'line', data: rows.map((r) => minutes(callDelay(r))),
      smooth: 0.2, symbolSize: 8,
      lineStyle: { width: 2.5, color: C.d2 },
      itemStyle: { color: C.d2, borderColor: C.panel, borderWidth: 2 },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
        { offset: 0, color: `${C.d2}30` }, { offset: 1, color: `${C.d2}00` }] } },
    }],
  });

  const first = minutes(callDelay(rows[0]));
  const last = minutes(callDelay(rows[rows.length - 1]));
  const gained = last - first;
  setReading('read-propagation', gained > 5 ? 'bad' : gained > 1 ? 'warn' : 'good',
    `${label} left its first tracked stop ${fmt(first)} min late and is ${fmt(last)} min late by ${rows[rows.length - 1].stop_name}. ` +
    (Math.abs(gained) < 1 ? 'It is holding its delay rather than compounding it.'
      : gained > 0 ? `It has picked up ${fmt(gained)} min more along the way.`
      : `It has recovered ${fmt(-gained)} min en route.`));
}

function renderWorstTrips(rows) {
  const body = $('worst-trips');
  if (!body) return;
  if (!rows || !rows.length) {
    body.innerHTML = '<tr><td colspan="4"><div class="empty"><strong>Nothing delayed</strong><span>No service is currently reporting a delay.</span></div></td></tr>';
    return;
  }
  body.innerHTML = rows.map((r) => {
    const colour = severityColor(r.max_delay_seconds);
    return `<tr tabindex="0" role="button" data-trip="${r.trip_id}" data-label="${r.route_name || r.trip_id}"
                aria-label="Trace ${r.route_name || r.trip_id}">
      <td>${r.route_name || r.trip_id}</td>
      <td><span class="tag" style="color:${categoryColor(r.route_category)}">${r.route_category || '—'}</span></td>
      <td class="num">${r.stops ?? '—'}</td>
      <td class="num" style="color:${colour}">+${fmt(minutes(r.max_delay_seconds), 0)}</td>
    </tr>`;
  }).join('');
}

function renderImportance(model) {
  if (!model || !model.feature_importance || !model.feature_importance.length) {
    showEmpty('chart-importance', 'Model not trained yet',
      'Run <code>python -m dbrt.ml train</code> once the warehouse has a few hundred observations.');
    return;
  }
  clearEmpty('chart-importance');
  const instance = chart('chart-importance');
  if (!instance) return;

  const rows = [...model.feature_importance].sort((a, b) => a.importance - b.importance);

  instance.setOption({
    animation: !REDUCED, animationDuration: 400,
    tooltip: baseTooltip({ trigger: 'item', formatter: (p) => `${p.name}<br/><b>${fmt(p.value * 100)}%</b> of model gain` }),
    grid: baseGrid({ left: 8, right: 40 }),
    xAxis: valueAxis('share', { axisLabel: { ...AXIS_LABEL, formatter: (v) => `${Math.round(v * 100)}%` } }),
    yAxis: { type: 'category', data: rows.map((r) => r.feature), axisLabel: { ...AXIS_LABEL, width: 120, overflow: 'truncate' }, axisLine: { show: false }, axisTick: { show: false } },
    series: [{
      type: 'bar', data: rows.map((r) => r.importance),
      itemStyle: { color: C.ec, borderRadius: [0, 4, 4, 0] }, barMaxWidth: 13,
      label: { show: true, position: 'right', color: C.ink2, fontSize: 10.5, formatter: (p) => `${Math.round(p.value * 100)}%` },
    }],
  });

  const top = rows[rows.length - 1];
  setReading('read-model', 'good',
    `The model leans hardest on ${top.feature} (${Math.round(top.importance * 100)}% of total gain). ` +
    (model.mae_seconds ? `Cross-validated MAE is ${fmt(minutes(model.mae_seconds))} min against a ${fmt(minutes(model.baseline_mae_seconds))} min naive baseline.` : ''));
}

function renderIngestion(polls) {
  if (!polls || polls.length < 2) { showEmpty('chart-ingestion', 'No polls recorded yet', 'Start the collector with <code>python -m dbrt</code>.'); return; }
  clearEmpty('chart-ingestion');
  const instance = chart('chart-ingestion');
  if (!instance) return;

  instance.setOption({
    animation: !REDUCED, animationDuration: 400,
    tooltip: baseTooltip(),
    legend: { data: ['Rows written', 'Fetch duration'], top: 0, right: 0, textStyle: { color: C.ink2, fontSize: 11 }, itemWidth: 14, itemHeight: 2, icon: 'rect' },
    grid: baseGrid({ top: 34 }),
    xAxis: timeAxis(),
    // Two measures of different scale, indexed against their own maximum so
    // they share one axis instead of inventing a second.
    yAxis: valueAxis('% of window max', { max: 100 }),
    series: ['rows_written', 'duration_ms'].map((key, i) => {
      const max = Math.max(...polls.map((p) => p[key] || 0)) || 1;
      return {
        name: i === 0 ? 'Rows written' : 'Fetch duration',
        type: 'line', showSymbol: false, smooth: 0.25,
        data: polls.map((p) => [p.fetched_at, ((p[key] || 0) / max) * 100]),
        lineStyle: { width: 2, color: i === 0 ? C.ice : C.oth },
      };
    }),
  });

  const latest = polls[polls.length - 1];
  const errors = polls.filter((p) => p.error).length;
  setReading('read-ingestion', errors ? 'warn' : 'good',
    `Last poll wrote ${(latest.rows_written || 0).toLocaleString()} rows in ${latest.duration_ms || 0} ms. ` +
    (errors ? `${errors} of the last ${polls.length} polls reported an error.` : `No errors in the last ${polls.length} polls.`));
}

/* --- map ------------------------------------------------------------------ */

const TrainMap = {
  map: null,
  ready: false,
  current: new Map(),   // trip_id -> {lat, lon} being displayed
  target: new Map(),    // trip_id -> {lat, lon} most recently received
  meta: new Map(),
  lastUpdate: 0,
  raf: null,

  init() {
    if (!window.maplibregl) return;

    this.map = new maplibregl.Map({
      container: 'map',
      style: 'https://tiles.openfreemap.org/styles/dark',
      center: [10.2, 51.1],
      zoom: 5.15,
      attributionControl: { compact: true },
      // Without this the wheel zooms the map instead of scrolling the page, so
      // the dashboard traps the reader the moment the cursor crosses the map.
      cooperativeGestures: true,
      // Tiles are the one remote dependency; fail soft rather than throwing.
      transformRequest: (url) => ({ url }),
    });

    this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-left');
    this.map.on('load', () => this.onLoad());
    this.map.on('error', (e) => {
      // A tile failure must not take the trains down with it.
      console.warn('map resource failed', e && e.error && e.error.message);
    });
  },

  onLoad() {
    this.ready = true;
    this.tintBasemap();

    this.map.addSource('rail', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    this.map.addLayer({
      id: 'rail-line', type: 'line', source: 'rail',
      paint: { 'line-color': '#3a4a63', 'line-width': ['interpolate', ['linear'], ['zoom'], 4, 0.4, 8, 1.4], 'line-opacity': 0.75 },
    });

    this.map.addSource('stations', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    this.map.addLayer({
      id: 'station-dot', type: 'circle', source: 'stations',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 4, 1.2, 9, 3.4],
        'circle-color': '#55677f',
      },
    });

    this.map.addSource('trains', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    this.map.addLayer({
      id: 'train-glow', type: 'circle', source: 'trains',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 4, 6, 9, 15],
        'circle-color': ['get', 'color'], 'circle-opacity': 0.16, 'circle-blur': 0.6,
      },
    });
    this.map.addLayer({
      id: 'train-dot', type: 'circle', source: 'trains',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 4, 2.6, 9, 5.5],
        'circle-color': ['get', 'color'],
        'circle-stroke-width': 1.2,
        'circle-stroke-color': '#0b0f16',   // 2px surface ring keeps overlaps readable
      },
    });

    this.map.on('click', 'train-dot', (e) => this.showPopup(e));
    this.map.on('mouseenter', 'train-dot', () => { this.map.getCanvas().style.cursor = 'pointer'; });
    this.map.on('mouseleave', 'train-dot', () => { this.map.getCanvas().style.cursor = ''; });

    this.loadGeometry();
    this.tick();
  },

  /* Nudge the off-the-shelf dark basemap toward the dashboard's slate so the
     map reads as part of the page rather than an embed. */
  tintBasemap() {
    const recolour = { background: '#0b0f16', water: '#0e141d' };
    for (const layer of this.map.getStyle().layers || []) {
      const target = recolour[layer.id] || (layer.id.includes('water') ? recolour.water : null);
      if (!target) continue;
      try {
        if (layer.type === 'background') this.map.setPaintProperty(layer.id, 'background-color', target);
        if (layer.type === 'fill') this.map.setPaintProperty(layer.id, 'fill-color', target);
      } catch { /* style layer vocabulary varies; skip what does not apply */ }
    }
  },

  async loadGeometry() {
    try {
      const [rail, stations] = await Promise.all([
        fetch('/api/geo/network').then((r) => r.json()),
        fetch('/api/geo/stations?min_calls=6').then((r) => r.json()),
      ]);
      if (this.map.getSource('rail')) this.map.getSource('rail').setData(rail);
      if (this.map.getSource('stations')) this.map.getSource('stations').setData(stations);
    } catch (err) {
      console.warn('network geometry unavailable', err);
    }
  },

  setPositions(positions) {
    if (!positions) return;
    const seen = new Set();
    for (const p of positions) {
      seen.add(p.trip_id);
      this.target.set(p.trip_id, { lat: p.lat, lon: p.lon });
      this.meta.set(p.trip_id, p);
      if (!this.current.has(p.trip_id)) this.current.set(p.trip_id, { lat: p.lat, lon: p.lon });
    }
    for (const id of [...this.current.keys()]) {
      if (!seen.has(id)) { this.current.delete(id); this.target.delete(id); this.meta.delete(id); }
    }
    this.lastUpdate = performance.now();
    const count = $('map-train-count');
    if (count) count.textContent = positions.length.toLocaleString();
    if (REDUCED) { for (const [id, t] of this.target) this.current.set(id, { ...t }); this.paint(); }
  },

  /* Lerp displayed positions toward their targets across the push interval, so
     trains glide instead of jumping every ten seconds. */
  tick() {
    if (REDUCED) return;
    const step = () => {
      const t = Math.min(1, (performance.now() - this.lastUpdate) / PUSH_MS);
      for (const [id, target] of this.target) {
        const cur = this.current.get(id);
        if (!cur) { this.current.set(id, { ...target }); continue; }
        cur.lat += (target.lat - cur.lat) * 0.08 * (1 + t);
        cur.lon += (target.lon - cur.lon) * 0.08 * (1 + t);
      }
      this.paint();
      this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  },

  paint() {
    if (!this.ready || !this.map.getSource('trains')) return;
    const features = [];
    for (const [id, pos] of this.current) {
      const m = this.meta.get(id) || {};
      features.push({
        type: 'Feature',
        properties: {
          trip_id: id,
          color: severityColor(m.delay_seconds),
          label: m.route_name || id,
          delay: Math.round(minutes(m.delay_seconds)),
          from: m.from_stop || '', to: m.to_stop || '',
        },
        geometry: { type: 'Point', coordinates: [pos.lon, pos.lat] },
      });
    }
    this.map.getSource('trains').setData({ type: 'FeatureCollection', features });
  },

  showPopup(e) {
    const p = e.features[0].properties;
    new maplibregl.Popup({ closeButton: false, offset: 12, className: 'train-popup' })
      .setLngLat(e.lngLat)
      .setHTML(
        `<div style="font:600 12.5px Inter,sans-serif;color:#eef2f8">${p.label}</div>` +
        `<div style="font:400 11.5px Inter,sans-serif;color:#a8b6ca;margin-top:3px">${p.from} → ${p.to}</div>` +
        `<div style="font:600 11.5px ui-monospace,monospace;color:${p.color};margin-top:4px">${p.delay >= 0 ? '+' : ''}${p.delay} min</div>`)
      .addTo(this.map);
  },
};

function renderLegend() {
  const host = $('map-legend');
  if (!host) return;
  // The severity ramp is multi-hue semantic heat, so it ships with this scale
  // legend rather than relying on hue being self-evident.
  host.innerHTML = SEVERITY.map((band) =>
    `<div class="legend-row"><i style="background:${band.color}"></i>${band.label}</div>`).join('');
}

/* --- time slider ---------------------------------------------------------- */

const Timeline = {
  window: { start: null, end: null },
  live: true,
  playing: false,
  timer: null,

  init() {
    const slider = $('time-slider');
    const liveBtn = $('btn-live');
    const playBtn = $('btn-play');

    slider.addEventListener('input', () => {
      this.live = false;
      this.stop();
      this.reflectMode();
      this.seek(Number(slider.value));
    });

    liveBtn.addEventListener('click', () => {
      this.live = true;
      this.stop();
      slider.value = 100;
      this.reflectMode();
    });

    playBtn.addEventListener('click', () => (this.playing ? this.stop() : this.play()));
  },

  setWindow(w) {
    if (!w || !w.start) return;
    this.window = w;
    const start = $('tb-start');
    if (start) start.textContent = clockTime(w.start);
  },

  instantFor(pct) {
    if (!this.window.start || !this.window.end) return null;
    const a = new Date(this.window.start).getTime();
    const b = new Date(this.window.end).getTime();
    return new Date(a + (b - a) * (pct / 100));
  },

  async seek(pct) {
    const instant = this.instantFor(pct);
    if (!instant) return;
    const readout = $('tb-current');
    if (readout) readout.textContent = clockTime(instant.toISOString());
    try {
      const body = await fetch(`/api/positions?at=${encodeURIComponent(instant.toISOString())}`).then((r) => r.json());
      TrainMap.setPositions(body.positions);
    } catch (err) {
      console.warn('history seek failed', err);
    }
  },

  play() {
    if (!this.window.start) return;
    this.live = false;
    this.playing = true;
    const slider = $('time-slider');
    if (Number(slider.value) >= 100) slider.value = 0;
    this.reflectMode();
    this.timer = setInterval(() => {
      const next = Number(slider.value) + 1;
      if (next > 100) { this.stop(); return; }
      slider.value = next;
      this.seek(next);
    }, 700);
  },

  stop() {
    this.playing = false;
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
    this.reflectMode();
  },

  reflectMode() {
    const liveBtn = $('btn-live');
    const playBtn = $('btn-play');
    const mode = $('tb-mode');
    const readout = $('tb-current');

    liveBtn.classList.toggle('is-live', this.live);
    liveBtn.setAttribute('aria-pressed', String(this.live));
    playBtn.textContent = this.playing ? 'Pause' : 'Replay';
    if (mode) mode.textContent = this.live ? 'following feed' : this.playing ? 'replaying' : 'history';
    if (this.live && readout) readout.textContent = 'LIVE';
  },
};

/* --- payload wiring ------------------------------------------------------- */

const history = { punctuality: [], delay: [], trips: [] };

function pushHistory(key, value) {
  if (value === null || value === undefined || Number.isNaN(value)) return;
  history[key].push(value);
  if (history[key].length > 40) history[key].shift();
}

let selectedTrip = null;
let lastFeedTimestamp = null;

function apply(payload) {
  const summary = payload.summary || {};
  const punctuality = summary.punctuality || {};
  const feed = summary.feed || {};

  // Header
  const sourceLabel = $('source-label');
  if (sourceLabel && summary.source) {
    sourceLabel.textContent = summary.source.label;
    $('lineage').classList.toggle('official', !!summary.source.official);
  }
  lastFeedTimestamp = feed.feed_timestamp || lastFeedTimestamp;
  $('feed-clock').textContent = clockTime(lastFeedTimestamp);
  $('data-age').textContent = relativeAge(lastFeedTimestamp);

  // Metrics
  setCounter('punctuality', punctuality.punctuality_pct, 1);
  setCounter('meandelay', minutes(punctuality.mean_delay_seconds), 1);
  setCounter('trips', (payload.positions || []).length, 0);
  setCounter('maxdelay', Math.round(minutes(punctuality.max_delay_seconds)), 0);
  setCounter('skipped', (payload.cancellations || {}).skipped_stops, 0);
  setCounter('observations', summary.observations_stored, 0);

  pushHistory('punctuality', punctuality.punctuality_pct);
  pushHistory('delay', minutes(punctuality.mean_delay_seconds));
  pushHistory('trips', (payload.positions || []).length);
  sparkline('spark-punctuality', history.punctuality, C.ok);
  sparkline('spark-delay', history.delay, C.d2);
  sparkline('spark-trips', history.trips, C.ice);

  // Charts
  renderTimeseries(payload.timeseries);
  renderPunctuality(payload.timeseries);
  renderDistribution(payload.distribution);
  renderStations(payload.stations);
  renderCategories(payload.categories);
  renderWorstTrips(payload.worst_trips);
  if (payload.polls) renderIngestion(payload.polls);

  // Map: only follow the feed when the user has not scrubbed away from live.
  Timeline.setWindow(payload.history_window);
  if (Timeline.live) TrainMap.setPositions(payload.positions);

  const stats = $('feed-stats');
  if (stats) {
    stats.textContent = `${(feed.entity_count || 0).toLocaleString()} entities · ` +
      `${(feed.long_distance_trips || 0).toLocaleString()} long-distance · ${feed.duration_ms || 0} ms`;
  }
}

async function loadModel() {
  try {
    const model = await fetch('/api/model').then((r) => r.json());
    renderImportance(model);
    const info = $('model-info');
    if (info) {
      info.textContent = model.trained_at
        ? `Delay model: ${model.algorithm}, MAE ${fmt(minutes(model.mae_seconds))} min, trained ${new Date(model.trained_at).toLocaleString('en-GB')}`
        : 'Delay model: not trained';
    }
  } catch { /* the dashboard is useful without the model */ }
}

async function loadTrip(tripId, label) {
  try {
    const rows = await fetch(`/api/trips/${encodeURIComponent(tripId)}/propagation`).then((r) => r.json());
    renderPropagation(rows, label);
    const tag = $('prop-tag');
    if (tag) { tag.textContent = label; tag.style.color = C.ice; }
  } catch (err) {
    console.warn('propagation failed', err);
  }
}

document.addEventListener('click', (e) => {
  const row = e.target.closest('tr[data-trip]');
  if (!row) return;
  selectRow(row);
});

document.addEventListener('keydown', (e) => {
  const row = e.target.closest && e.target.closest('tr[data-trip]');
  if (row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); selectRow(row); }
});

function selectRow(row) {
  for (const other of document.querySelectorAll('tr[data-trip]')) other.removeAttribute('aria-selected');
  row.setAttribute('aria-selected', 'true');
  selectedTrip = row.dataset.trip;
  loadTrip(row.dataset.trip, row.dataset.label);
}

/* --- connection ----------------------------------------------------------- */

function setStatus(state, text) {
  const el = $('status');
  if (!el) return;
  el.className = `meta-chip status ${state}`;
  $('status-text').textContent = text;
}

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${scheme}://${location.host}/ws`);

  socket.addEventListener('open', () => setStatus('live', 'live'));
  socket.addEventListener('message', (event) => {
    try { apply(JSON.parse(event.data)); setStatus('live', 'live'); }
    catch (err) { console.error('bad payload', err); }
  });
  socket.addEventListener('close', () => {
    setStatus('down', 'reconnecting');
    setTimeout(connect, 3000);   // The dashboard is long-lived; always come back.
  });
  socket.addEventListener('error', () => socket.close());
}

/* Fall back to polling the REST payload when WebSockets are unavailable. */
async function pollOnce() {
  try {
    apply(await fetch('/api/dashboard').then((r) => r.json()));
    setStatus('live', 'polling');
  } catch {
    setStatus('down', 'offline');
  }
}

function boot() {
  renderLegend();
  Timeline.init();
  TrainMap.init();
  pollOnce();                    // First paint without waiting for the socket.
  loadModel();

  if ('WebSocket' in window) connect();
  else setInterval(pollOnce, PUSH_MS);

  setInterval(loadModel, 60_000);
  // Data age keeps counting between pushes so a stalled feed is visible.
  setInterval(() => {
    if (lastFeedTimestamp) $('data-age').textContent = relativeAge(lastFeedTimestamp);
  }, 1000);

  window.addEventListener('resize', () => {
    for (const instance of charts.values()) instance.resize();
  });
}

document.addEventListener('DOMContentLoaded', boot);
